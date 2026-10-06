"""ccsessiond: serves this machine's Claude Code sessions to local consumers.

  ccsessiond            local API v1 on 127.0.0.1:8788 (bearer token, see below)
  ccsessiond --stdio    the /v1/events stream as JSON lines on stdout, actions as JSON lines on
                        stdin; no port or token; exits when stdin closes
  ccsessiond --once     one /v1/live snapshot on stdout, then exit

The token is in ~/.local/state/ccsession/token (created 0600 on first run). Every request needs
`Authorization: Bearer <token>` and a Host of 127.0.0.1:<port> or localhost:<port>.

  GET  /v1/health                          version, instances and accounts, reader config, warnings
  GET  /v1/live                            {machine, now, sessions}: the live sessions
  GET  /v1/events                          SSE: snapshot, then upsert / remove / status, and health
  GET  /v1/sessions?kind=&instance=&account=&project=&archived=&since=&cursor=
  GET  /v1/sessions/{id}
  GET  /v1/sessions/{id}/transcript?prose=1
  POST /v1/sessions/{id}/archive           {"archived": true|false}
  POST /v1/sessions/{id}/open              not implemented yet (501)
  POST /v1/sessions/{id}/message           V2 (501)

Standard library only.
"""
import argparse
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.parse

from . import __version__, owner
from . import reader as R

SCHEMA = "session.v1"
DEFAULT_PORT = 8788
LIVE_POLL = 0.5     # s between reads of ~/.claude/sessions (files are rewritten in place)
INDEX_POLL = 2.0    # s between transcript and desktop-record scans
PROCS_POLL = 10.0   # s between process-table reads when the live set hasn't changed
KEEPALIVE = 15.0    # s between SSE comments on an idle stream
MAX_BODY = 64 * 1024
MAX_BACKLOG = 5000  # queued events before a stalled listener is dropped
STATUS_KEYS = {"status", "status_since", "waiting_for"}


# ---------------------------------------------------------------- config and token

def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(R.HOME, ".local", "state"), "ccsession")


def daemon_config(cfg):
    """The daemon's own keys in config.json, with defaults."""
    return {
        "port": int(cfg.get("port") or DEFAULT_PORT),
        "machine": cfg.get("machine") or socket.gethostname().split(".")[0],
        "token_file": os.path.expanduser(cfg.get("token_file") or os.path.join(state_dir(), "token")),
    }


def load_token(path):
    """The API token, created (0600) on first use. A token file others can read is tightened."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    except FileExistsError:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    with open(path) as f:
        token = f.read().strip()
    if not token:
        raise SystemExit(f"ccsessiond: empty token file {path}")
    return token


# ---------------------------------------------------------------- archive

ARCHIVED_FLAG = re.compile(rb'"isArchived"\s*:\s*(true|false)')


class Refused(Exception):
    def __init__(self, status, error, detail=""):
        super().__init__(detail or error)
        self.status, self.error, self.detail = status, error, detail


def set_archived(path, archived):
    """Sets isArchived in a desktop record by swapping the one flag in place (the rest of the file is
    untouched) and renaming a complete copy over it. Refuses unless the file holds exactly one
    isArchived flag. Returns True when the file changed."""
    with open(path, "rb") as f:
        data = f.read()
    hits = list(ARCHIVED_FLAG.finditer(data))
    if len(hits) != 1:
        raise Refused(409, "not_single_flag", f"{len(hits)} isArchived flags in the record")
    m = hits[0]
    new = data[:m.start(1)] + (b"true" if archived else b"false") + data[m.end(1):]
    if new == data:
        return False
    try:
        json.loads(new)
    except ValueError:
        raise Refused(409, "unreadable_record", "the record isn't valid JSON")
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".ccsession-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


# ---------------------------------------------------------------- transcript prose

def prose(path):
    """User prompts and assistant prose from a transcript: no tool calls or results, no thinking,
    no command or system entries, no API error notices."""
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("isSidechain") or e.get("isMeta") or e.get("isApiErrorMessage"):
                continue
            t = e.get("type")
            if t not in ("user", "assistant"):
                continue
            txt = R._text((e.get("message") or {}).get("content")).strip()
            if not txt or (t == "user" and (txt.startswith("<") or txt.startswith(R.INTERRUPTED))):
                continue
            out.append({"role": t, "text": txt, "at": R.iso_ms(e.get("timestamp"))})
    return out


# ---------------------------------------------------------------- state

class Sessions:
    """This machine's sessions, kept current by refresh(): the index (every transcript and desktop
    record), the live sessions, and the event stream's view of them (live sessions plus every desktop
    record, so a consumer sees dormant and archived sessions too). Index rows carry a change number
    for the /v1/sessions cursor."""

    def __init__(self, cfg, machine, procs=owner.process_table, alive=R.is_live):
        self.cfg = cfg
        self.machine = machine
        self.lock = threading.RLock()
        self.boot = secrets.token_hex(4)
        self.seq = 0
        self.rows = {}        # id -> index row (private keys kept, no live overlay)
        self.changed = {}     # id -> seq of its last change
        self.removed = {}     # id -> seq of its removal
        self.view = {}        # id -> public row in the event stream
        self.live = []        # public live rows
        self.health = {}
        self.listeners = set()
        self._procs_fn = procs
        self._alive_fn = alive
        self._alive_cache = {}
        self._procs = {}
        self._files = {}
        self._scans = {}
        self._desktop_cache = {}
        self._desktop = {}
        self._fails = {}      # path -> consecutive parse failures
        self._live_raw = None
        self._stats = {}
        self._last_index = self._last_procs = -1e9
        self._scan_lock = threading.Lock()

    # -- reading

    def _alive(self, pid, proc_start):
        """is_live, with the procStart check (a ps call) done once per pid and procStart."""
        if not R.pid_alive(pid):
            self._alive_cache.pop(pid, None)
            return False
        key = (proc_start or None)
        hit = self._alive_cache.get(pid)
        if hit is None or hit[0] != key:
            hit = self._alive_cache[pid] = (key, self._alive_fn(pid, proc_start))
        return hit[1]

    def _count_failures(self, kind, paths):
        """Consecutive parse failures per file of one kind (live or desktop); paths failed this read.
        Returns True when the set of warnings (two or more in a row) changed."""
        before = {p for p, (_, n) in self._fails.items() if n >= 2}
        for p in list(self._fails):
            if self._fails[p][0] == kind and p not in paths:
                del self._fails[p]
        for p in paths:
            self._fails[p] = (kind, self._fails.get(p, (kind, 0))[1] + 1)
        return before != {p for p, (_, n) in self._fails.items() if n >= 2}

    def refresh(self, force_index=False):
        """Reads what changed (the index when due or forced, then the live files) and returns the
        events it caused, which also go to every listener. The running daemon calls refresh_live and
        refresh_index from separate loops so a long index scan never holds up live status."""
        events = []
        if force_index or time.monotonic() - self._last_index >= INDEX_POLL:
            events += self.refresh_index()
        return events + self.refresh_live()

    def prime(self):
        """A fast first read: the live sessions, every desktop record, and only the live sessions'
        transcripts. The next refresh_index reads the rest."""
        live = R.read_live(os.path.join(self.cfg["claude_dir"], "sessions"), alive=self._alive)
        self.refresh_index(only={lv.get("id") for lv in live})
        self._last_index = -1e9
        return self.refresh_live()

    def refresh_live(self):
        now = time.monotonic()
        with self.lock:
            fails = []
            raw = R.read_live(os.path.join(self.cfg["claude_dir"], "sessions"), failures=fails, alive=self._alive)
            changed = self._count_failures("live", set(fails))
            changed = raw != self._live_raw or changed
            ids_changed = {lv.get("id") for lv in raw} != {lv.get("id") for lv in self._live_raw or []}
            self._live_raw = raw
            if ids_changed or now - self._last_procs >= PROCS_POLL:
                self._last_procs = now
                procs = self._procs_fn()
                if procs != self._procs:
                    self._procs = procs
                    self._resolve_owners()
                    changed = True  # owners and health.instances may have moved
            return self._rebuild() if changed else []

    def refresh_index(self, only=None):
        """Scans the transcripts and desktop records that changed (only: just these session ids'
        transcripts) outside the main lock, then applies the changed rows."""
        with self._scan_lock:
            self._last_index = time.monotonic()
            fails = []
            desktop = R.read_desktop_index(R.data_dirs(self.cfg), failures=fails, cache=self._desktop_cache)
            rows, files, stats = R.scan_index(self.cfg, files=self._files, desktop=desktop, scans=self._scans,
                                              only=only)
            self._files = files
            for p in list(self._scans):
                if p not in files:
                    del self._scans[p]
            if rows:
                owner.resolve(rows.values(), self._procs)
            with self.lock:
                warned = self._count_failures("desktop", set(fails))
                self._desktop = desktop
                self._stats = stats
                return self._rebuild() if self._apply(rows, files) or warned else []

    def _apply(self, rows, files):
        present = {k[8:] if k.startswith("desktop:") else os.path.splitext(os.path.basename(k))[0]
                   for k in files}
        changed = False
        for sid, row in rows.items():
            if sid is None:
                continue
            if sid not in self.rows or R.public(self.rows[sid]) != R.public(row):
                self._bump(sid)
                changed = True
            elif self.rows[sid] != row:
                changed = True  # private keys only (live error status, paths)
            self.rows[sid] = row
            self.removed.pop(sid, None)
        for sid in [s for s in self.rows if s not in present]:
            del self.rows[sid]
            self.changed.pop(sid, None)
            self.seq += 1
            self.removed[sid] = self.seq
            changed = True
        return changed

    def _resolve_owners(self):
        before = {sid: row.get("owner") for sid, row in self.rows.items()}
        owner.resolve(self.rows.values(), self._procs)
        for sid, row in self.rows.items():
            if row.get("owner") != before[sid]:
                self._bump(sid)

    def _bump(self, sid):
        self.seq += 1
        self.changed[sid] = self.seq

    def _rebuild(self):
        """Recomputes the live rows and the stream view, and the events between old and new."""
        raw = [dict(lv) for lv in self._live_raw or []]
        joined = R.join_live(raw, self._desktop, self.cfg["projects_root"])
        copies = {lv.get("id"): dict(self.rows[lv.get("id")]) for lv in joined if lv.get("id") in self.rows}
        live = R.live_rows(copies, joined)
        owner.resolve(live, self._procs)
        self.live = [R.public(r) for r in live]
        view = {sid: R.public(r) for sid, r in self.rows.items() if r.get("kind") != "cli"}
        view.update({r["id"]: r for r in self.live if r.get("id")})
        events = []
        for sid, row in view.items():
            old = self.view.get(sid)
            if old is None:
                events.append(("upsert", row))
            elif old != row:
                diff = {k for k in set(old) | set(row) if old.get(k) != row.get(k)}
                if diff <= STATUS_KEYS:
                    events.append(("status", {"id": sid, **{k: row.get(k) for k in sorted(STATUS_KEYS)}}))
                else:
                    events.append(("upsert", row))
        events += [("remove", {"id": sid}) for sid in self.view if sid not in view]
        self.view = view
        health = self._health()
        if health != self.health:
            self.health = health
            events.append(("health", health))
        self._send(events)
        return events

    def _health(self):
        running = owner.instances(self._procs)
        dirs = [os.path.normpath(d) for d in R.data_dirs(self.cfg)]
        accounts = {}
        for meta in self._desktop.values():
            accounts.setdefault(meta["_data_dir"], set()).add(meta["account"])
        return {
            "ccsession": __version__,
            "schema": SCHEMA,
            "machine": self.machine,
            "instances": [{"instance": os.path.basename(d), "data_dir": d, "pid": running.get(d),
                           "accounts": sorted(accounts.get(d, ()))} for d in dirs],
            "accounts": sorted(set().union(*accounts.values())) if accounts else [],
            "config": {"claude_dir": self.cfg["claude_dir"], "projects_root": self.cfg["projects_root"],
                       "app_dirs": dirs, "app_dirs_discovered": self.cfg.get("app_dirs") is None,
                       "min_path_mentions": self.cfg["min_path_mentions"]},
            "warnings": [{"path": p, "kind": kind, "error": "parse", "count": n}
                         for p, (kind, n) in sorted(self._fails.items()) if n >= 2],
            "counts": {"live": len(self.live), "sessions": len(self.rows),
                       "transcripts": self._stats.get("transcripts", 0), "desktop": len(self._desktop)},
        }

    # -- listeners

    def _send(self, events):
        for q in list(self.listeners):
            if q.qsize() > MAX_BACKLOG:
                self.listeners.discard(q)
                q.put(None)  # tells the listener it was dropped
                continue
            for ev in events:
                q.put(ev)

    def subscribe(self):
        """(queue, snapshot, health): the snapshot and every later event, in order."""
        q = queue.Queue()
        with self.lock:
            self.listeners.add(q)
            return q, self.snapshot(stream=True), self.health

    def unsubscribe(self, q):
        with self.lock:
            self.listeners.discard(q)

    # -- views

    def snapshot(self, stream=False):
        """The /v1/live envelope; stream=True gives the event stream's set (live plus desktop records)."""
        with self.lock:
            sessions = list(self.view.values()) if stream else list(self.live)
            return {"machine": self.machine, "now": int(time.time() * 1000), "sessions": sessions}

    def get(self, sid):
        with self.lock:
            if sid in self.view:
                return self.view[sid]
            row = self.rows.get(sid)
            return R.public(row) if row else None

    def query(self, params):
        """GET /v1/sessions: filtered rows, and with a cursor only rows changed since it."""
        with self.lock:
            cursor = params.get("cursor")
            since_seq, reset = 0, False
            if cursor:
                boot, _, n = cursor.partition(":")
                if boot == self.boot and n.isdigit() and int(n) <= self.seq:
                    since_seq = int(n)
                else:
                    reset = True
            ids = [sid for sid in self.rows if self.changed.get(sid, 0) > since_seq]
            rows = [self.get(sid) for sid in ids]
            removed = [sid for sid, n in self.removed.items() if n > since_seq] if since_seq else []
            out_cursor = f"{self.boot}:{self.seq}"
        rows = [r for r in rows if r and matches(r, params)]
        rows.sort(key=lambda r: r.get("last") or 0, reverse=True)
        return {"sessions": rows, "removed": removed, "cursor": out_cursor, "reset": reset}

    def transcript(self, sid):
        with self.lock:
            row = self.rows.get(sid)
            path = row and row.get("_transcript")
        if not path:
            return None
        return {"id": sid, "entries": prose(path)}

    # -- actions

    def act(self, action, sid, body):
        """An action from HTTP or stdin. Returns (status, payload)."""
        try:
            if action == "archive":
                return 200, self._archive(sid, body)
            if action == "open":
                raise Refused(501, "not_implemented", "open isn't implemented yet")
            if action == "message":
                raise Refused(501, "not_implemented", "messages are planned for a later version")
            raise Refused(400, "unknown_action", str(action))
        except Refused as e:
            return e.status, {"error": e.error, "detail": e.detail}

    def _archive(self, sid, body):
        archived = (body or {}).get("archived")
        if not isinstance(archived, bool):
            raise Refused(400, "bad_request", "archived must be true or false")
        with self.lock:
            row = self.rows.get(sid)
            record = row and row.get("_record")
        if not row:
            raise Refused(404, "not_found", sid or "")
        if not record:
            raise Refused(409, "not_desktop", "only desktop sessions can be archived")
        try:
            set_archived(record, archived)
        except FileNotFoundError:
            raise Refused(404, "record_gone", "the desktop record no longer exists")
        self.refresh(force_index=True)
        return {"id": sid, "archived": archived}


def matches(row, params):
    for key in ("kind", "instance", "account", "project"):
        if params.get(key) is not None and row.get(key) != params[key]:
            return False
    if params.get("archived") is not None and row.get("archived") != (params["archived"] in ("1", "true")):
        return False
    since = params.get("since")
    if since and since.isdigit() and (row.get("last") or 0) < int(since):
        return False
    return True


# ---------------------------------------------------------------- HTTP

ROUTE = re.compile(r"^/v1/sessions/([^/]+)(?:/(transcript|archive|open|message))?$")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ccsessiond/" + __version__
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("ccsessiond: " + (fmt % args) + "\n")

    # -- plumbing

    def _json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, error, detail=""):
        self.close_connection = True  # a request body may be left unread
        self._json(status, {"error": error, "detail": detail})

    def _allowed(self):
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._error(403, "bad_host")
            return False
        auth = self.headers.get("Authorization") or ""
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(),
                                                                    self.server.token.encode())):
            self._error(401, "unauthorized")
            return False
        return True

    def _route(self):
        url = urllib.parse.urlsplit(self.path)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        return url.path.rstrip("/") or "/", params

    # -- methods

    def do_GET(self):
        if not self._allowed():
            return
        path, params = self._route()
        s = self.server.sessions
        if path == "/v1/health":
            return self._json(200, s.health)
        if path == "/v1/live":
            return self._json(200, s.snapshot())
        if path == "/v1/events":
            return self._events()
        if path == "/v1/sessions":
            return self._json(200, s.query(params))
        m = ROUTE.match(path)
        if m and m.group(2) in (None, "transcript"):
            sid = urllib.parse.unquote(m.group(1))
            if m.group(2) is None:
                row = s.get(sid)
                return self._json(200, row) if row else self._error(404, "not_found", sid)
            if params.get("prose") != "1":
                return self._error(400, "bad_request", "only prose=1 is supported")
            out = s.transcript(sid)
            return self._json(200, out) if out else self._error(404, "not_found", sid)
        if m:
            return self._error(405, "method_not_allowed")
        self._error(404, "not_found", path)

    def do_POST(self):
        if not self._allowed():
            return
        path, _ = self._route()
        m = ROUTE.match(path)
        if not m or m.group(2) in (None, "transcript"):
            return self._error(404 if not m else 405, "not_found" if not m else "method_not_allowed")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._error(413, "too_large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._error(400, "bad_request", "body isn't JSON")
        status, payload = self.server.sessions.act(m.group(2), urllib.parse.unquote(m.group(1)), body)
        self._json(status, payload)

    def _not_allowed(self):
        if self._allowed():
            self._error(405, "method_not_allowed")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def _events(self):
        s = self.server.sessions
        q, snap, health = s.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self._sse("snapshot", snap)
            self._sse("health", health)
            while not self.server.stopping.is_set():
                try:
                    ev = q.get(timeout=KEEPALIVE)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if ev is None:
                    return
                self._sse(*ev)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            s.unsubscribe(q)

    def _sse(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())
        self.wfile.flush()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, sessions, token, verbose=False):
        super().__init__(addr, Handler)
        self.sessions, self.token, self.verbose = sessions, token, verbose
        self.stopping = threading.Event()


def loop(fn, interval, stop):
    while not stop.is_set():
        try:
            fn()
        except Exception as e:  # keep serving; the next pass retries
            sys.stderr.write(f"ccsessiond: {fn.__name__} failed: {e!r}\n")
        stop.wait(interval)


def run(sessions, stop):
    """Primes the state, then keeps it current from two background loops until stop is set."""
    sessions.prime()
    for fn, interval in ((sessions.refresh_live, LIVE_POLL), (sessions.refresh_index, INDEX_POLL)):
        threading.Thread(target=loop, args=(fn, interval, stop), daemon=True).start()


def serve(sessions, port, token, verbose=False, ready=None):
    server = Server(("127.0.0.1", port), sessions, token, verbose)
    stop = server.stopping
    run(sessions, stop)
    if ready:
        ready(server)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


# ---------------------------------------------------------------- stdio

def stdio(sessions, stdin=None, stdout=None):
    """Events as JSON lines on stdout; actions as JSON lines on stdin, each answered with a result
    line: {"action": "archive", "id": "<session>", "archived": true, "req": "<anything>"}. Returns
    when stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    out_lock = threading.Lock()
    stop = threading.Event()

    def emit(event, data):
        line = json.dumps({"event": event, "data": data}, separators=(",", ":"))
        with out_lock:
            stdout.write(line + "\n")
            stdout.flush()

    def actions():
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    raise ValueError
            except ValueError:
                emit("result", {"req": None, "action": None, "id": None, "status": 400,
                                "result": {"error": "bad_request", "detail": "not a JSON object"}})
                continue
            status, result = sessions.act(msg.get("action"), msg.get("id"), msg)
            emit("result", {"req": msg.get("req"), "action": msg.get("action"), "id": msg.get("id"),
                            "status": status, "result": result})
        stop.set()

    run(sessions, stop)
    q, snap, health = sessions.subscribe()
    emit("snapshot", snap)
    emit("health", health)
    threading.Thread(target=actions, daemon=True).start()
    try:
        while not stop.is_set():
            try:
                ev = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev is None:  # dropped for falling behind: start over with a fresh snapshot
                q, snap, health = sessions.subscribe()
                emit("snapshot", snap)
                continue
            emit(*ev)
    except BrokenPipeError:
        pass
    finally:
        sessions.unsubscribe(q)


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="ccsessiond", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config file (default ~/.config/ccsession/config.json)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="events on stdout, actions on stdin")
    mode.add_argument("--once", action="store_true", help="print one /v1/live snapshot and exit")
    ap.add_argument("--port", type=int, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    ap.add_argument("--verbose", action="store_true", help="log requests to stderr")
    ap.add_argument("--version", action="version", version=f"ccsessiond {__version__} ({SCHEMA})")
    args = ap.parse_args(argv)
    cfg = R.load_config(args.config)
    dc = daemon_config(cfg)
    sessions = Sessions(cfg, dc["machine"])
    if args.once:
        sessions.prime()
        json.dump(sessions.snapshot(), sys.stdout, indent=1)
        print()
        return 0
    if args.stdio:
        stdio(sessions)
        return 0
    serve(sessions, args.port or dc["port"], load_token(dc["token_file"]), verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""ccsessiond: the local API, the event stream, --stdio and --once, over a made-up home.

Output is validated against schema/session.v1.json (with unknown keys refused) when jsonschema is
installed (`uv run python -m unittest discover -s tests`)."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import ccsession.daemon as D  # noqa: E402
import ccsession.reader as R  # noqa: E402
from test_schema import closed, jsonschema, needs_jsonschema, schema, validator  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOKEN = "test-token"
ACCOUNT = "0a1b2c3d-0000-4000-8000-000000000001"
ORG = "9e8d7c6b-0000-4000-8000-0000000000f1"


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        if isinstance(obj, list):
            f.write("".join(json.dumps(o) + "\n" for o in obj))
        elif isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, indent=2)


def entry(kind, minute, content, cwd, **kw):
    return dict({"type": kind, "cwd": cwd, "gitBranch": "main", "timestamp": "2026-10-01T12:%02d:00.000Z" % minute,
                 "message": {"role": kind, "content": content}}, **kw)


STUB = """#!{python}
# A stand-in for Session Tiles' claude-open: records its arguments, answers as the real one does.
# The exit code (or "sleep") comes from the file "mode" next to it.
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
path = os.path.join(here, "mode")
mode = open(path).read().strip() if os.path.exists(path) else "0"
if mode == "sleep":
    time.sleep(10)
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
code = int(mode)
if code == 0:
    print(json.dumps({{"instance": os.path.basename(args["--data-dir"]), "dataDir": args["--data-dir"],
                      "pid": int(args.get("--pid", 4321)), "delivered": True, "launched": "--pid" not in args,
                      "elapsedMs": 3}}))
else:
    print(json.dumps({{"error": "stub failure %d" % code, "code": code}}))
sys.exit(code)
"""


class StubClaudeOpen:
    def __init__(self, base):
        self.dir = os.path.join(base, "stub")
        os.makedirs(self.dir)
        self.path = os.path.join(self.dir, "claude-open")
        write(self.path, STUB.format(python=sys.executable))
        os.chmod(self.path, 0o755)

    def set(self, mode):
        write(os.path.join(self.dir, "mode"), str(mode))

    def calls(self):
        path = os.path.join(self.dir, "calls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]


class Home:
    """A made-up machine: one desktop instance with a live Code session (this process's pid), a
    dormant archived one and one whose record holds two isArchived flags; plus a cli session."""

    def __init__(self, base):
        self.base = base
        self.root = os.path.join(base, "Projects")
        self.garden = os.path.join(self.root, "garden")
        os.makedirs(self.garden)
        self.claude = os.path.join(base, ".claude")
        self.data_dir = os.path.join(base, "Library", "Application Support", "Claude")
        self.cfg = dict(R.DEFAULT_CONFIG, claude_dir=self.claude, projects_root=self.root, app_dirs=[self.data_dir])
        proj = os.path.join(self.claude, "projects", "-garden")
        self.transcript = os.path.join(proj, "s-live.jsonl")
        write(self.transcript, [
            entry("user", 0, "plan the beds", self.garden),
            entry("assistant", 1, [{"type": "text", "text": "Planning."},
                                   {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}}],
                  self.garden),
            entry("user", 2, [{"type": "tool_result", "tool_use_id": "t1", "content": "SECRET-TOOL-OUTPUT"}],
                  self.garden),
            entry("assistant", 3, [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Beds planned."}],
                  self.garden),
        ])
        write(os.path.join(proj, "s-old.jsonl"), [entry("user", 0, "old work", self.garden),
                                                  entry("assistant", 1, [{"type": "text", "text": "ok"}], self.garden)])
        write(os.path.join(proj, "s-cli.jsonl"), [entry("user", 0, "a terminal session", self.garden),
                                                  entry("assistant", 1, [{"type": "text", "text": "ok"}], self.garden)])
        sess = os.path.join(self.data_dir, "claude-code-sessions", ACCOUNT, ORG)
        self.records = {}
        for name, cli, archived in (("local_live", "s-live", False), ("local_old", "s-old", True)):
            self.records[cli] = os.path.join(sess, name + ".json")
            write(self.records[cli], {"sessionId": name, "cliSessionId": cli, "cwd": self.garden,
                                      "title": "Session " + cli, "createdAt": 1790000000000,
                                      "lastActivityAt": 1790000060000, "isArchived": archived})
        self.records["s-twice"] = os.path.join(sess, "local_twice.json")
        write(self.records["s-twice"], '{"sessionId": "local_twice", "cliSessionId": "s-twice", "isArchived": false,'
                                       ' "copy": {"isArchived": false}}')
        self.live_file = os.path.join(self.claude, "sessions", "1.json")
        self.set_live("idle")

    def set_live(self, status, updated=1790000030000):
        """Rewrites the live file in place, as Claude Code does."""
        write(self.live_file, {"pid": os.getpid(), "sessionId": "s-live", "cwd": self.garden, "status": status,
                               "entrypoint": "claude-desktop", "startedAt": 1790000000000,
                               "statusUpdatedAt": updated, "hostSessionId": "local_live"})

    def config_file(self):
        path = os.path.join(self.base, "config.json")
        write(path, {k: self.cfg[k] for k in ("claude_dir", "projects_root", "app_dirs")})
        return path


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(self.tmp.name)
        self.sessions = D.Sessions(self.home.cfg, "test-mac", procs=lambda: {})

    def tearDown(self):
        self.tmp.cleanup()


class TestSessions(DaemonCase):
    def test_events(self):
        self.sessions.prime()
        self.sessions.refresh(force_index=True)
        self.home.set_live("waiting", 1790000040000)
        events = self.sessions.refresh()
        self.assertEqual(events, [("status", {"id": "s-live", "status": "waiting", "status_since": 1790000040000,
                                              "waiting_for": None})])
        os.unlink(self.home.live_file)
        (kind, row), = [e for e in self.sessions.refresh() if e[0] != "health"]
        self.assertEqual((kind, row["id"], row["status"], row["dormant"]), ("upsert", "s-live", None, True))

    def test_stream_covers_live_and_desktop_sessions(self):
        self.sessions.prime()
        self.sessions.refresh(force_index=True)
        view = self.sessions.snapshot(stream=True)["sessions"]
        self.assertEqual({s["id"] for s in view}, {"s-live", "s-old", "s-twice"})
        self.assertEqual([s["id"] for s in self.sessions.snapshot()["sessions"]], ["s-live"])

    def test_prime_reads_only_live_transcripts(self):
        self.sessions.prime()
        rows = self.sessions.rows
        self.assertEqual(rows["s-live"]["last_reply"], "Beds planned.")
        self.assertNotIn("s-cli", rows)
        self.assertFalse(rows["s-old"]["first_prompt"])  # from the record until the full scan
        self.sessions.refresh_index()
        self.assertEqual(rows["s-old"]["first_prompt"], "old work")
        self.assertIn("s-cli", rows)

    def test_parse_failure_twice_is_a_warning(self):
        write(os.path.join(self.home.claude, "sessions", "2.json"), "{not json")
        self.sessions.prime()
        self.assertEqual(self.sessions.health["warnings"], [])
        self.sessions.refresh_live()
        self.sessions.refresh_live()
        (w,) = self.sessions.health["warnings"]
        self.assertEqual((w["kind"], w["error"], os.path.basename(w["path"])), ("live", "parse", "2.json"))

    def test_owner_and_live_error_status(self):
        self.sessions.prime()
        self.assertEqual(self.sessions.get("s-live")["owner"], {"data_dir": self.home.data_dir, "pid": None})
        with open(self.home.transcript, "a") as f:
            f.write(json.dumps(entry("assistant", 4, [{"type": "text", "text": "limit"}], self.home.garden,
                                     isApiErrorMessage=True, error="rate_limit", apiErrorStatus=429,
                                     quotaLimits={"resetsAt": 1790010000})) + "\n")
        self.sessions.refresh(force_index=True)
        row = self.sessions.get("s-live")
        self.assertEqual((row["status"], row["error_kind"], row["api_status"], row["resets_at"], row["outcome"]),
                         ("error", "rate_limit", 429, 1790010000000, "error"))

    def append(self, *entries):
        with open(self.home.transcript, "a") as f:
            f.write("".join(json.dumps(e) + "\n" for e in entries))

    def launch_background_agent(self, minute=5):
        g = self.home.garden
        self.append(
            entry("assistant", minute, [{"type": "tool_use", "id": "t9", "name": "Agent",
                                         "input": {"description": "weed", "prompt": "weed", "run_in_background": True}}], g),
            entry("user", minute, [{"type": "tool_result", "tool_use_id": "t9", "content": "launched"}], g,
                  toolUseResult={"status": "async_launched", "isAsync": True, "agentId": "a0weed"}),
            entry("assistant", minute, [{"type": "text", "text": "Weeding in the background."}], g))

    def notify(self, minute=7):
        self.append({"type": "queue-operation", "operation": "enqueue",
                     "timestamp": "2026-10-01T12:%02d:00.000Z" % minute,
                     "content": "<task-notification>\n<task-id>a0weed</task-id>\n<status>completed</status>\n"
                                "<summary>Agent \"weed\" finished</summary>\n</task-notification>"})

    def test_running_subagent_keeps_an_idle_session_busy(self):
        """The live file says idle throughout: the transcript scan alone moves the status."""
        self.sessions.prime()
        self.sessions.refresh(force_index=True)
        self.assertEqual(self.sessions.get("s-live")["status"], "idle")
        for change, status, since in ((self.launch_background_agent, "busy", "12:05"),
                                      (self.notify, "idle", "12:07")):
            change()
            events = [e for e in self.sessions.refresh(force_index=True) if e[0] != "health"]
            (kind, row), = events  # an upsert: the transcript's last and last_reply moved too
            self.assertEqual((kind, row["id"], row["status"], row["status_since"]),
                             ("upsert", "s-live", status, R.iso_ms("2026-10-01T%s:00Z" % since)))
            self.assertEqual(self.sessions.get("s-live")["status"], status)

    def test_idle_within_2s_after_the_last_subagent(self):
        """With the real poll intervals, the final notification turns the session idle within about
        2 s (INDEX_POLL, plus the scan)."""
        self.launch_background_agent()
        stop = threading.Event()
        D.run(self.sessions, stop)
        try:
            self.assertEqual(self.sessions.get("s-live")["status"], "busy")
            while self.sessions._last_index < 0:  # the index loop's first pass has started...
                time.sleep(0.01)
            with self.sessions._scan_lock:  # ...and finished, so a full INDEX_POLL wait follows
                pass
            t0 = time.monotonic()
            self.notify()
            while self.sessions.get("s-live")["status"] != "idle" and time.monotonic() - t0 < 5:
                time.sleep(0.05)
            elapsed = time.monotonic() - t0
        finally:
            stop.set()
        self.assertEqual(self.sessions.get("s-live")["status"], "idle")
        self.assertLess(elapsed, D.INDEX_POLL + 0.5)


class TestArchive(unittest.TestCase):
    def test_swaps_one_flag_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "local_x.json")
            text = '{\n  "title": "x",\n  "isArchived" : false,\n  "n": 1\n}\n'
            write(path, text)
            os.chmod(path, 0o640)
            ino = os.stat(path).st_ino
            self.assertTrue(D.set_archived(path, True))
            with open(path) as f:
                self.assertEqual(f.read(), text.replace('"isArchived" : false', '"isArchived" : true'))
            self.assertNotEqual(os.stat(path).st_ino, ino)  # renamed over, not rewritten
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
            self.assertFalse(D.set_archived(path, True))
            self.assertEqual(os.listdir(d), ["local_x.json"])

    def test_refuses_unless_exactly_one_flag(self):
        with tempfile.TemporaryDirectory() as d:
            for text in ('{"title": "x"}', '{"isArchived": false, "copy": {"isArchived": true}}'):
                path = os.path.join(d, "local_x.json")
                write(path, text)
                with self.assertRaises(D.Refused) as cm:
                    D.set_archived(path, True)
                self.assertEqual(cm.exception.error, "not_single_flag")
                with open(path) as f:
                    self.assertEqual(f.read(), text)


class TestOpen(DaemonCase):
    def setUp(self):
        super().setUp()
        self.stub = StubClaudeOpen(self.tmp.name)
        self.procs = {}
        self.sessions = D.Sessions(self.home.cfg, "test-mac", procs=lambda: self.procs, claude_open=self.stub.path,
                                   platform="darwin")
        self.sessions.refresh(force_index=True)
        self.link = "claude://claude.ai/epitaxy/local_live"

    def running(self, pid=500):
        self.procs = {pid: (1, "/Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=" + self.home.data_dir)}

    def test_delivers_to_the_running_owner(self):
        self.running()
        status, body = self.sessions.act("open", "s-live", {})
        self.assertEqual((status, body), (200, {"id": "s-live", "instance": "Claude", "data_dir": self.home.data_dir,
                                                "pid": 500, "delivered": True, "launched": False}))
        self.assertEqual(self.stub.calls(), [["--data-dir", self.home.data_dir, "--pid", "500", "--url", self.link]])

    def test_owner_not_running_is_launched(self):
        status, body = self.sessions.act("open", "s-old", {})  # dormant (and archived) desktop session
        self.assertEqual((status, body["launched"], body["pid"]), (200, True, 4321))
        self.assertEqual(self.stub.calls(), [["--data-dir", self.home.data_dir, "--url",
                                              "claude://claude.ai/epitaxy/local_old"]])

    def test_owner_is_resolved_when_opening(self):
        self.sessions.act("open", "s-live", {})
        self.running(501)  # the instance started after the last process-table read
        self.sessions.act("open", "s-live", {})
        self.assertEqual([c[:4] for c in self.stub.calls()],
                         [["--data-dir", self.home.data_dir, "--url", self.link],
                          ["--data-dir", self.home.data_dir, "--pid", "501"]])

    def test_claude_open_failures(self):
        for code, status, error in ((2, 500, "claude_open_usage"), (3, 409, "not_openable"),
                                    (4, 403, "consent_denied"), (5, 504, "launch_failed"), (6, 502, "send_failed"),
                                    (9, 500, "claude_open_failed")):
            self.stub.set(code)
            self.assertEqual(self.sessions.act("open", "s-live", {}),
                             (status, {"error": error, "detail": f"stub failure {code}"}))

    def test_timeout(self):
        self.stub.set("sleep")
        timeout, D.OPEN_TIMEOUT = D.OPEN_TIMEOUT, 0.5
        try:
            self.assertEqual(self.sessions.act("open", "s-live", {})[:1], (504,))
        finally:
            D.OPEN_TIMEOUT = timeout

    def test_not_openable(self):
        self.assertEqual(self.sessions.act("open", "s-cli", {})[0], 409)
        self.assertEqual(self.sessions.act("open", "nope", {})[0], 404)
        cowork = dict(self.sessions.get("s-live"), kind="cowork")  # no deep link for Cowork is known
        with mock.patch.object(self.sessions, "get", return_value=cowork):
            self.assertEqual(self.sessions.act("open", "s-live", {})[0], 409)
        self.sessions.platform = "linux"
        self.assertEqual(self.sessions.act("open", "s-live", {}), (409, {
            "error": "not_openable", "detail": "opening needs the Claude desktop app (macOS)"}))
        self.assertEqual(self.stub.calls(), [])

    def test_claude_open_missing(self):
        for path in (None, os.path.join(self.tmp.name, "nope")):
            self.sessions.claude_open = path
            self.assertEqual(self.sessions.act("open", "s-live", {})[1]["error"], "claude_open_missing")

    def test_flag_wins_over_config(self):
        self.assertEqual(D.daemon_config({"claude_open": "~/a"})["claude_open"], os.path.expanduser("~/a"))
        self.assertEqual(D.daemon_config({"claude_open": "~/a"}, "/b")["claude_open"], "/b")
        self.assertIsNone(D.daemon_config({})["claude_open"])


class TestHTTP(DaemonCase):
    def setUp(self):
        super().setUp()
        self.stop = threading.Event()
        self._index_poll = D.INDEX_POLL
        D.INDEX_POLL = 0.3  # the live poll keeps its default, so the SSE timing test is real
        self.server = D.Server(("127.0.0.1", 0), self.sessions, TOKEN)
        self.port = self.server.server_address[1]
        D.run(self.sessions, self.server.stopping)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.sessions.refresh(force_index=True)

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        D.INDEX_POLL = self._index_poll
        super().tearDown()

    def request(self, method, path, body=None, token=TOKEN, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        if token:
            h["Authorization"] = "Bearer " + token
        if body is not None:
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        if host:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", host)
            for k, v in h.items():
                c.putheader(k, v)
            c.endheaders(body.encode() if body else None)
        else:
            c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), (json.loads(data) if data else None)

    def test_token_required(self):
        self.assertEqual(self.request("GET", "/v1/health", token=None)[0], 401)
        self.assertEqual(self.request("GET", "/v1/health", token="wrong")[0], 401)
        self.assertEqual(self.request("GET", "/v1/health")[0], 200)

    def test_token_file_is_private(self):
        path = os.path.join(self.tmp.name, "state", "token")
        token = D.load_token(path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        os.chmod(path, 0o644)
        self.assertEqual(D.load_token(path), token)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_wrong_host_refused(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", f"192.168.1.2:{self.port}"):
            with self.subTest(host):
                self.assertEqual(self.request("GET", "/v1/health", host=host)[0], 403)
        self.assertEqual(self.request("GET", "/v1/health", host=f"localhost:{self.port}")[0], 200)

    def test_no_cors_headers(self):
        origin = {"Origin": "https://dash.example"}
        for method, path, token in (("GET", "/v1/live", TOKEN), ("GET", "/v1/live", None),
                                    ("OPTIONS", "/v1/live", TOKEN), ("POST", "/v1/sessions/s-live/open", TOKEN)):
            with self.subTest(f"{method} {path} token={bool(token)}"):
                status, headers, _ = self.request(method, path, token=token, headers=origin)
                self.assertEqual([h for h in headers if h.lower().startswith("access-control")], [])
        self.assertEqual(self.request("OPTIONS", "/v1/live")[0], 405)

    def test_health(self):
        _, _, h = self.request("GET", "/v1/health")
        self.assertEqual((h["ccsession"], h["schema"], h["machine"]), (D.__version__, "session.v1", "test-mac"))
        self.assertEqual(h["instances"], [{"instance": "Claude", "data_dir": self.home.data_dir, "pid": None,
                                           "accounts": [ACCOUNT]}])
        self.assertEqual(h["accounts"], [ACCOUNT])
        self.assertEqual(h["warnings"], [])

    @needs_jsonschema
    def test_live_validates(self):
        status, _, snap = self.request("GET", "/v1/live")
        self.assertEqual(status, 200)
        self.assertEqual([e.message for e in validator(closed(schema()), "live").iter_errors(snap)], [])
        (s,) = snap["sessions"]
        self.assertEqual((s["id"], s["kind"], s["status"], s["instance"], s["account"]),
                         ("s-live", "code", "idle", "Claude", ACCOUNT))

    @needs_jsonschema
    def test_sessions_validate(self):
        _, _, out = self.request("GET", "/v1/sessions")
        v = validator(closed(schema()))
        self.assertEqual({s["id"] for s in out["sessions"]}, {"s-live", "s-old", "s-cli", "s-twice"})
        for s in out["sessions"]:
            self.assertEqual([e.message for e in v.iter_errors(s)], [], s["id"])
        _, _, one = self.request("GET", "/v1/sessions/s-old")
        self.assertEqual([e.message for e in v.iter_errors(one)], [])
        self.assertEqual((one["archived"], one["dormant"], one["status"]), (True, True, None))

    def test_filters(self):
        def ids(q):
            return {s["id"] for s in self.request("GET", "/v1/sessions?" + q)[2]["sessions"]}
        self.assertEqual(ids("kind=cli"), {"s-cli"})
        self.assertEqual(ids("archived=true"), {"s-old"})
        self.assertEqual(ids("instance=Claude&archived=false"), {"s-live", "s-twice"})
        self.assertEqual(ids("project=garden&kind=code"), {"s-live", "s-old"})
        self.assertEqual(ids(f"account={ACCOUNT}&kind=cowork"), set())
        self.assertEqual(ids("since=%d" % R.iso_ms("2026-10-01T12:02:00Z")), {"s-live"})

    def test_cursor_returns_changed_rows(self):
        _, _, first = self.request("GET", "/v1/sessions")
        _, _, again = self.request("GET", "/v1/sessions?cursor=" + first["cursor"])
        self.assertEqual((again["sessions"], again["removed"], again["reset"]), ([], [], False))
        with open(os.path.join(self.home.claude, "projects", "-garden", "s-cli.jsonl"), "a") as f:
            f.write(json.dumps(entry("user", 5, "one more thing", self.home.garden)) + "\n")
        os.unlink(os.path.join(self.home.claude, "projects", "-garden", "s-old.jsonl"))
        os.unlink(self.home.records["s-old"])
        deadline = time.time() + 5
        while time.time() < deadline:
            _, _, changed = self.request("GET", "/v1/sessions?cursor=" + first["cursor"])
            if changed["sessions"] and changed["removed"]:
                break
            time.sleep(0.1)
        self.assertEqual([s["id"] for s in changed["sessions"]], ["s-cli"])
        self.assertEqual(changed["sessions"][0]["last_prompt"], "one more thing")
        self.assertEqual(changed["removed"], ["s-old"])
        _, _, stale = self.request("GET", "/v1/sessions?cursor=gone:3")
        self.assertTrue(stale["reset"])
        self.assertEqual(len(stale["sessions"]), 3)

    def test_session_not_found(self):
        self.assertEqual(self.request("GET", "/v1/sessions/nope")[0], 404)
        self.assertEqual(self.request("GET", "/v1/nope")[0], 404)

    def test_transcript_prose(self):
        status, _, out = self.request("GET", "/v1/sessions/s-live/transcript?prose=1")
        self.assertEqual(status, 200)
        self.assertEqual([(e["role"], e["text"]) for e in out["entries"]],
                         [("user", "plan the beds"), ("assistant", "Planning."), ("assistant", "Beds planned.")])
        self.assertNotIn("SECRET-TOOL-OUTPUT", json.dumps(out))
        self.assertNotIn("ls -la", json.dumps(out))
        self.assertEqual(self.request("GET", "/v1/sessions/s-live/transcript")[0], 400)

    def test_archive(self):
        status, _, out = self.request("POST", "/v1/sessions/s-live/archive", {"archived": True})
        self.assertEqual((status, out), (200, {"id": "s-live", "archived": True}))
        with open(self.home.records["s-live"]) as f:
            self.assertTrue(json.load(f)["isArchived"])
        self.assertTrue(self.request("GET", "/v1/sessions/s-live")[2]["archived"])
        self.assertEqual(self.request("POST", "/v1/sessions/s-live/archive", {"archived": False})[0], 200)
        self.assertFalse(self.request("GET", "/v1/sessions/s-live")[2]["archived"])

    def test_archive_refusals(self):
        with open(self.home.records["s-twice"]) as f:
            before = f.read()
        status, _, out = self.request("POST", "/v1/sessions/s-twice/archive", {"archived": True})
        self.assertEqual((status, out["error"]), (409, "not_single_flag"))
        with open(self.home.records["s-twice"]) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.request("POST", "/v1/sessions/s-cli/archive", {"archived": True})[2]["error"],
                         "not_desktop")
        self.assertEqual(self.request("POST", "/v1/sessions/nope/archive", {"archived": True})[0], 404)
        self.assertEqual(self.request("POST", "/v1/sessions/s-live/archive", {"archived": "yes"})[0], 400)

    def test_open(self):
        stub = StubClaudeOpen(self.tmp.name)
        self.sessions.claude_open, self.sessions.platform = stub.path, "darwin"
        status, _, body = self.request("POST", "/v1/sessions/s-live/open", {})
        self.assertEqual((status, body["instance"], body["delivered"]), (200, "Claude", True))
        stub.set(4)
        status, _, body = self.request("POST", "/v1/sessions/s-live/open", {})
        self.assertEqual((status, body["error"]), (403, "consent_denied"))
        self.assertEqual(self.request("POST", "/v1/sessions/s-cli/open", {})[0], 409)

    def test_message_not_implemented(self):
        self.assertEqual(self.request("POST", "/v1/sessions/s-live/message", {"text": "hi"})[0], 501)

    def test_sse_status_within_2s(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/v1/events", headers={"Authorization": "Bearer " + TOKEN})
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/event-stream"))
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

        def next_event():
            event = data = None
            while True:
                line = r.fp.readline().decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "" and event:
                    return event, data

        kind, snap = next_event()
        self.assertEqual(kind, "snapshot")
        self.assertEqual({s["id"] for s in snap["sessions"]}, {"s-live", "s-old", "s-twice"})
        self.assertEqual(next_event()[0], "health")
        t0 = time.monotonic()
        self.home.set_live("busy", 1790000050000)
        while True:
            kind, data = next_event()
            if kind == "status":
                break
        elapsed = time.monotonic() - t0
        self.assertEqual(data, {"id": "s-live", "status": "busy", "status_since": 1790000050000, "waiting_for": None})
        self.assertLess(elapsed, 2.0)
        c.close()


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(self.tmp.name)
        self.env = dict(os.environ, PYTHONPATH=ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_once(self):
        out = subprocess.run([sys.executable, "-m", "ccsession.daemon", "--once", "--config", self.home.config_file()],
                             capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        snap = json.loads(out.stdout)
        self.assertEqual([s["id"] for s in snap["sessions"]], ["s-live"])
        if jsonschema:
            self.assertEqual([e.message for e in validator(closed(schema()), "live").iter_errors(snap)], [])

    def test_stdio(self):
        stub = StubClaudeOpen(self.tmp.name)
        p = subprocess.Popen([sys.executable, "-m", "ccsession.daemon", "--stdio", "--config", self.home.config_file(),
                              "--claude-open", stub.path],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=self.env)
        try:
            lines = []

            def read_until(event):
                while True:
                    line = p.stdout.readline()
                    if not line:
                        self.fail("stdout closed early: " + p.stderr.read())
                    msg = json.loads(line)
                    lines.append(msg)
                    if msg["event"] == event:
                        return msg["data"]

            snap = read_until("snapshot")
            self.assertEqual({s["id"] for s in snap["sessions"]}, {"s-live", "s-old", "s-twice"})
            read_until("health")
            p.stdin.write(json.dumps({"action": "archive", "id": "s-live", "archived": True, "req": "r1"}) + "\n")
            p.stdin.write("not json\n")
            p.stdin.write(json.dumps({"action": "open", "id": "s-live", "req": "r2"}) + "\n")
            p.stdin.flush()
            results = {}
            while len(results) < 3:
                r = read_until("result")
                results[r["req"]] = r
            self.assertEqual((results["r1"]["status"], results["r1"]["result"]), (200, {"id": "s-live", "archived": True}))
            self.assertEqual(results[None]["status"], 400)
            if sys.platform == "darwin":
                self.assertEqual((results["r2"]["status"], results["r2"]["result"]["delivered"]), (200, True))
                self.assertEqual(stub.calls()[0][-2:], ["--url", "claude://claude.ai/epitaxy/local_live"])
            else:
                self.assertEqual(results["r2"]["status"], 409)
            with open(self.home.records["s-live"]) as f:
                self.assertTrue(json.load(f)["isArchived"])
            t0 = time.monotonic()
            self.home.set_live("waiting", 1790000070000)
            status = read_until("status")
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertEqual(status["status"], "waiting")
            p.stdin.close()
            self.assertEqual(p.wait(timeout=5), 0)
            if jsonschema:
                v = validator(closed(schema()))
                for msg in lines:
                    rows = (msg["data"]["sessions"] if msg["event"] == "snapshot"
                            else [msg["data"]] if msg["event"] == "upsert" else [])
                    for row in rows:
                        self.assertEqual([e.message for e in v.iter_errors(row)], [], row["id"])
                self.assertTrue(any(m["event"] == "upsert" and m["data"]["archived"] for m in lines))
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdout.close()
            p.stderr.close()


if __name__ == "__main__":
    unittest.main()

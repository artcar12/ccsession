"""Claude Code session reader.

Reads this machine's Claude Code state and turns it into plain session rows:

  live      ~/.claude/sessions/<pid>.json        sessions running right now (status, waitingFor)
  index     ~/.claude/projects/*/*.jsonl         every transcript, scanned incrementally by mtime
  desktop   <data dir>/claude-code-sessions      desktop-app index per account (title, summary, PRs)
  cowork    <data dir>/local-agent-mode-sessions

  python3 -m ccsession            print counts of what was read
  python3 -m ccsession --json     print the live sessions and the index as JSON

Standard library only, so it runs unchanged on macOS and Linux.
"""
import argparse
import calendar
import glob
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
APP_SUPPORT = os.path.join(HOME, "Library", "Application Support")

DEFAULT_CONFIG = {
    "claude_dir": os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"),
    "projects_root": os.path.join(HOME, "Projects"),
    # Desktop-app data dirs. None reads every Application Support/Claude* dir that has
    # claude-code-sessions (see discover_data_dirs).
    "app_dirs": None,
    # A session with no repo cwd is attributed to the project its tool calls touched this often.
    "min_path_mentions": 3,
}

SNIPPET = 280  # max characters kept from a prompt or reply


# ---------------------------------------------------------------- helpers

def config_path():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                        "ccsession", "config.json")


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = path or config_path()
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    for k in ("claude_dir", "projects_root"):
        cfg[k] = os.path.expanduser(cfg[k])
    if cfg["app_dirs"] is not None:
        cfg["app_dirs"] = [os.path.expanduser(p) for p in cfg["app_dirs"]]
    return cfg


def iso_ms(ts):
    """ISO-8601 UTC transcript timestamp ("2026-10-05T13:11:02.123Z") -> epoch ms."""
    if not ts:
        return None
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) * 1000
    except ValueError:
        return None


def to_ms(v):
    """Desktop-index timestamps arrive as int, float or numeric string."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def clip(text, n=SNIPPET):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def project_of(cwd, projects_root):
    """Repo name for a working dir: the first path segment under projects_root, with
    .claude/worktrees/<x> folded back into its repo. None for the root itself or outside it."""
    if not cwd:
        return None
    cwd = cwd.split("/.claude/worktrees/")[0].rstrip("/")
    root = projects_root.rstrip("/")
    if cwd.startswith(root + "/"):
        return cwd[len(root) + 1:].split("/")[0] or None
    return None


# ---------------------------------------------------------------- live sessions

def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def real_start_utc(pid):
    """Process start as UTC asctime text (the format Claude Code writes to procStart)."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             env={**os.environ, "LC_ALL": "C"}, timeout=5).stdout.strip()
        t = time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
        return time.asctime(time.gmtime(t))
    except Exception:
        return None


def same_start(proc_start, real):
    """procStart vs ps start, tolerating the 1 s rounding difference between the two."""
    if not proc_start or not real:
        return False
    try:
        a = time.mktime(time.strptime(" ".join(proc_start.split()), "%a %b %d %H:%M:%S %Y"))
        b = time.mktime(time.strptime(" ".join(real.split()), "%a %b %d %H:%M:%S %Y"))
        return abs(a - b) <= 2
    except ValueError:
        return False


def is_live(pid, proc_start):
    """The pid is alive and, when the file has a procStart, it matches the process start
    (a mismatch means the pid was reused). A file with no procStart is kept on the pid alone."""
    if not pid_alive(pid):
        return False
    return not proc_start or same_start(proc_start, real_start_utc(pid))


def read_live(sessions_dir, check=True, failures=None, alive=is_live):
    """Live sessions. check=False skips the liveness test (for fixtures); alive(pid, procStart) is
    the test. Files that fail to parse are appended to failures when a list is given."""
    out = []
    for path in sorted(glob.glob(os.path.join(sessions_dir, "*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            if failures is not None:
                failures.append(path)
            continue
        pid = d.get("pid")
        if check and not alive(pid, d.get("procStart")):
            continue
        wf = d.get("waitingFor")
        out.append({
            "id": d.get("sessionId"),
            "host_session_id": d.get("hostSessionId"),
            "pid": pid,
            "cwd": d.get("cwd"),
            "title": d.get("name"),
            "status": d.get("status") if d.get("status") in ("waiting", "idle", "busy") else "unknown",
            "waiting_for": wf if isinstance(wf, str) else (json.dumps(wf) if wf else None),
            "entrypoint": d.get("entrypoint"),
            "started": to_ms(d.get("startedAt")),
            "status_since": to_ms(d.get("statusUpdatedAt")) or to_ms(d.get("startedAt")),
        })
    return out


def join_live(live, desktop, projects_root):
    """Adds the desktop record (matched on hostSessionId) and the project to each live session."""
    by_host = {m["desktop_id"]: m for m in desktop.values() if m.get("desktop_id")}
    for lv in live:
        meta = by_host.get(lv.get("host_session_id")) or {}
        lv.update({
            "instance": meta.get("instance"),
            "account": meta.get("account"),
            "kind": meta.get("kind") or "cli",
            "owner": owner_of(meta),
            "project": project_of(meta.get("cwd") or lv.get("cwd"), projects_root),
            "title": meta.get("title") or lv.get("title"),
            "archived": meta.get("archived", False),
            "dormant": False,
            # A live session with no index row yet (no turns) has nothing to report.
            "outcome": "ok", "error_kind": None, "api_status": None, "resets_at": None,
        })
    return live


LIVE_KEYS = ("status", "status_since", "waiting_for", "pid", "started", "entrypoint")


def _lay_over(row, lv):
    row.update({k: lv.get(k) for k in LIVE_KEYS})
    row["title"] = lv.get("title") or row.get("title")
    row["dormant"] = False
    if row.get("_error_newest"):
        row.update(status="error", status_since=row.get("_error_at") or lv.get("status_since"), waiting_for=None)
    elif lv.get("status") == "idle" and lv.get("started"):
        # Subagents: idle in the live file while a subagent this process launched is still running
        # is busy, from the earliest such launch; once the last one finishes, idle from then on.
        running = [ms for ms in (row.get("_agents") or {}).values() if ms and ms >= lv["started"] - 1000]
        done = row.get("_agents_done_at")
        if running:
            row.update(status="busy", status_since=min(running))
        elif done and done > (row.get("status_since") or 0):
            row["status_since"] = done
    return row


def _same_session(row, lv):
    """A live process is the index row's session unless the row is a desktop record the process
    isn't running under (a desktop transcript resumed from a terminal has its own process)."""
    return row.get("kind") == "cli" or row.get("host_session_id") == lv.get("host_session_id")


def overlay_live(rows, live):
    """Marks the index rows that have a live process: their live status, and not dormant. A live
    session whose newest transcript entry is an API error has status error from that entry on. An
    idle one with a subagent still running (see TranscriptScan) is busy."""
    for lv in live:
        row = rows.get(lv.get("id"))
        if row and _same_session(row, lv):
            _lay_over(row, lv)
    return rows


def live_rows(rows, live):
    """Full rows for the live sessions: the index row with the live status laid over it (see
    overlay_live), or the live row itself when the session has no index row yet. A desktop
    transcript resumed outside the desktop app keeps the transcript's facts but not the record's."""
    overlay_live(rows, live)
    out = []
    for lv in live:
        row = rows.get(lv.get("id"))
        if not row:
            out.append(lv)
        elif _same_session(row, lv):
            out.append(row)
        else:
            r = dict(row, summary=None, needs_user=None, prs=[], _record=None)
            r.update({k: lv.get(k) for k in ("kind", "instance", "account", "owner", "host_session_id",
                                             "project", "archived")})
            out.append(_lay_over(r, lv))
    return out


def public(row):
    """A row without the reader's private keys (those starting with _)."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ---------------------------------------------------------------- desktop index

def discover_data_dirs(app_support=None):
    """Every Claude* desktop data dir that has claude-code-sessions."""
    return sorted(d for d in glob.glob(os.path.join(app_support or APP_SUPPORT, "Claude*"))
                  if os.path.isdir(os.path.join(d, "claude-code-sessions")))


def data_dirs(cfg):
    return cfg["app_dirs"] if cfg.get("app_dirs") is not None else discover_data_dirs()


def owner_of(meta):
    """The owner a desktop record implies before any process is looked at: its data dir, pid
    unknown (see ccsession.owner). None for a session with no desktop record."""
    return {"data_dir": meta["_data_dir"], "pid": None} if meta.get("_data_dir") else None


def read_desktop_index(dirs, failures=None, cache=None):
    """cliSessionId -> desktop metadata, for every data dir's Code and Cowork sessions.
    instance is the data dir's name; account is the account-id folder under it. The same
    account id can appear under two instances. Records that fail to parse are appended to
    failures when a list is given. cache (a dict kept between calls) skips re-reading records
    whose mtime and size are unchanged."""
    idx = {}
    for base in dirs:
        base = os.path.normpath(base)
        instance = os.path.basename(base)
        for kind, sub in (("code", "claude-code-sessions"), ("cowork", "local-agent-mode-sessions")):
            for f in glob.glob(os.path.join(base, sub, "*", "*", "local_*.json")):
                if cache is not None:
                    try:
                        st = os.stat(f)
                    except OSError:
                        continue
                    sig = (st.st_mtime, st.st_size)
                    hit = cache.get(f)
                    if hit and hit[0] == sig:
                        if hit[1]:
                            idx[hit[1][0]] = hit[1][1]
                        continue
                try:
                    with open(f) as fh:
                        j = json.load(fh)
                except Exception:
                    if failures is not None:
                        failures.append(f)
                    continue
                cli = j.get("cliSessionId")
                if not cli:
                    if cache is not None:
                        cache[f] = (sig, None)
                    continue
                pts = j.get("postTurnSummary")
                cwd = j.get("originCwd") or j.get("cwd")
                if kind == "cowork":
                    folders = j.get("userSelectedFolders") or []
                    cwd = folders[0] if folders else None
                prs = []
                for p in j.get("prs") or []:
                    if isinstance(p, dict):
                        prs.append({"number": p.get("prNumber"), "url": p.get("url"), "repo": p.get("repo"),
                                    "state": p.get("state"), "branch": p.get("branch")})
                idx[cli] = {
                    "instance": instance,
                    "account": os.path.relpath(f, os.path.join(base, sub)).split(os.sep)[0],
                    "kind": kind,
                    "desktop_id": j.get("sessionId"),
                    "cwd": cwd,
                    "title": j.get("title"),
                    "summary": pts.get("status_detail") if isinstance(pts, dict) else None,
                    "needs_user": pts.get("needs_user") if isinstance(pts, dict) else None,
                    "prs": prs,
                    "branches": j.get("writtenBranches") or [],
                    "archived": bool(j.get("isArchived")),
                    "created": to_ms(j.get("createdAt")),
                    "last": to_ms(j.get("lastActivityAt")),
                    "turns": to_ms(j.get("completedTurns")),
                    "initial": j.get("initialMessage"),
                    "_mtime": os.path.getmtime(f),
                    "_cowork_dir": os.path.splitext(f)[0] if kind == "cowork" else None,
                    "_data_dir": base,
                    "_path": f,
                }
                if cache is not None:
                    cache[f] = (sig, (cli, idx[cli]))
    return idx


# ---------------------------------------------------------------- transcripts

def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


INTERRUPTED = "[Request interrupted by user"  # also "... for tool use]"


def api_error(e):
    """The error facts of an API error entry (isApiErrorMessage), else None. resets_at comes from
    quotaLimits.resetsAt (epoch seconds) when the error names one."""
    if not e.get("isApiErrorMessage"):
        return None
    quota = e.get("quotaLimits") if isinstance(e.get("quotaLimits"), dict) else {}
    resets = quota.get("resetsAt")
    status = e.get("apiErrorStatus")
    return {"error_kind": e.get("error") if isinstance(e.get("error"), str) else None,
            "api_status": status if isinstance(status, int) and 100 <= status <= 599 else None,
            "resets_at": int(resets * 1000) if isinstance(resets, (int, float)) and resets > 0 else None,
            "at": iso_ms(e.get("timestamp"))}


AGENT_TOOLS = ("Agent", "Task")  # Task is the tool's older name
NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.S)
TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
TASK_STATUS = re.compile(r"<status>([a-z_]+)</status>")


def notifications(e):
    """The <task-notification> blocks a main-thread entry carries: in a queue-operation enqueue
    (always written when a background task stops), a user prompt (when the notification starts a
    turn) or a queued_command attachment (when it is absorbed mid-turn). The same notification is
    often written two or three times; other entry types (prompt snapshots, queue removals) repeat
    it too and are not read."""
    t = e.get("type")
    if t == "queue-operation" and e.get("operation") == "enqueue":
        text = e.get("content")
    elif t == "user":
        text = _text((e.get("message") or {}).get("content"))
    elif t == "attachment" and (e.get("attachment") or {}).get("type") == "queued_command":
        text = e["attachment"].get("prompt")
    else:
        return []
    return NOTIFICATION.findall(text) if isinstance(text, str) else []


def outcome_of(err, err_at, interrupt_at, newest_at):
    """(outcome, error facts or None, error is the newest entry). err is the last assistant entry's
    API error (None when that entry succeeded); err_at, interrupt_at and newest_at are main-thread
    positions: of that entry, of the last user entry when it was an interrupt, of the newest user or
    assistant entry."""
    if err and (interrupt_at is None or err_at > interrupt_at):
        return "error", err, err_at == newest_at
    if interrupt_at is not None:
        return "interrupted", None, False
    return "ok", None, False


class TranscriptScan:
    """A resumable pass over one transcript .jsonl: read() picks up where the last read stopped, so a
    growing transcript is not re-read from the start. A partial last line is left for the next read
    unless final=True."""

    def __init__(self, path, projects_root):
        self.path = path
        self.root_rx = re.compile(re.escape(projects_root.rstrip("/")) + r"/([A-Za-z0-9._-]+)")
        self.offset = self.n = 0
        self.ino = None
        self.first = self.last_user = self.last_asst = self.title = None
        self.cwd = self.branch = None
        self.t0 = self.t1 = None
        self.turns = 0
        self.mentions = {}
        # main-thread positions for the outcome: see outcome_of
        self.err = self.err_at = self.interrupt_at = self.newest_at = None
        # subagents: see _agent_result and _notified
        self.calls = {}         # Agent/TaskStop tool_use id -> (tool, launch ms, task id) until its result
        self.agents = {}        # background agent id -> launch ms until a final notification
        self.agents_done_at = None
        self.ended = {}         # task id -> ms of a final notification for an agent not (yet) started
        self.seen = set()       # entries (uuid) and notifications (hash) already read: see _first_copy

    def stale(self, st):
        """The file was replaced or truncated since the last read: start over."""
        return self.ino is not None and (st.st_ino != self.ino or st.st_size < self.offset)

    def read(self, final=False):
        with open(self.path, "rb") as f:
            self.ino = os.fstat(f.fileno()).st_ino
            f.seek(self.offset)
            for raw in f:
                if not raw.endswith(b"\n") and not final:
                    break
                self.offset += len(raw)
                self.feed(raw.decode("utf-8", "replace"))
        return self

    def feed(self, line):
        n = self.n
        self.n += 1
        try:
            e = json.loads(line)
        except ValueError:
            return
        if not isinstance(e, dict):
            return
        t = e.get("type")
        if t == "custom-title":
            self.title = e.get("customTitle") or self.title
        elif t == "ai-title" and not self.title:
            self.title = e.get("aiTitle")
        elif t == "summary" and not self.title:
            self.title = e.get("summary")
        ts = e.get("timestamp")
        if ts:
            self.t0 = self.t0 or ts
            self.t1 = ts
        self.cwd = self.cwd or e.get("cwd")
        gb = e.get("gitBranch")
        if gb and gb != "HEAD":
            self.branch = gb
        if e.get("isSidechain"):
            return
        if "<task-notification>" in line:
            self._notified(e)
        if e.get("isMeta"):
            return
        msg = e.get("message") or {}
        if t == "user":
            self._agent_result(e, msg.get("content"))
            self.newest_at = n
            txt = _text(msg.get("content")).strip()
            if txt.startswith(INTERRUPTED):
                self.interrupt_at = n
            elif txt and not txt.startswith("<"):
                self.turns += 1
                self.first = self.first or txt
                self.last_user = txt
                self.interrupt_at = None
        elif t == "assistant":
            self.newest_at = n
            self.err, self.err_at = api_error(e), n
            if self.err:
                return
            content = msg.get("content")
            txt = _text(content).strip()
            if txt:
                self.last_asst = txt
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        for hit in self.root_rx.findall(json.dumps(b.get("input", {}))):
                            self.mentions[hit] = self.mentions.get(hit, 0) + 1
                        if b.get("name") in AGENT_TOOLS + ("TaskStop",) and b.get("id") and self._first_copy(e):
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            self.calls[b["id"]] = (b["name"], iso_ms(ts), inp.get("task_id") or inp.get("shell_id"))

    def _first_copy(self, e, key=None):
        """False for an entry (by uuid, or key) already read. A transcript can hold the same entries
        twice (the same uuid further down the file), and every notification is written two or three
        times; a second copy must not launch or stop an agent again."""
        key = key if key is not None else e.get("uuid")
        if key is None:
            return True
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def _agent_result(self, e, content):
        """Subagent launches and stops from tool results. An Agent call is running in the foreground
        until its result; a result with status async_launched starts a background agent (agentId),
        which runs until a final notification (see _notified). A SendMessage that resumes an agent
        (resumedAgentId) starts it again; a TaskStop result stops one."""
        if not isinstance(content, list):
            return
        tur = e.get("toolUseResult") if isinstance(e.get("toolUseResult"), dict) else {}
        at = iso_ms(e.get("timestamp"))
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            tool, launched, task = self.calls.pop(b.get("tool_use_id"), (None, None, None))
            if tool in AGENT_TOOLS:
                if tur.get("status") == "async_launched" and isinstance(tur.get("agentId"), str):
                    self._start(tur["agentId"], launched or at)
                else:
                    self.agents_done_at = at or self.agents_done_at
            elif tool == "TaskStop" and not b.get("is_error") and self.agents.pop(task, None):
                self.agents_done_at = at or self.agents_done_at
        if isinstance(tur.get("resumedAgentId"), str) and self._first_copy(e):
            self._start(tur["resumedAgentId"], at)

    def _notified(self, e):
        """A background agent stops at the first notification for its id with a final status (any
        but running); one notification can name several ids. A copy of a notification already read
        is skipped, so a late copy of an earlier run's notification doesn't stop a resumed agent."""
        for block in notifications(e):
            status = TASK_STATUS.search(block)
            if not self._first_copy(e, hash(block)) or not status or status.group(1) == "running":
                continue
            at = iso_ms(e.get("timestamp"))
            for task in TASK_ID.findall(block):
                if self.agents.pop(task.strip(), None) is not None:
                    self.agents_done_at = at or self.agents_done_at
                else:
                    self.ended[task.strip()] = at

    def _start(self, agent, launched):
        """A background agent starts, unless its notification came first: with many agents launched
        in one message, a quick one can finish before its launch result is written."""
        if agent in self.ended:
            self.agents_done_at = max(filter(None, [self.agents_done_at, self.ended.pop(agent)]), default=None)
        else:
            self.agents[agent] = launched

    def running_agents(self):
        """{id: launch ms} of the subagents still running: foreground Agent calls without a result
        (by tool_use id) and background agents without a final notification (by agent id)."""
        out = {tid: ms for tid, (tool, ms, _) in self.calls.items() if tool in AGENT_TOOLS}
        out.update(self.agents)
        return out

    def facts(self):
        outcome, error, error_newest = outcome_of(self.err, self.err_at, self.interrupt_at, self.newest_at)
        return {
            "id": os.path.splitext(os.path.basename(self.path))[0],
            "cwd": self.cwd, "branch": self.branch, "title": self.title,
            "first_prompt": self.first, "last_prompt": self.last_user, "last_reply": self.last_asst,
            "created": iso_ms(self.t0), "last": iso_ms(self.t1), "turns": self.turns,
            "mentions": dict(self.mentions),
            "outcome": outcome, "error": error, "error_newest": error_newest,
            "agents": self.running_agents(), "agents_done_at": self.agents_done_at,
        }


def scan_transcript(path, projects_root):
    """One pass over a transcript .jsonl. Returns the raw facts."""
    return TranscriptScan(path, projects_root).read(final=True).facts()


def transcript_paths(cfg, desktop):
    paths = glob.glob(os.path.join(cfg["claude_dir"], "projects", "*", "*.jsonl"))
    for meta in desktop.values():
        if meta.get("_cowork_dir"):
            paths += glob.glob(os.path.join(meta["_cowork_dir"], ".claude", "projects", "*", "*.jsonl"))
    return paths


# ---------------------------------------------------------------- session rows

def attribute_project(scan, cwd, cfg):
    """(project, source). Direct cwd wins; a session started at the Projects root (or home) is
    attributed to the repo its tool calls touched most, if at least min_path_mentions times."""
    proj = project_of(cwd, cfg["projects_root"])
    if proj:
        return proj, "cwd"
    if scan and scan["mentions"]:
        dirs = {p: n for p, n in scan["mentions"].items()
                if os.path.isdir(os.path.join(cfg["projects_root"], p))}
        if dirs:
            best, n = max(dirs.items(), key=lambda kv: kv[1])
            if n >= cfg["min_path_mentions"]:
                return best, "mentions"
    return None, "none"


def build_session(scan, meta, cfg):
    """One index row from a transcript scan and its desktop record (either may be missing)."""
    meta = meta or {}
    s = scan or {}
    cwd = meta.get("cwd") or s.get("cwd")
    project, psrc = attribute_project(scan, cwd, cfg)
    title = meta.get("title") or s.get("title")
    err = s.get("error") or {}
    return {
        "id": s.get("id") or meta.get("cli_id"),
        "instance": meta.get("instance"),
        "account": meta.get("account"),
        "kind": meta.get("kind") or "cli",
        "project": project,
        "project_source": psrc,
        "cwd": cwd,
        "branch": s.get("branch") or ((meta.get("branches") or [None])[-1]),
        "title": clip(title, 120) if title else None,
        "summary": meta.get("summary"),
        "needs_user": meta.get("needs_user"),
        "first_prompt": clip(s.get("first_prompt") or meta.get("initial")),
        "last_prompt": clip(s.get("last_prompt")),
        "last_reply": clip(s.get("last_reply")),
        "created": meta.get("created") or s.get("created"),
        "last": max(filter(None, [meta.get("last"), s.get("last")]), default=None),
        "turns": s.get("turns") or meta.get("turns") or 0,
        "archived": meta.get("archived", False),
        "prs": meta.get("prs") or [],
        "host_session_id": meta.get("desktop_id"),
        "owner": owner_of(meta),
        # Index rows assume no live process; overlay_live marks the live ones.
        "status": None,
        "status_since": None,
        "dormant": bool(meta.get("kind")),
        "outcome": s.get("outcome") or "ok",
        "error_kind": err.get("error_kind"),
        "api_status": err.get("api_status"),
        "resets_at": err.get("resets_at"),
        # Private (public() drops them): what overlay_live, archive and the transcript route need.
        "_error_newest": bool(s.get("error_newest")),
        "_error_at": err.get("at"),
        "_agents": s.get("agents") or {},
        "_agents_done_at": s.get("agents_done_at"),
        "_transcript": s.get("_path"),
        "_record": meta.get("_path"),
    }


def scan_index(cfg, files=None, full=False, desktop=None, scans=None, only=None):
    """Returns (sessions, files, stats). sessions maps id -> row for every transcript or desktop
    record that changed since `files` (the file signatures a previous call returned), or for all
    of them when full=True. Pass the returned `files` to the next call. scans (a dict kept between
    calls) holds a TranscriptScan per transcript, so a changed transcript is read from where the
    last call stopped. only (a set of session ids) skips every other transcript, leaving it out of
    the returned files so the next call reads it."""
    files = files or {}
    if desktop is None:
        desktop = read_desktop_index(data_dirs(cfg))
    seen = {}
    rows = {}
    stats = {"transcripts": 0, "scanned": 0, "desktop": len(desktop)}

    for p in transcript_paths(cfg, desktop):
        if only is not None and os.path.splitext(os.path.basename(p))[0] not in only:
            continue
        stats["transcripts"] += 1
        try:
            st = os.stat(p)
        except OSError:
            continue
        sid = os.path.splitext(os.path.basename(p))[0]
        meta = desktop.get(sid)
        sig = [int(st.st_mtime), st.st_size, meta["_mtime"] if meta else 0]
        seen[p] = sig
        if not full and files.get(p) == sig:
            continue
        if scans is None:
            scan = scan_transcript(p, cfg["projects_root"])
        else:
            ts = scans.get(p)
            if ts is None or ts.stale(st):
                ts = scans[p] = TranscriptScan(p, cfg["projects_root"])
            try:
                scan = ts.read().facts()
            except OSError:
                continue
        scan["_path"] = p
        stats["scanned"] += 1
        if scan.get("turns") or meta:
            rows[sid] = build_session(scan, meta, cfg)

    # Desktop records whose transcript is gone still deserve a row (title, dates, summary).
    have = {os.path.splitext(os.path.basename(p))[0] for p in seen}
    for cli, meta in desktop.items():
        if cli in have:
            continue
        key = "desktop:" + cli
        sig = [meta["_mtime"]]
        seen[key] = sig
        if not full and files.get(key) == sig:
            continue
        rows[cli] = build_session({"id": cli, "mentions": {}, "turns": meta.get("turns") or 0},
                                  dict(meta, cli_id=cli), cfg)
    return rows, seen, stats


def read_all(cfg, check_live=True, procs=None):
    """(live, sessions, stats): a full read of this machine's sessions, as Session v1 rows. procs
    is a process table for owner resolution (ccsession.owner.process_table() when None)."""
    from . import owner
    desktop = read_desktop_index(data_dirs(cfg))
    rows, _, stats = scan_index(cfg, full=True, desktop=desktop)
    live = live_rows(rows, join_live(read_live(os.path.join(cfg["claude_dir"], "sessions"), check=check_live),
                                     desktop, cfg["projects_root"]))
    owner.resolve(list(rows.values()) + live, owner.process_table() if procs is None else procs)
    return [public(r) for r in live], [public(r) for r in rows.values()], stats


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--json", action="store_true", help="print live sessions and the index as JSON")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    live, sessions, stats = read_all(cfg)
    if args.json:
        json.dump({"live": live, "sessions": sessions}, sys.stdout, indent=1)
        print()
        return 0
    from collections import Counter
    print(f"{stats['transcripts']} transcripts, {stats['desktop']} desktop records, "
          f"{len(sessions)} sessions, {len(live)} live")
    print("  by instance/kind:", dict(Counter(f"{r['instance'] or '-'}/{r['kind']}" for r in sessions)))
    top = Counter(r["project"] or "(none)" for r in sessions).most_common(15)
    print("  top projects:", ", ".join(f"{p} {n}" for p, n in top))
    for lv in live:
        print(f"  live {lv['status']:8} {lv.get('project') or '-':18} {lv.get('title') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

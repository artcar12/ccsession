#!/usr/bin/env python3
"""claude-dash collector agent.

Reads this machine's Claude Code state and turns it into normalized session rows:

  live      ~/.claude/sessions/<pid>.json        sessions running right now (status, waitingFor)
  index     ~/.claude/projects/*/*.jsonl         every transcript, scanned incrementally by mtime
  desktop   <app support>/claude-code-sessions   desktop-app index per account (title, summary, PRs)
  cowork    <app support>/local-agent-mode-sessions

Rows are classed work/personal by project, and work rows are cut down to an allow-list of
metadata fields before anything leaves the machine (see WORK_FIELDS).

  ccdash_agent.py --dry-run           print counts of what would be sent
  ccdash_agent.py --dry-run --json    print the full payload
  ccdash_agent.py --full              resend every session, not just changed ones
  ccdash_agent.py                     push changed rows to the server over SSH

Standard library only, so it runs unchanged on macOS and Linux.
"""
import argparse
import calendar
import glob
import gzip
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
VERSION = 1

DEFAULT_CONFIG = {
    "machine": None,  # defaults to the short hostname
    "claude_dir": os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"),
    "projects_root": os.path.join(HOME, "Projects"),
    # Desktop-app data dirs by account label. Missing dirs (e.g. on Linux) are skipped.
    "app_dirs": {
        "work": os.path.join(HOME, "Library/Application Support/Claude"),
        "personal": os.path.join(HOME, "Library/Application Support/Claude-personal"),
    },
    # Account label for sessions with no desktop-index entry (plain CLI runs).
    "cli_account": "cli",
    # Class for sessions with no project, by account label. Anything unlisted -> default_class.
    "account_class": {"work": "work", "personal": "personal"},
    "default_class": "personal",
    "work_repos": ["acme-api", "acme-web"],
    "ticket_regexes": [r"\bACME-\d+\b",
                       r"\bHOA-\d+\b", r"\bVIS-\d+\b", r"\bDASH-\d+\b"],
    # First prompts that mark a scripted probe session, not real work.
    "probe_prefixes": ["Reply with exactly one word", "List every skill or slash command",
                       "Output only the count of available skills"],
    "min_path_mentions": 3,
    # Per-session class overrides set on this machine: {session_id: "work" | "personal"}. Only
    # these can move a session to personal; the server can only move one to work.
    "local_overrides": {},
    "server": {"ssh": None, "command": "ingest", "timeout": 20},
    "state_dir": os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"), "ccdash"),
}

# The only keys a work row may carry off this machine. Prompts, replies and summaries are excluded.
WORK_FIELDS = {
    "id", "machine", "account", "class", "class_source", "project", "branch", "tickets", "title",
    "created", "last", "turns", "archived", "kind", "prs", "host_session_id", "desktop_id",
}
WORK_PR_FIELDS = {"number", "url", "repo", "state", "branch"}
WORK_LIVE_FIELDS = {"id", "machine", "account", "class", "project", "branch", "title", "status",
                    "waiting_for", "entrypoint", "host_session_id", "pid", "started", "status_updated"}

SNIPPET = 280  # max characters kept from a prompt or reply


# ---------------------------------------------------------------- helpers

def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = path or os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                                "ccdash", "config.json")
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    cfg["machine"] = cfg["machine"] or socket.gethostname().split(".")[0]
    for k in ("claude_dir", "projects_root", "state_dir"):
        cfg[k] = os.path.expanduser(cfg[k])
    cfg["app_dirs"] = {a: os.path.expanduser(p) for a, p in cfg["app_dirs"].items()}
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


def find_tickets(texts, regexes):
    found = []
    for t in texts:
        for rx in regexes:
            for m in re.findall(rx, t or ""):
                if m not in found:
                    found.append(m)
    return found[:12]


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


def read_live(sessions_dir, check=True):
    """Live sessions. check=False skips the pid/procStart test (for fixtures)."""
    out = []
    for path in sorted(glob.glob(os.path.join(sessions_dir, "*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        pid = d.get("pid")
        if check and not (pid_alive(pid) and same_start(d.get("procStart"), real_start_utc(pid))):
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
            "status_updated": to_ms(d.get("statusUpdatedAt")),
        })
    return out


# ---------------------------------------------------------------- desktop index

def read_desktop_index(app_dirs):
    """cliSessionId -> desktop metadata, for every account's Code and Cowork sessions."""
    idx = {}
    for account, base in app_dirs.items():
        for kind, sub in (("code", "claude-code-sessions"), ("cowork", "local-agent-mode-sessions")):
            for f in glob.glob(os.path.join(base, sub, "*", "*", "local_*.json")):
                try:
                    with open(f) as fh:
                        j = json.load(fh)
                except Exception:
                    continue
                cli = j.get("cliSessionId")
                if not cli:
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
                    "account": account,
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
                }
    return idx


# ---------------------------------------------------------------- transcripts

def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def scan_transcript(path, projects_root):
    """One pass over a transcript .jsonl. Returns the raw facts; classification happens later."""
    first = last_user = last_asst = title = None
    cwd = branch = None
    t0 = t1 = None
    turns = 0
    mentions = {}
    root_rx = re.compile(re.escape(projects_root.rstrip("/")) + r"/([A-Za-z0-9._-]+)")
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "custom-title":
                title = e.get("customTitle") or title
            elif t == "ai-title" and not title:
                title = e.get("aiTitle")
            elif t == "summary" and not title:
                title = e.get("summary")
            ts = e.get("timestamp")
            if ts:
                t0 = t0 or ts
                t1 = ts
            cwd = cwd or e.get("cwd")
            gb = e.get("gitBranch")
            if gb and gb != "HEAD":
                branch = gb
            if e.get("isSidechain") or e.get("isMeta"):
                continue
            msg = e.get("message") or {}
            if t == "user":
                txt = _text(msg.get("content")).strip()
                if txt and not txt.startswith("<"):
                    turns += 1
                    first = first or txt
                    last_user = txt
            elif t == "assistant":
                content = msg.get("content")
                txt = _text(content).strip()
                if txt:
                    last_asst = txt
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            for hit in root_rx.findall(json.dumps(b.get("input", {}))):
                                mentions[hit] = mentions.get(hit, 0) + 1
    return {
        "id": os.path.splitext(os.path.basename(path))[0],
        "cwd": cwd, "branch": branch, "title": title,
        "first_prompt": first, "last_prompt": last_user, "last_reply": last_asst,
        "created": iso_ms(t0), "last": iso_ms(t1), "turns": turns, "mentions": mentions,
    }


def transcript_paths(cfg, desktop):
    paths = glob.glob(os.path.join(cfg["claude_dir"], "projects", "*", "*.jsonl"))
    for meta in desktop.values():
        if meta.get("_cowork_dir"):
            paths += glob.glob(os.path.join(meta["_cowork_dir"], ".claude", "projects", "*", "*.jsonl"))
    return paths


# ---------------------------------------------------------------- build rows

def is_probe(text, prefixes):
    text = (text or "").lstrip()
    return any(text.startswith(p) for p in prefixes)


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


def classify(project, account, cfg):
    if project and project in cfg["work_repos"]:
        return "work", "project"
    if project:
        return "personal", "project"
    return cfg["account_class"].get(account, cfg["default_class"]), "account"


def build_row(scan, meta, cfg):
    meta = meta or {}
    cwd = meta.get("cwd") or (scan or {}).get("cwd")
    project, psrc = attribute_project(scan, cwd, cfg)
    account = meta.get("account") or cfg["cli_account"]
    cls, csrc = classify(project, account, cfg)
    s = scan or {}
    title = meta.get("title") or s.get("title")
    branch = s.get("branch") or ((meta.get("branches") or [None])[-1])
    first = s.get("first_prompt") or meta.get("initial")
    row = {
        "id": s.get("id") or meta.get("cli_id"),
        "machine": cfg["machine"],
        "account": account,
        "kind": meta.get("kind") or "cli",
        "class": cls,
        "class_source": csrc,
        "project": project,
        "project_source": psrc,
        "cwd": cwd,
        "branch": branch,
        "title": clip(title, 120) if title else None,
        "summary": meta.get("summary"),
        "needs_user": meta.get("needs_user"),
        "first_prompt": clip(first),
        "last_prompt": clip(s.get("last_prompt")),
        "last_reply": clip(s.get("last_reply")),
        "created": meta.get("created") or s.get("created"),
        "last": max(filter(None, [meta.get("last"), s.get("last")]), default=None),
        "turns": s.get("turns") or meta.get("turns") or 0,
        "archived": meta.get("archived", False),
        "prs": meta.get("prs") or [],
        "host_session_id": meta.get("desktop_id"),
        "desktop_id": meta.get("desktop_id"),
    }
    row["tickets"] = find_tickets([row["title"], row["branch"], first, s.get("last_prompt")],
                                  cfg["ticket_regexes"])
    return row


def redact(row):
    """Strip a work row down to WORK_FIELDS. Personal rows pass through, minus local-only keys."""
    if row.get("class") != "work":
        return {k: v for k, v in row.items() if not k.startswith("_")}
    out = {k: v for k, v in row.items() if k in WORK_FIELDS}
    out["prs"] = [{k: v for k, v in p.items() if k in WORK_PR_FIELDS} for p in row.get("prs") or []]
    return out


def redact_live(row):
    if row.get("class") != "work":
        return row
    return {k: v for k, v in row.items() if k in WORK_LIVE_FIELDS}


# ---------------------------------------------------------------- collect

def load_state(cfg):
    try:
        with open(os.path.join(cfg["state_dir"], "state.json")) as f:
            return json.load(f)
    except Exception:
        return {"files": {}, "last_hash": None, "last_sent": 0}


def save_state(cfg, state):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    tmp = os.path.join(cfg["state_dir"], "state.json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, os.path.join(cfg["state_dir"], "state.json"))


def effective_overrides(cfg, server_overrides):
    """Server overrides only ever tighten (to work): a compromised or mistaken server must not be
    able to make the agent upload a work session's prompts. Loosening is local config only."""
    eff = {k: v for k, v in (server_overrides or {}).items() if v == "work"}
    eff.update({k: v for k, v in (cfg.get("local_overrides") or {}).items() if v in ("work", "personal")})
    return eff


def apply_override(row, overrides):
    cls = (overrides or {}).get(row.get("id"))
    if cls in ("work", "personal") and cls != row.get("class"):
        row["class"], row["class_source"] = cls, "override"
    return row


def collect(cfg, state=None, full=False, check_live=True):
    """Returns (payload, new_file_state, stats). Only sessions whose transcript or desktop-index
    entry changed since `state` are included unless full=True."""
    state = state or {"files": {}}
    overrides = effective_overrides(cfg, state.get("overrides"))
    # A session whose override changed since the last push is resent so the server gets the
    # right detail level (re-classed to personal: full row; to work: already stripped server-side).
    changed = set(overrides) ^ set(state.get("sent_overrides") or {}) | {
        k for k, v in overrides.items() if (state.get("sent_overrides") or {}).get(k) != v}
    files_state = {p: sig for p, sig in state["files"].items()
                   if os.path.splitext(os.path.basename(p))[0].removeprefix("desktop:") not in changed}
    state = dict(state, files=files_state)
    desktop = read_desktop_index(cfg["app_dirs"])
    seen_files = {}
    rows = {}
    stats = {"transcripts": 0, "scanned": 0, "probes": 0, "desktop": len(desktop)}

    scans = {}
    for p in transcript_paths(cfg, desktop):
        stats["transcripts"] += 1
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig = [int(st.st_mtime), st.st_size]
        seen_files[p] = sig
        sid = os.path.splitext(os.path.basename(p))[0]
        meta = desktop.get(sid)
        meta_sig = int(meta["_mtime"]) if meta else 0
        prev = state["files"].get(p)
        if not full and prev == sig + [meta_sig]:
            continue
        seen_files[p] = sig + [meta_sig]
        scans[sid] = scan_transcript(p, cfg["projects_root"])
        stats["scanned"] += 1

    for sid, sc in scans.items():
        meta = desktop.get(sid)
        if is_probe(sc.get("first_prompt"), cfg["probe_prefixes"]):
            stats["probes"] += 1
            continue
        if not sc.get("turns") and not meta:
            continue
        rows[sid] = build_row(sc, meta, cfg)

    # Desktop entries whose transcript is gone still deserve a row (title, dates, summary).
    have = {os.path.splitext(os.path.basename(p))[0] for p in seen_files}
    for cli, meta in desktop.items():
        if cli in have:
            continue
        key = "desktop:" + cli
        sig = [int(meta["_mtime"])]
        seen_files[key] = sig
        if not full and state["files"].get(key) == sig:
            continue
        m = dict(meta, cli_id=cli)
        rows[cli] = build_row({"id": cli, "mentions": {}, "turns": meta.get("turns") or 0}, m, cfg)

    # Live sessions, enriched with class/project so the dashboard can group them without a join.
    by_id = {}
    live = []
    for lv in read_live(os.path.join(cfg["claude_dir"], "sessions"), check=check_live):
        meta = desktop.get(lv["id"]) or {}
        project, _ = attribute_project(None, meta.get("cwd") or lv["cwd"], cfg)
        account = meta.get("account") or cfg["cli_account"]
        cls, _ = classify(project, account, cfg)
        lv.update({"machine": cfg["machine"], "account": account, "class": cls, "project": project,
                   "title": meta.get("title") or lv["title"]})
        lv.pop("cwd", None)
        live.append(redact_live(apply_override(lv, overrides)))
        by_id[lv["id"]] = lv

    payload = {
        "v": VERSION,
        "machine": cfg["machine"],
        "sent_at": int(time.time() * 1000),
        "full": full,
        "live": live,
        "sessions": [redact(apply_override(r, overrides)) for r in rows.values()],
    }
    stats["rows"] = len(payload["sessions"])
    stats["live"] = len(live)
    return payload, seen_files, stats


# ---------------------------------------------------------------- push

def push(cfg, payload):
    target = cfg["server"].get("ssh")
    if not target:
        raise RuntimeError("server.ssh is not set in config; use --dry-run")
    data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
    if target == "local":  # run the server's ingest directly (dev, or the agent on the Linode itself)
        argv = shlex.split(cfg["server"]["command"])
    else:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, cfg["server"]["command"]]
    r = subprocess.run(argv, input=data, capture_output=True, timeout=cfg["server"]["timeout"])
    if r.returncode != 0:
        raise RuntimeError(f"ingest exited {r.returncode}: {r.stderr.decode(errors='replace').strip()[:300]}")
    return r.stdout.decode(errors="replace").strip()


def summarize(payload, stats):
    from collections import Counter
    s = payload["sessions"]
    print(f"machine {payload['machine']}: {stats['transcripts']} transcripts, {stats['scanned']} scanned, "
          f"{stats['desktop']} desktop entries, {stats['probes']} probes skipped")
    print(f"sessions: {len(s)}  live: {len(payload['live'])}")
    print("  by account:", dict(Counter(r["account"] + "/" + r["kind"] for r in s)))
    print("  by class:  ", dict(Counter(r["class"] for r in s)))
    print("  by class source:", dict(Counter(r["class_source"] for r in s)))
    top = Counter(r["project"] or "(none)" for r in s).most_common(15)
    print("  top projects:", ", ".join(f"{p} {n}" for p, n in top))
    for lv in payload["live"]:
        print(f"  live {lv['status']:8} {lv['class']:8} {lv.get('project') or '-':18} {lv.get('title') or ''}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="with --dry-run, print the payload")
    ap.add_argument("--full", action="store_true", help="send every session, ignoring the change cursor")
    ap.add_argument("--heartbeat", type=int, default=60, help="seconds between pushes when nothing changed")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    state = load_state(cfg)

    payload, files, stats = collect(cfg, state, full=args.full or args.dry_run)
    if args.dry_run:
        if args.json:
            json.dump(payload, sys.stdout, indent=1)
            print()
        else:
            summarize(payload, stats)
        return 0

    digest = hashlib.sha256(json.dumps([payload["live"], payload["sessions"]], sort_keys=True).encode()).hexdigest()
    now = time.time()
    if digest == state.get("last_hash") and now - state.get("last_sent", 0) < args.heartbeat:
        return 0
    try:
        reply = push(cfg, payload)
    except Exception as e:
        print(f"ccdash: push failed, will retry: {e}", file=sys.stderr)
        return 3  # same meaning as bin/board: server unreachable; cursor not advanced
    try:
        overrides = json.loads(reply.splitlines()[-1]).get("overrides") or {}
    except (ValueError, IndexError):
        overrides = state.get("overrides") or {}
    state.update({"files": files, "last_hash": digest, "last_sent": now,
                  "sent_overrides": effective_overrides(cfg, state.get("overrides")), "overrides": overrides})
    save_state(cfg, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

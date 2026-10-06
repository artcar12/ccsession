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


def read_live(sessions_dir, check=True):
    """Live sessions. check=False skips the liveness test (for fixtures)."""
    out = []
    for path in sorted(glob.glob(os.path.join(sessions_dir, "*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        pid = d.get("pid")
        if check and not is_live(pid, d.get("procStart")):
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
            "project": project_of(meta.get("cwd") or lv.get("cwd"), projects_root),
            "title": meta.get("title") or lv.get("title"),
            "archived": meta.get("archived", False),
            "dormant": False,
        })
    return live


def overlay_live(rows, live):
    """Marks the index rows that have a live process: their live status, and not dormant."""
    for lv in live:
        row = rows.get(lv.get("id"))
        if row:
            row.update({k: lv.get(k) for k in ("status", "status_since", "waiting_for", "pid", "started",
                                               "entrypoint")})
            row["dormant"] = False
    return rows


# ---------------------------------------------------------------- desktop index

def discover_data_dirs(app_support=None):
    """Every Claude* desktop data dir that has claude-code-sessions."""
    return sorted(d for d in glob.glob(os.path.join(app_support or APP_SUPPORT, "Claude*"))
                  if os.path.isdir(os.path.join(d, "claude-code-sessions")))


def data_dirs(cfg):
    return cfg["app_dirs"] if cfg.get("app_dirs") is not None else discover_data_dirs()


def read_desktop_index(dirs):
    """cliSessionId -> desktop metadata, for every data dir's Code and Cowork sessions.
    instance is the data dir's name; account is the account-id folder under it. The same
    account id can appear under two instances."""
    idx = {}
    for base in dirs:
        instance = os.path.basename(base.rstrip("/"))
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
    """One pass over a transcript .jsonl. Returns the raw facts."""
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
        # Index rows assume no live process; overlay_live marks the live ones.
        "status": None,
        "status_since": None,
        "dormant": bool(meta.get("kind")),
    }


def scan_index(cfg, files=None, full=False, desktop=None):
    """Returns (sessions, files, stats). sessions maps id -> row for every transcript or desktop
    record that changed since `files` (the file signatures a previous call returned), or for all
    of them when full=True. Pass the returned `files` to the next call."""
    files = files or {}
    if desktop is None:
        desktop = read_desktop_index(data_dirs(cfg))
    seen = {}
    rows = {}
    stats = {"transcripts": 0, "scanned": 0, "desktop": len(desktop)}

    for p in transcript_paths(cfg, desktop):
        stats["transcripts"] += 1
        try:
            st = os.stat(p)
        except OSError:
            continue
        sid = os.path.splitext(os.path.basename(p))[0]
        meta = desktop.get(sid)
        sig = [int(st.st_mtime), st.st_size, int(meta["_mtime"]) if meta else 0]
        seen[p] = sig
        if not full and files.get(p) == sig:
            continue
        scan = scan_transcript(p, cfg["projects_root"])
        stats["scanned"] += 1
        if scan.get("turns") or meta:
            rows[sid] = build_session(scan, meta, cfg)

    # Desktop records whose transcript is gone still deserve a row (title, dates, summary).
    have = {os.path.splitext(os.path.basename(p))[0] for p in seen}
    for cli, meta in desktop.items():
        if cli in have:
            continue
        key = "desktop:" + cli
        sig = [int(meta["_mtime"])]
        seen[key] = sig
        if not full and files.get(key) == sig:
            continue
        rows[cli] = build_session({"id": cli, "mentions": {}, "turns": meta.get("turns") or 0},
                                  dict(meta, cli_id=cli), cfg)
    return rows, seen, stats


def read_all(cfg, check_live=True):
    """(live, sessions, stats): a full read of this machine's sessions."""
    desktop = read_desktop_index(data_dirs(cfg))
    rows, _, stats = scan_index(cfg, full=True, desktop=desktop)
    live = join_live(read_live(os.path.join(cfg["claude_dir"], "sessions"), check=check_live),
                     desktop, cfg["projects_root"])
    return live, list(overlay_live(rows, live).values()), stats


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

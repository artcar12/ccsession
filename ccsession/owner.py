"""Which desktop instance owns a session.

Both desktop accounts run the same Claude.app with different --user-data-dir values, so a session
belongs to a data dir, not to the app. A desktop session's owner is {data_dir, pid}:

  data_dir  the data dir whose claude-code-sessions (or local-agent-mode-sessions) holds its record
  pid       that instance's main process: found by walking the ppid chain up from the live
            session's pid (claude -> disclaimer -> Claude), else the running main process whose
            data dir matches; null when that instance isn't running

The process table is one `ps` call, passed in so tests can supply their own.
"""
import os
import re
import subprocess

from . import reader

MAIN_EXE = "/Contents/MacOS/Claude"  # the desktop app's main process; helpers live under Frameworks/
DATA_DIR_ARG = re.compile(r"--user-data-dir=(.+?)(?= --|$)")


def process_table():
    """{pid: (ppid, args)} for every process, or {} when ps isn't available."""
    try:
        out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "ppid=", "-o", "args="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def default_data_dir():
    return os.path.join(reader.APP_SUPPORT, "Claude")


def main_data_dir(args):
    """The data dir of a desktop main process (no --user-data-dir means the default one), or None
    when args aren't a desktop main process."""
    if not args.split(" --")[0].endswith(MAIN_EXE) or "--type=" in args:
        return None
    m = DATA_DIR_ARG.search(args)
    return os.path.normpath(os.path.expanduser(m.group(1).strip().strip('"'))) if m else default_data_dir()


def instances(procs):
    """{data dir: main pid} for every running desktop instance."""
    out = {}
    for pid, (_, args) in sorted(procs.items()):
        d = main_data_dir(args)
        if d:
            out.setdefault(d, pid)
    return out


def main_ancestor(pid, procs, limit=64):
    """(main pid, data dir) of the desktop main process above pid, or (None, None)."""
    seen = set()
    while pid and pid not in seen and len(seen) < limit:
        seen.add(pid)
        ppid, args = procs.get(pid, (None, ""))
        d = main_data_dir(args)
        if d:
            return pid, d
        pid = ppid
    return None, None


def resolve(rows, procs):
    """Fills owner.pid on every row that has an owner. A live session's ppid chain wins when it
    reaches the instance holding its record; otherwise the record's data dir is matched to a
    running instance."""
    running = instances(procs)
    for row in rows:
        own = row.get("owner")
        if not own:
            continue
        data_dir = os.path.normpath(own["data_dir"])
        pid = None
        if row.get("pid"):
            main, d = main_ancestor(row["pid"], procs)
            if main and d == data_dir:
                pid = main
        row["owner"] = {"data_dir": data_dir, "pid": pid or running.get(data_dir)}
    return rows

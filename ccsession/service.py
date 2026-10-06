"""ccsessiond as a user service: a launchd agent on macOS, a systemd user unit on Linux.

  ccsessiond service install [--dry-run]   write the agent/unit for this ccsessiond and start it
  ccsessiond service uninstall             stop it and remove the file
  ccsessiond service status                installed? loaded? running? which program?

The service runs the ccsessiond this command was started from (an absolute path, e.g. the
~/.local/bin/ccsessiond that `uv tool install` made), so install from the one you want to keep.
"""
import os
import plistlib
import shlex
import shutil
import subprocess
import sys

from . import reader as R

LABEL = "com.github.artcar12.ccsessiond"
UNIT = "ccsessiond.service"


def run(cmd):
    """(returncode, output) of a command; the default runner, replaced in tests."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def program(exe=None):
    """The command line the service runs: this ccsessiond by absolute path (symlinks kept, so a
    reinstall behind ~/.local/bin/ccsessiond keeps working), or python -m when run from a checkout."""
    if exe:
        return [os.path.abspath(os.path.expanduser(exe))]
    argv0 = sys.argv[0] or ""
    if os.path.basename(argv0) == "ccsessiond":
        return [os.path.abspath(shutil.which(argv0) or argv0)]
    found = shutil.which("ccsessiond")
    if found:
        return [os.path.abspath(found)]
    return [sys.executable, "-m", "ccsession.daemon"]


def log_path():
    from .daemon import state_dir
    return os.path.join(state_dir(), "ccsessiond.log")


class Launchd:
    kind = "launchd"

    def __init__(self, home=None, uid=None):
        self.path = os.path.join(home or R.HOME, "Library", "LaunchAgents", LABEL + ".plist")
        self.target = f"gui/{os.getuid() if uid is None else uid}"

    def render(self, argv):
        log = log_path()
        return plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # restarted when it crashes or is killed
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }).decode()

    def program_of(self, text):
        return plistlib.loads(text.encode()).get("ProgramArguments")

    def start(self, runner):
        if runner(["launchctl", "print", f"{self.target}/{LABEL}"])[0] == 0:
            runner(["launchctl", "bootout", f"{self.target}/{LABEL}"])
        return [runner(["launchctl", "bootstrap", self.target, self.path])]

    def stop(self, runner):
        return [runner(["launchctl", "bootout", f"{self.target}/{LABEL}"])]

    def state(self, runner):
        code, out = runner(["launchctl", "print", f"{self.target}/{LABEL}"])
        if code != 0:
            return {"loaded": False, "running": False, "pid": None}
        fields = {}
        for line in out.splitlines():
            k, sep, v = line.strip().partition(" = ")
            if sep and k in ("state", "pid") and k not in fields:
                fields[k] = v.strip()
        pid = fields.get("pid")
        return {"loaded": True, "running": fields.get("state") == "running",
                "pid": int(pid) if pid and pid.isdigit() else None}


class Systemd:
    kind = "systemd"

    def __init__(self, home=None):
        config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home or R.HOME, ".config")
        self.path = os.path.join(config, "systemd", "user", UNIT)

    def render(self, argv):
        return "\n".join([
            "[Unit]",
            "Description=ccsessiond: this machine's Claude Code sessions on a local API",
            "",
            "[Service]",
            "ExecStart=" + " ".join(f'"{a}"' if any(c in a for c in ' "\\') else a for a in argv),
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])

    def program_of(self, text):
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                return shlex.split(line[len("ExecStart="):])
        return None

    def start(self, runner):
        return [runner(["systemctl", "--user", "daemon-reload"]),
                runner(["systemctl", "--user", "enable", "--now", UNIT]),
                runner(["systemctl", "--user", "restart", UNIT])]

    def stop(self, runner):
        return [runner(["systemctl", "--user", "disable", "--now", UNIT])]

    def state(self, runner):
        code, out = runner(["systemctl", "--user", "show", "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID",
                            UNIT])
        fields = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line) if code == 0 else {}
        pid = fields.get("MainPID", "0")
        return {"loaded": fields.get("LoadState") == "loaded", "running": fields.get("ActiveState") == "active",
                "pid": int(pid) if pid.isdigit() and int(pid) > 0 else None}


def manager(platform=None, home=None):
    platform = platform or sys.platform
    if platform == "darwin":
        return Launchd(home)
    if platform.startswith("linux"):
        return Systemd(home)
    return None


def status(svc, runner=run):
    """{kind, path, installed, program, loaded, running, pid}."""
    out = {"kind": svc.kind, "path": svc.path, "installed": os.path.exists(svc.path), "program": None}
    if out["installed"]:
        try:
            with open(svc.path) as f:
                out["program"] = svc.program_of(f.read())
        except Exception:
            pass
    out.update(svc.state(runner))
    return out


def install(svc, argv, runner=run, dry_run=False, out=sys.stdout):
    text = svc.render(argv)
    if dry_run:
        out.write(f"# {svc.path}\n{text}\n")
        return 0
    os.makedirs(os.path.dirname(svc.path), exist_ok=True)
    os.makedirs(os.path.dirname(log_path()), mode=0o700, exist_ok=True)
    with open(svc.path, "w") as f:
        f.write(text)
    failed = [o for c, o in svc.start(runner) if c != 0]
    for o in failed:
        out.write(f"ccsessiond: {o}\n")
    out.write(f"{'failed to start' if failed else 'installed and started'}: {svc.path}\n")
    return 1 if failed else 0


def uninstall(svc, runner=run, out=sys.stdout):
    if svc.state(runner)["loaded"]:
        svc.stop(runner)
    if os.path.exists(svc.path):
        os.unlink(svc.path)
        out.write(f"removed {svc.path}\n")
    else:
        out.write("not installed\n")
    return 0


def main(argv, runner=run, out=sys.stdout):
    import argparse
    ap = argparse.ArgumentParser(prog="ccsessiond service", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["install", "uninstall", "status"])
    ap.add_argument("--dry-run", action="store_true", help="print what install would write")
    ap.add_argument("--exe", help="the ccsessiond to run (default: this one)")
    args = ap.parse_args(argv)
    svc = manager()
    if svc is None:
        out.write(f"ccsessiond: no service manager for {sys.platform}\n")
        return 2
    if args.action == "install":
        return install(svc, program(args.exe), runner, args.dry_run, out)
    if args.action == "uninstall":
        return uninstall(svc, runner, out)
    st = status(svc, runner)
    out.write("".join(f"{k}: {v}\n" for k, v in st.items()))
    return 0 if st["running"] else 1

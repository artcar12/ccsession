"""ccsessiond doctor: a local self-check.

Checks that the config parses, the Claude dirs it reads are there and readable, the token file is
private, the service is installed and running this ccsessiond, the API answers, claude-open is found
(macOS), and lists files that fail to parse. Prints one line per check (ok, warn, fail) and exits 1
when anything failed. --json prints the same checks as JSON.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from . import __version__, service
from . import reader as R

KNOWN_KEYS = set(R.DEFAULT_CONFIG) | {"port", "machine", "token_file", "claude_open"}


class Report:
    def __init__(self):
        self.checks = []

    def add(self, level, name, detail):
        self.checks.append({"level": level, "check": name, "detail": detail})

    def ok(self, name, detail):
        self.add("ok", name, detail)

    def warn(self, name, detail):
        self.add("warn", name, detail)

    def fail(self, name, detail):
        self.add("fail", name, detail)

    @property
    def failed(self):
        return any(c["level"] == "fail" for c in self.checks)


def readable_dir(path):
    return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


def check_config(r, path):
    if not os.path.exists(path):
        r.ok("config", f"{path} not present; defaults in use")
        return R.load_config(path)
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        r.fail("config", f"{path}: {e}")
        return None
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        r.warn("config", f"{path}: unknown keys {', '.join(unknown)}")
    else:
        r.ok("config", path)
    return R.load_config(path)


def check_paths(r, cfg, platform):
    claude = cfg["claude_dir"]
    if not readable_dir(claude):
        r.fail("claude_dir", f"{claude} missing or unreadable")
    else:
        r.ok("claude_dir", claude)
        for sub, level in (("sessions", "warn"), ("projects", "warn")):
            p = os.path.join(claude, sub)
            if readable_dir(p):
                r.ok(sub, p)
            else:
                r.add(level, sub, f"{p} missing or unreadable (no {sub} yet?)")
    root = cfg["projects_root"]
    if readable_dir(root):
        r.ok("projects_root", root)
    else:
        r.warn("projects_root", f"{root} missing; sessions won't be attributed to projects")
    dirs = R.data_dirs(cfg)
    if platform != "darwin" and not dirs:
        r.ok("app_dirs", "no desktop data dirs (the desktop app is macOS only)")
        return
    if not dirs:
        r.warn("app_dirs", "no Claude desktop data dirs found" +
               (" (app_dirs is unset, so Application Support/Claude* was searched)" if cfg["app_dirs"] is None else ""))
    for d in dirs:
        if readable_dir(os.path.join(d, "claude-code-sessions")):
            r.ok("app_dir", d)
        else:
            r.fail("app_dir", f"{d}: claude-code-sessions missing or unreadable")


def check_token(r, path):
    if not os.path.exists(path):
        r.warn("token", f"{path} not created yet (ccsessiond creates it on first start)")
        return None
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        r.fail("token", f"{path} is {oct(mode)}; should be 0o600 (ccsessiond tightens it on start)")
    else:
        r.ok("token", path)
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError as e:
        r.fail("token", f"{path}: {e}")
        return None


def check_service(r, svc, runner):
    if svc is None:
        r.warn("service", f"no service manager for {sys.platform}")
        return None
    st = service.status(svc, runner)
    if not st["installed"]:
        r.warn("service", f"not installed ({svc.path}); run `ccsessiond service install`, unless a program "
                          "starts ccsessiond --stdio itself")
        return st
    if not st["running"]:
        r.fail("service", f"{svc.kind} {'loaded but not running' if st['loaded'] else 'not loaded'}: {svc.path}")
    else:
        r.ok("service", f"{svc.kind} running, pid {st['pid']}")
    prog = (st["program"] or [None])[0]
    if prog and not os.path.exists(prog):
        r.fail("service program", f"{prog} doesn't exist; reinstall the service")
    elif prog:
        r.ok("service program", prog)
    return st


def check_api(r, port, token, st):
    if token is None:
        r.warn("api", "no token, so the API wasn't tried")
        return None
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health",
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            health = json.load(resp)
    except urllib.error.HTTPError as e:
        r.fail("api", f"127.0.0.1:{port} answered {e.code}: the token doesn't match, or another program has the port")
        return None
    except (OSError, ValueError) as e:
        level = "fail" if st and st.get("installed") else "warn"
        r.add(level, "api", f"nothing answers on 127.0.0.1:{port} ({getattr(e, 'reason', e)})")
        return None
    version = health.get("ccsession")
    if version != __version__:
        r.warn("api", f"127.0.0.1:{port} runs ccsession {version}, this is {__version__}; restart the service")
    else:
        r.ok("api", f"127.0.0.1:{port} ccsession {version}, {health.get('schema')}")
    return health


def check_claude_open(r, path, platform):
    if platform != "darwin":
        r.ok("claude-open", "not used off macOS")
    elif not path:
        r.warn("claude-open", "not configured (claude_open in config.json); open fails until it is, unless the "
                              "program starting ccsessiond --stdio passes --claude-open")
    elif os.path.isfile(path) and os.access(path, os.X_OK):
        r.ok("claude-open", path)
    else:
        r.fail("claude-open", f"{path} missing or not executable")


def check_parse(r, cfg, health):
    if health is not None:
        warnings = health.get("warnings") or []
        source = "the running ccsessiond"
    else:
        fails = []
        R.read_live(os.path.join(cfg["claude_dir"], "sessions"), check=False, failures=fails)
        R.read_desktop_index(R.data_dirs(cfg), failures=fails)
        warnings = [{"path": p} for p in fails]
        source = "one read"
    if warnings:
        r.warn("parse", f"{len(warnings)} file(s) failed to parse ({source}): " +
               ", ".join(w["path"] for w in warnings[:5]) + (" …" if len(warnings) > 5 else ""))
    else:
        r.ok("parse", f"no parse failures ({source})")


def doctor(config_path=None, claude_open=None, platform=None, runner=service.run, home=None):
    from .daemon import daemon_config
    platform = platform or sys.platform
    r = Report()
    cfg = check_config(r, config_path or R.config_path())
    if cfg is None:
        return r
    dc = daemon_config(cfg, claude_open)
    check_paths(r, cfg, platform)
    token = check_token(r, dc["token_file"])
    st = check_service(r, service.manager(platform, home), runner)
    health = check_api(r, dc["port"], token, st)
    check_claude_open(r, dc["claude_open"], platform)
    check_parse(r, cfg, health)
    return r


def main(argv, out=sys.stdout):
    import argparse
    ap = argparse.ArgumentParser(prog="ccsessiond doctor", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--claude-open", help="the claude-open to check instead of config's")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = doctor(args.config, args.claude_open)
    if args.json:
        json.dump({"ccsession": __version__, "ok": not r.failed, "checks": r.checks}, out, indent=1)
        out.write("\n")
    else:
        out.write(f"ccsessiond doctor (ccsession {__version__})\n")
        for c in r.checks:
            out.write(f"  {c['level']:4}  {c['check']:15} {c['detail']}\n")
    return 1 if r.failed else 0

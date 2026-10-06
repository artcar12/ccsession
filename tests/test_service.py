"""ccsessiond service (launchd agent / systemd user unit) and ccsessiond doctor, with a fake
launchctl/systemctl and a made-up home."""
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ccsession.daemon as D  # noqa: E402
import ccsession.doctor as Doc  # noqa: E402
import ccsession.reader as R  # noqa: E402
import ccsession.service as S  # noqa: E402

LAUNCHCTL_RUNNING = """gui/501/com.github.artcar12.ccsessiond = {
\tactive count = 1
\tpath = /x/com.github.artcar12.ccsessiond.plist
\tstate = running
\tprogram = /x/ccsessiond
\tpid = 4242
\tendpoints = {
\t\tstate = waiting
\t}
}"""


class FakeRunner:
    """Records commands; answers from a table of (command prefix) -> (code, output)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, cmd):
        self.calls.append(cmd)
        for prefix, answer in self.answers.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return answer
        return 0, ""


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        env = {"XDG_STATE_HOME": os.path.join(self.home, "state"), "XDG_CONFIG_HOME": os.path.join(self.home, "cfg")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class TestLaunchd(TempHome):
    def test_plist(self):
        svc = S.Launchd(self.home, uid=501)
        text = svc.render(["/Users/dev/.local/bin/ccsessiond"])
        p = plistlib.loads(text.encode())
        self.assertEqual((p["Label"], p["ProgramArguments"], p["RunAtLoad"], p["KeepAlive"]),
                         (S.LABEL, ["/Users/dev/.local/bin/ccsessiond"], True, {"SuccessfulExit": False}))
        self.assertEqual(p["StandardErrorPath"], os.path.join(self.home, "state", "ccsession", "ccsessiond.log"))
        self.assertEqual(svc.program_of(text), ["/Users/dev/.local/bin/ccsessiond"])
        if shutil.which("plutil"):
            path = os.path.join(self.home, "x.plist")
            with open(path, "w") as f:
                f.write(text)
            self.assertEqual(subprocess.run(["plutil", "-lint", path], capture_output=True).returncode, 0)

    def test_install_and_uninstall(self):
        svc = S.Launchd(self.home, uid=501)
        run = FakeRunner({("launchctl", "print"): (113, "not found")})
        self.assertEqual(S.install(svc, ["/bin/ccsessiond"], run, out=io.StringIO()), 0)
        self.assertTrue(os.path.exists(svc.path))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "state", "ccsession")))
        self.assertEqual(run.calls[-1], ["launchctl", "bootstrap", "gui/501", svc.path])
        # reinstall over a loaded agent: bootout first
        run = FakeRunner({("launchctl", "print"): (0, LAUNCHCTL_RUNNING)})
        S.install(svc, ["/bin/ccsessiond"], run, out=io.StringIO())
        self.assertEqual([c[1] for c in run.calls], ["print", "bootout", "bootstrap"])
        S.uninstall(svc, run, out=io.StringIO())
        self.assertEqual(run.calls[-1], ["launchctl", "bootout", f"gui/501/{S.LABEL}"])
        self.assertFalse(os.path.exists(svc.path))

    def test_failed_start_is_reported(self):
        svc = S.Launchd(self.home, uid=501)
        run = FakeRunner({("launchctl", "print"): (113, ""), ("launchctl", "bootstrap"): (5, "Input/output error")})
        out = io.StringIO()
        self.assertEqual(S.install(svc, ["/bin/ccsessiond"], run, out=out), 1)
        self.assertIn("Input/output error", out.getvalue())

    def test_status(self):
        svc = S.Launchd(self.home, uid=501)
        st = S.status(svc, FakeRunner({("launchctl", "print"): (0, LAUNCHCTL_RUNNING)}))
        self.assertEqual((st["installed"], st["loaded"], st["running"], st["pid"]), (False, True, True, 4242))
        st = S.status(svc, FakeRunner({("launchctl", "print"): (113, "")}))
        self.assertEqual((st["loaded"], st["running"], st["pid"]), (False, False, None))

    def test_dry_run_writes_nothing(self):
        svc = S.Launchd(self.home, uid=501)
        out = io.StringIO()
        run = FakeRunner()
        S.install(svc, ["/bin/ccsessiond"], run, dry_run=True, out=out)
        self.assertIn(S.LABEL, out.getvalue())
        self.assertEqual((run.calls, os.path.exists(svc.path)), ([], False))


class TestSystemd(TempHome):
    def test_unit(self):
        svc = S.Systemd(self.home)
        self.assertEqual(svc.path, os.path.join(self.home, "cfg", "systemd", "user", "ccsessiond.service"))
        argv = ["/home/dev/my tools/ccsessiond"]
        text = svc.render(argv)
        self.assertIn('ExecStart="/home/dev/my tools/ccsessiond"', text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertEqual(svc.program_of(text), argv)

    def test_install_and_uninstall(self):
        svc = S.Systemd(self.home)
        run = FakeRunner({("systemctl", "--user", "show"): (0, "LoadState=loaded\nActiveState=active\nMainPID=77")})
        S.install(svc, ["/bin/ccsessiond"], run, out=io.StringIO())
        self.assertEqual([c[2:] for c in run.calls], [["daemon-reload"], ["enable", "--now", S.UNIT],
                                                       ["restart", S.UNIT]])
        st = S.status(svc, run)
        self.assertEqual((st["installed"], st["running"], st["pid"], st["program"]), (True, True, 77, ["/bin/ccsessiond"]))
        S.uninstall(svc, run, out=io.StringIO())
        self.assertEqual(run.calls[-1][2:], ["disable", "--now", S.UNIT])
        self.assertFalse(os.path.exists(svc.path))

    def test_manager_by_platform(self):
        self.assertIsInstance(S.manager("darwin", self.home), S.Launchd)
        self.assertIsInstance(S.manager("linux", self.home), S.Systemd)
        self.assertIsNone(S.manager("win32", self.home))


class TestDoctor(TempHome):
    def setUp(self):
        super().setUp()
        h = self.home
        self.claude = os.path.join(h, ".claude")
        for d in ("sessions", "projects"):
            os.makedirs(os.path.join(self.claude, d))
        os.makedirs(os.path.join(h, "Projects"))
        self.data_dir = os.path.join(h, "Library", "Application Support", "Claude")
        os.makedirs(os.path.join(self.data_dir, "claude-code-sessions"))
        self.token_file = os.path.join(h, "state", "ccsession", "token")
        self.claude_open = os.path.join(h, "claude-open")
        with open(self.claude_open, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.claude_open, 0o755)
        self.config = os.path.join(h, "config.json")
        self.write_config()
        D.load_token(self.token_file)
        self.svc = S.Launchd(h, uid=501)
        os.makedirs(os.path.dirname(self.svc.path))
        with open(self.svc.path, "w") as f:
            f.write(self.svc.render([self.claude_open]))  # any existing file stands in for ccsessiond

    def write_config(self, **extra):
        with open(self.config, "w") as f:
            json.dump(dict({"claude_dir": self.claude, "projects_root": os.path.join(self.home, "Projects"),
                            "app_dirs": [self.data_dir], "token_file": self.token_file, "port": 1,
                            "claude_open": self.claude_open}, **extra), f)

    def doctor(self, launchctl=(0, LAUNCHCTL_RUNNING)):
        return Doc.doctor(self.config, platform="darwin", runner=FakeRunner({("launchctl", "print"): launchctl}),
                          home=self.home)

    def levels(self, report):
        out = {}
        for c in report.checks:
            out.setdefault(c["check"], c["level"])
        return out

    def serve(self):
        """A real ccsessiond on an ephemeral port, the config pointing at it."""
        sessions = D.Sessions(R.load_config(self.config), "test", procs=lambda: {})
        server = D.Server(("127.0.0.1", 0), sessions, D.load_token(self.token_file))
        sessions.prime()
        sessions.health = sessions._health()
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.write_config(port=server.server_address[1])
        return server

    def test_healthy(self):
        self.serve()
        r = self.doctor()
        self.assertEqual([c for c in r.checks if c["level"] != "ok"], [])
        self.assertEqual(set(self.levels(r)), {"config", "claude_dir", "sessions", "projects", "projects_root",
                                               "app_dir", "token", "service", "service program", "api",
                                               "claude-open", "parse"})
        self.assertIn("running, pid 4242", next(c["detail"] for c in r.checks if c["check"] == "service"))

    def test_problems(self):
        os.chmod(self.token_file, 0o644)
        os.rmdir(os.path.join(self.data_dir, "claude-code-sessions"))
        with open(os.path.join(self.claude, "sessions", "9.json"), "w") as f:
            f.write("{broken")
        self.write_config(claude_open=os.path.join(self.home, "nope"), surprise=1)
        r = self.doctor(launchctl=(113, "Could not find service"))
        lv = self.levels(r)
        self.assertEqual((lv["config"], lv["token"], lv["app_dir"], lv["claude-open"], lv["service"], lv["api"],
                          lv["parse"]), ("warn", "fail", "fail", "fail", "fail", "fail", "warn"))
        self.assertTrue(r.failed)

    def test_not_installed_is_only_a_warning(self):
        os.unlink(self.svc.path)
        lv = self.levels(self.doctor(launchctl=(113, "")))
        self.assertEqual((lv["service"], lv["api"]), ("warn", "warn"))

    def test_bad_config_stops(self):
        with open(self.config, "w") as f:
            f.write("{nope")
        r = self.doctor()
        self.assertEqual([(c["check"], c["level"]) for c in r.checks], [("config", "fail")])

    def test_linux_skips_desktop_and_claude_open(self):
        self.write_config(app_dirs=[], claude_open=None)
        r = Doc.doctor(self.config, platform="linux",
                       runner=FakeRunner({("systemctl",): (0, "LoadState=not-found\nActiveState=inactive\nMainPID=0")}),
                       home=self.home)
        lv = self.levels(r)
        self.assertEqual((lv["app_dirs"], lv["claude-open"], lv["service"]), ("ok", "ok", "warn"))

    def test_json_and_exit_code(self):
        out = io.StringIO()
        with mock.patch.object(Doc, "doctor", return_value=self.doctor(launchctl=(113, ""))):
            code = Doc.main(["--json"], out=out)
        report = json.loads(out.getvalue())
        self.assertEqual((code, report["ok"]), (1, False))
        self.assertTrue(all({"level", "check", "detail"} == set(c) for c in report["checks"]))


if __name__ == "__main__":
    unittest.main()

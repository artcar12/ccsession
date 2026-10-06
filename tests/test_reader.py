"""Tests for the session reader. Fixtures are synthetic copies of the real on-disk shapes
(sessions/<pid>.json, desktop local_*.json, transcript .jsonl), with no real content.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ccsession.reader as R  # noqa: E402
import ccsession.owner as O  # noqa: E402
from unittest import mock  # noqa: E402


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        if isinstance(obj, list):
            f.write("\n".join(json.dumps(o) for o in obj) + "\n")
        else:
            json.dump(obj, f)


def transcript(cwd, first, branch="main", reply="done", tool_paths=()):
    lines = [
        {"type": "user", "cwd": cwd, "gitBranch": branch, "timestamp": "2026-10-01T12:00:00.000Z",
         "message": {"role": "user", "content": first}},
        {"type": "assistant", "cwd": cwd, "timestamp": "2026-10-01T12:01:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": reply}] + [
             {"type": "tool_use", "name": "Read", "input": {"file_path": p}} for p in tool_paths]}},
        {"type": "user", "cwd": cwd, "timestamp": "2026-10-01T12:02:00.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        {"type": "ai-title", "aiTitle": "Generated title"},
    ]
    return lines


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.root = os.path.join(t, "Projects")
        for repo in ("acme-api", "garden", "toys"):
            os.makedirs(os.path.join(self.root, repo))
        self.claude = os.path.join(t, ".claude")
        self.app_support = os.path.join(t, "Application Support")
        self.main_app = os.path.join(self.app_support, "Claude")
        self.second_app = os.path.join(self.app_support, "Claude-personal")
        self.cfg = R.load_config(os.path.join(t, "missing.json"))
        self.cfg.update({"claude_dir": self.claude, "projects_root": self.root,
                         "app_dirs": [self.main_app, self.second_app]})
        proj = os.path.join(self.claude, "projects")
        # 1. desktop Code session in acme-api with a PR
        write(os.path.join(proj, "-acme", "s-api.jsonl"),
              transcript(os.path.join(self.root, "acme-api"), "fix the logging", "feature/logging"))
        write(os.path.join(self.main_app, "claude-code-sessions", "acct-1", "org-1", "local_a.json"), {
            "sessionId": "local_a", "cliSessionId": "s-api", "cwd": os.path.join(self.root, "acme-api"),
            "title": "Payment logging", "createdAt": 1790000000000, "lastActivityAt": "1790000100000",
            "isArchived": False, "completedTurns": 3,
            "postTurnSummary": {"status_detail": "logging added", "needs_user": False},
            "prs": [{"prNumber": 1093, "url": "https://git.example/x/1093", "repo": "acme/acme-api",
                     "host": "gitlab", "branch": "feature/logging", "baseRef": "dev", "state": "OPEN"}],
            "writtenBranches": ["feature/logging"]})
        # 2. worktree session in garden; same account id as #1 but in the second instance
        write(os.path.join(proj, "-garden--claude-worktrees-x", "s-garden.jsonl"),
              transcript(os.path.join(self.root, "garden", ".claude", "worktrees", "brave-x"), "plant beds"))
        write(os.path.join(self.second_app, "claude-code-sessions", "acct-1", "org-2", "local_b.json"), {
            "sessionId": "local_b", "cliSessionId": "s-garden", "cwd": os.path.join(self.root, "garden"),
            "title": "Plant beds", "isArchived": True, "postTurnSummary": {"status_detail": "beds planted"}})
        # 3. CLI session at the Projects root that touched toys files -> attributed by mentions
        write(os.path.join(proj, "-root", "s-root.jsonl"),
              transcript(self.root, "look at the toys", tool_paths=[os.path.join(self.root, "toys", f"f{i}.ts") for i in range(3)]))
        # 4. CLI session at the root with no repo
        write(os.path.join(proj, "-root", "s-chat.jsonl"), transcript(self.root, "car research"))
        # 5. Cowork session whose transcript lives inside the cowork dir
        cw = os.path.join(self.second_app, "local-agent-mode-sessions", "acct-2", "org-2")
        write(os.path.join(cw, "local_c.json"), {
            "sessionId": "local_c", "cliSessionId": "s-cowork", "title": "Cowork setup",
            "userSelectedFolders": [os.path.join(self.root, "toys")]})
        write(os.path.join(cw, "local_c", ".claude", "projects", "-x", "s-cowork.jsonl"),
              transcript("/vm/cwd", "set up writing voice"))
        # 6. desktop record whose transcript is gone
        write(os.path.join(self.main_app, "claude-code-sessions", "acct-1", "org-1", "local_d.json"), {
            "sessionId": "local_d", "cliSessionId": "s-gone", "cwd": os.path.join(self.root, "garden"),
            "title": "Old session", "completedTurns": 4})
        # live sessions
        write(os.path.join(self.claude, "sessions", "101.json"), {
            "pid": 101, "sessionId": "s-api", "cwd": os.path.join(self.root, "acme-api"), "name": "fix logging",
            "status": "waiting", "waitingFor": "permission", "entrypoint": "claude-desktop",
            "hostSessionId": "local_a", "statusUpdatedAt": 1790000200000, "startedAt": 1790000000000})
        write(os.path.join(self.claude, "sessions", "102.json"), {
            "pid": 102, "sessionId": "s-garden", "cwd": os.path.join(self.root, "garden"), "name": "Garden CLI",
            "status": "weird", "entrypoint": "cli"})

    def tearDown(self):
        self.tmp.cleanup()

    def index(self, **kw):
        rows, files, stats = R.scan_index(self.cfg, **kw)
        return rows, files, stats


class TestHelpers(unittest.TestCase):
    def test_project_of_folds_worktrees(self):
        self.assertEqual(R.project_of("/h/Projects/toys/.claude/worktrees/zen-x/src", "/h/Projects"), "toys")
        self.assertEqual(R.project_of("/h/Projects/acme-api/app", "/h/Projects"), "acme-api")
        self.assertIsNone(R.project_of("/h/Projects", "/h/Projects"))
        self.assertIsNone(R.project_of("/h", "/h/Projects"))
        self.assertIsNone(R.project_of(None, "/h/Projects"))

    def test_iso_ms_is_utc(self):
        self.assertEqual(R.iso_ms("1970-01-01T00:00:01.000Z"), 1000)
        self.assertIsNone(R.iso_ms("garbage"))

    def test_same_start_tolerates_rounding(self):
        self.assertTrue(R.same_start("Mon Oct  5 13:00:00 2026", "Mon Oct  5 13:00:01 2026"))
        self.assertFalse(R.same_start("Mon Oct  5 13:00:00 2026", "Mon Oct  5 13:00:09 2026"))
        self.assertFalse(R.same_start(None, "Mon Oct  5 13:00:00 2026"))

    def test_clip(self):
        self.assertEqual(R.clip("a  b\nc"), "a b c")
        self.assertEqual(len(R.clip("x" * 500)), R.SNIPPET)


class TestLiveness(unittest.TestCase):
    """PLAN.md §1 reader rules: no procStart is kept if the pid is alive; a mismatch is dropped."""
    DEAD = 999999  # above the pid ceiling on macOS and default Linux

    def test_is_live(self):
        me = os.getpid()
        self.assertTrue(R.is_live(me, None))
        self.assertTrue(R.is_live(me, R.real_start_utc(me)))
        self.assertFalse(R.is_live(me, "Mon Jan  1 00:00:00 2001"))
        self.assertFalse(R.is_live(self.DEAD, None))

    def test_read_live_applies_rules(self):
        me = os.getpid()
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "a.json"), {"pid": me, "sessionId": "no-start", "status": "busy"})
            write(os.path.join(d, "b.json"), {"pid": me, "sessionId": "match", "procStart": R.real_start_utc(me)})
            write(os.path.join(d, "c.json"), {"pid": me, "sessionId": "reused", "procStart": "Mon Jan  1 00:00:00 2001"})
            write(os.path.join(d, "d.json"), {"pid": self.DEAD, "sessionId": "dead"})
            with open(os.path.join(d, "e.json"), "w") as f:
                f.write("{not json")
            self.assertEqual({lv["id"] for lv in R.read_live(d)}, {"no-start", "match"})


class TestDataDirs(unittest.TestCase):
    def test_discovers_claude_dirs_with_code_sessions(self):
        with tempfile.TemporaryDirectory() as base:
            for name in ("Claude", "Claude-personal", "Claude-old", "Other"):
                os.makedirs(os.path.join(base, name))
            for name in ("Claude", "Claude-personal", "Other"):
                os.makedirs(os.path.join(base, name, "claude-code-sessions"))
            found = [os.path.basename(p) for p in R.discover_data_dirs(base)]
            self.assertEqual(found, ["Claude", "Claude-personal"])

    def test_unset_app_dirs_means_discover(self):
        self.assertIsNone(R.DEFAULT_CONFIG["app_dirs"])
        cfg = dict(R.DEFAULT_CONFIG, app_dirs=["/x/Claude"])
        self.assertEqual(R.data_dirs(cfg), ["/x/Claude"])


class TestIndex(Fixture):
    def test_rows(self):
        rows, _, stats = self.index()
        self.assertEqual(set(rows), {"s-api", "s-garden", "s-root", "s-chat", "s-cowork", "s-gone"})
        self.assertEqual(stats["desktop"], 4)

    def test_instance_and_account(self):
        rows, _, _ = self.index()
        self.assertEqual((rows["s-api"]["instance"], rows["s-api"]["account"]), ("Claude", "acct-1"))
        self.assertEqual((rows["s-garden"]["instance"], rows["s-garden"]["account"]), ("Claude-personal", "acct-1"))
        self.assertEqual((rows["s-chat"]["instance"], rows["s-chat"]["account"], rows["s-chat"]["kind"]),
                         (None, None, "cli"))

    def test_projects(self):
        rows, _, _ = self.index()
        self.assertEqual((rows["s-api"]["project"], rows["s-api"]["project_source"]), ("acme-api", "cwd"))
        self.assertEqual(rows["s-garden"]["project"], "garden")
        self.assertEqual((rows["s-root"]["project"], rows["s-root"]["project_source"]), ("toys", "mentions"))
        self.assertEqual((rows["s-chat"]["project"], rows["s-chat"]["project_source"]), (None, "none"))
        self.assertEqual((rows["s-cowork"]["kind"], rows["s-cowork"]["project"]), ("cowork", "toys"))

    def test_mentions_below_threshold_not_attributed(self):
        self.cfg["min_path_mentions"] = 4
        rows, _, _ = self.index()
        self.assertIsNone(rows["s-root"]["project"])

    def test_desktop_join(self):
        rows, _, _ = self.index()
        a = rows["s-api"]
        self.assertEqual(a["title"], "Payment logging")
        self.assertEqual(a["prs"][0]["number"], 1093)
        self.assertNotIn("baseRef", a["prs"][0])
        self.assertEqual(a["branch"], "feature/logging")
        self.assertEqual(a["host_session_id"], "local_a")
        self.assertEqual(a["last"], R.iso_ms("2026-10-01T12:02:00.000Z"))  # later of index and transcript
        self.assertEqual(rows["s-garden"]["summary"], "beds planted")
        self.assertTrue(rows["s-garden"]["archived"])
        self.assertEqual(rows["s-chat"]["title"], "Generated title")
        self.assertEqual(rows["s-chat"]["first_prompt"], "car research")
        self.assertEqual(rows["s-chat"]["last_reply"], "done")

    def test_desktop_record_without_transcript(self):
        rows, _, _ = self.index()
        self.assertEqual((rows["s-gone"]["title"], rows["s-gone"]["turns"], rows["s-gone"]["project"]),
                         ("Old session", 4, "garden"))

    def test_turns_ignore_tool_results(self):
        rows, _, _ = self.index()
        self.assertEqual(rows["s-chat"]["turns"], 1)

    def test_incremental(self):
        _, files, _ = self.index()
        rows, _, stats = self.index(files=files)
        self.assertEqual(rows, {})
        self.assertEqual(stats["scanned"], 0)
        p = os.path.join(self.claude, "projects", "-root", "s-chat.jsonl")
        with open(p, "a") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-10-02T00:00:00Z",
                                "message": {"role": "user", "content": "more"}}) + "\n")
        rows, _, _ = self.index(files=files)
        self.assertEqual(set(rows), {"s-chat"})
        self.assertEqual(rows["s-chat"]["turns"], 2)

    def test_full_rereads_everything(self):
        _, files, _ = self.index()
        rows, _, _ = self.index(files=files, full=True)
        self.assertEqual(len(rows), 6)


class TestLive(Fixture):
    def test_live_joined_on_host_session_id(self):
        live, _, _ = R.read_all(self.cfg, check_live=False)
        live = {lv["id"]: lv for lv in live}
        a = live["s-api"]
        self.assertEqual((a["status"], a["waiting_for"]), ("waiting", "permission"))
        self.assertEqual((a["instance"], a["account"], a["kind"]), ("Claude", "acct-1", "code"))
        self.assertEqual((a["title"], a["project"]), ("Payment logging", "acme-api"))
        # A CLI session has no hostSessionId, so it isn't joined to s-garden's desktop record.
        g = live["s-garden"]
        self.assertEqual((g["status"], g["kind"], g["instance"]), ("unknown", "cli", None))
        self.assertEqual((g["title"], g["project"]), ("Garden CLI", "garden"))


APP = "/Applications/Claude.app/Contents/MacOS/Claude"
HELPER = "/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"


class TestOwner(Fixture):
    """owner {data_dir, pid} from a made-up process table: work instance 500 (no --user-data-dir,
    so the default data dir), second instance 600; live s-api (pid 101) runs under 500."""

    def table(self, extra=None):
        t = {
            500: (1, APP),
            501: (500, f"{HELPER} --type=renderer --user-data-dir={self.main_app}"),
            600: (1, f"{APP} --user-data-dir={self.second_app}"),
            90: (500, "/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup -- /x/claude"),
            101: (90, "/x/claude --output-format stream-json"),
            102: (7, "/usr/local/bin/claude"),
        }
        t.update(extra or {})
        return t

    def read(self, procs):
        with mock.patch.object(R, "APP_SUPPORT", self.app_support):
            live, sessions, _ = R.read_all(self.cfg, check_live=False, procs=procs)
        return {r["id"]: r for r in live}, {r["id"]: r for r in sessions}

    def test_main_data_dir(self):
        with mock.patch.object(R, "APP_SUPPORT", self.app_support):
            self.assertEqual(O.main_data_dir(APP), self.main_app)
            self.assertEqual(O.main_data_dir(f"{APP} --user-data-dir={self.second_app} --foo=1"), self.second_app)
            self.assertIsNone(O.main_data_dir(f"{HELPER} --type=gpu-process --user-data-dir={self.main_app}"))
            self.assertIsNone(O.main_data_dir("/usr/local/bin/claude"))

    def test_live_session_walks_the_ppid_chain(self):
        live, sessions = self.read(self.table())
        self.assertEqual(live["s-api"]["owner"], {"data_dir": self.main_app, "pid": 500})
        self.assertEqual(sessions["s-api"]["owner"], {"data_dir": self.main_app, "pid": 500})

    def test_not_live_matched_to_running_instance(self):
        _, sessions = self.read(self.table())
        self.assertEqual(sessions["s-gone"]["owner"], {"data_dir": self.main_app, "pid": 500})
        self.assertEqual(sessions["s-garden"]["owner"], {"data_dir": self.second_app, "pid": 600})
        self.assertEqual(sessions["s-cowork"]["owner"], {"data_dir": self.second_app, "pid": 600})

    def test_instance_not_running(self):
        t = self.table()
        del t[600]
        _, sessions = self.read(t)
        self.assertEqual(sessions["s-garden"]["owner"], {"data_dir": self.second_app, "pid": None})

    def test_chain_to_another_instance_falls_back_to_the_record(self):
        # The chain reaches 600, but the record lives in the work dir: the record wins.
        live, _ = self.read(self.table({90: (600, "disclaimer")}))
        self.assertEqual(live["s-api"]["owner"], {"data_dir": self.main_app, "pid": 500})

    def test_cli_sessions_have_no_owner(self):
        live, sessions = self.read(self.table())
        self.assertIsNone(sessions["s-chat"]["owner"])
        # s-garden resumed from a terminal: the live process isn't the desktop session.
        self.assertIsNone(live["s-garden"]["owner"])
        self.assertTrue(sessions["s-garden"]["dormant"])


class TestTranscriptScan(unittest.TestCase):
    def test_resumed_read_matches_one_pass(self):
        """A transcript read in pieces (including a half-written last line) gives the same facts
        as one pass over the finished file."""
        cwd = "/tmp/Projects/garden"
        lines = [json.dumps(e) + "\n" for e in transcript(cwd, "first ask", tool_paths=["/tmp/Projects/garden/a"]) + [
            {"type": "user", "cwd": cwd, "timestamp": "2026-10-01T12:03:00.000Z",
             "message": {"role": "user", "content": "second ask"}},
            {"type": "assistant", "timestamp": "2026-10-01T12:04:00.000Z", "isApiErrorMessage": True,
             "error": "rate_limit", "apiErrorStatus": 429, "message": {"role": "assistant", "content": "limit"}}]]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s-x.jsonl")
            scan = R.TranscriptScan(path, "/tmp/Projects")
            text = "".join(lines)
            cuts = [len(lines[0]) + 10, len(lines[0]) + len(lines[1]) + len(lines[2]), len(text) - 5, len(text)]
            with open(path, "w") as f:
                pos = 0
                for cut in cuts:
                    f.write(text[pos:cut])
                    f.flush()
                    pos = cut
                    scan.read()
            self.assertEqual(scan.facts(), R.scan_transcript(path, "/tmp/Projects"))
            self.assertEqual((scan.facts()["turns"], scan.facts()["outcome"]), (2, "error"))
            self.assertFalse(scan.stale(os.stat(path)))
            with open(path, "w") as f:
                f.write(lines[0])
            self.assertTrue(scan.stale(os.stat(path)))


if __name__ == "__main__":
    unittest.main()

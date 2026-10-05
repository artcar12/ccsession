"""Tests for the collector agent. Fixtures are synthetic copies of the real on-disk shapes
(sessions/<pid>.json, desktop local_*.json, transcript .jsonl), with no real content.

Run: python3 -m unittest discover -s agent/tests
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ccdash_agent as A  # noqa: E402

SECRET = "SECRET-PROMPT-TEXT"


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
        for repo in ("acme-api", "HomeOrganizerApp", "ai_vis"):
            os.makedirs(os.path.join(self.root, repo))
        self.claude = os.path.join(t, ".claude")
        self.work_app = os.path.join(t, "app", "Claude")
        self.pers_app = os.path.join(t, "app", "Claude-personal")
        self.cfg = A.load_config(os.path.join(t, "missing.json"))
        self.cfg.update({
            "machine": "testbox", "claude_dir": self.claude, "projects_root": self.root,
            "app_dirs": {"work": self.work_app, "personal": self.pers_app},
            "state_dir": os.path.join(t, "state"),
        })
        proj = os.path.join(self.claude, "projects")
        # 1. work repo, run from the work desktop account, with a PR
        write(os.path.join(proj, "-acme", "s-work.jsonl"),
              transcript(os.path.join(self.root, "acme-api"), f"fix ACME-1068 {SECRET}", "feature/ACME-1068-x"))
        write(os.path.join(self.work_app, "claude-code-sessions", "acct", "org", "local_a.json"), {
            "sessionId": "local_a", "cliSessionId": "s-work", "cwd": os.path.join(self.root, "acme-api"),
            "title": "Payment logging", "createdAt": 1790000000000, "lastActivityAt": "1790000100000",
            "isArchived": False, "completedTurns": 3,
            "postTurnSummary": {"status_detail": f"summary {SECRET}", "needs_user": False},
            "prs": [{"prNumber": 1093, "url": "https://gitlab/x/1093", "repo": "acme/acme-api", "host": "gitlab",
                     "branch": "feature/ACME-1068-x", "baseRef": "dev", "state": "OPEN"}],
            "writtenBranches": ["feature/ACME-1068-x"]})
        # 2. personal repo, run from the WORK account (project wins over account)
        write(os.path.join(proj, "-hoa--claude-worktrees-x", "s-hoa.jsonl"),
              transcript(os.path.join(self.root, "HomeOrganizerApp", ".claude", "worktrees", "brave-x"), "work HOA-84"))
        write(os.path.join(self.work_app, "claude-code-sessions", "acct", "org", "local_b.json"), {
            "sessionId": "local_b", "cliSessionId": "s-hoa", "cwd": os.path.join(self.root, "HomeOrganizerApp"),
            "title": "Fix HOA-84", "postTurnSummary": {"status_detail": "HOA-84 fixed"}})
        # 3. CLI session at the Projects root that touched ai_vis files -> attributed by mentions
        write(os.path.join(proj, "-root", "s-root.jsonl"),
              transcript(self.root, "look at the toys", tool_paths=[os.path.join(self.root, "ai_vis", f"f{i}.ts") for i in range(3)]))
        # 4. CLI session at the root with no repo -> classed by account fallback (cli -> default)
        write(os.path.join(proj, "-root", "s-chat.jsonl"), transcript(self.root, "car research"))
        # 5. probe session -> dropped
        write(os.path.join(proj, "-root", "s-probe.jsonl"),
              transcript(self.root, "Reply with exactly one word: YES if a skill named x appears"))
        # 6. personal Cowork session whose transcript lives inside the cowork dir
        cw = os.path.join(self.pers_app, "local-agent-mode-sessions", "acct", "org")
        write(os.path.join(cw, "local_c.json"), {
            "sessionId": "local_c", "cliSessionId": "s-cowork", "title": "Cowork setup",
            "userSelectedFolders": [os.path.join(self.root, "ai_vis")]})
        write(os.path.join(cw, "local_c", ".claude", "projects", "-x", "s-cowork.jsonl"),
              transcript("/vm/cwd", "set up writing voice"))
        # live sessions
        write(os.path.join(self.claude, "sessions", "101.json"), {
            "pid": 101, "sessionId": "s-work", "cwd": os.path.join(self.root, "acme-api"), "name": "Payment logging",
            "status": "waiting", "waitingFor": "permission", "entrypoint": "claude-desktop",
            "hostSessionId": "local_a", "statusUpdatedAt": 1790000200000, "startedAt": 1790000000000})
        write(os.path.join(self.claude, "sessions", "102.json"), {
            "pid": 102, "sessionId": "s-hoa", "cwd": os.path.join(self.root, "HomeOrganizerApp"), "name": "Fix HOA-84",
            "status": "weird", "entrypoint": "cli"})

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, **kw):
        payload, files, stats = A.collect(self.cfg, check_live=False, **kw)
        return payload, {r["id"]: r for r in payload["sessions"]}, files, stats


class TestHelpers(unittest.TestCase):
    def test_project_of_folds_worktrees(self):
        self.assertEqual(A.project_of("/h/Projects/ai_vis/.claude/worktrees/zen-x/src", "/h/Projects"), "ai_vis")
        self.assertEqual(A.project_of("/h/Projects/acme-web/app", "/h/Projects"), "acme-web")
        self.assertIsNone(A.project_of("/h/Projects", "/h/Projects"))
        self.assertIsNone(A.project_of("/h", "/h/Projects"))
        self.assertIsNone(A.project_of(None, "/h/Projects"))

    def test_iso_ms_is_utc(self):
        self.assertEqual(A.iso_ms("1970-01-01T00:00:01.000Z"), 1000)
        self.assertIsNone(A.iso_ms("garbage"))

    def test_same_start_tolerates_rounding(self):
        self.assertTrue(A.same_start("Mon Oct  5 13:00:00 2026", "Mon Oct  5 13:00:01 2026"))
        self.assertFalse(A.same_start("Mon Oct  5 13:00:00 2026", "Mon Oct  5 13:00:09 2026"))
        self.assertFalse(A.same_start(None, "Mon Oct  5 13:00:00 2026"))

    def test_tickets(self):
        rx = A.DEFAULT_CONFIG["ticket_regexes"]
        self.assertEqual(A.find_tickets(["ACME-1068 and HOA-84", "HOA-84 again", None], rx), ["ACME-1068", "HOA-84"])


class TestCollect(Fixture):
    def test_counts_and_probe_dropped(self):
        payload, rows, _, stats = self.collect()
        self.assertEqual(set(rows), {"s-work", "s-hoa", "s-root", "s-chat", "s-cowork"})
        self.assertEqual(stats["probes"], 1)

    def test_classification(self):
        _, rows, _, _ = self.collect()
        self.assertEqual((rows["s-work"]["class"], rows["s-work"]["account"]), ("work", "work"))
        self.assertEqual((rows["s-hoa"]["class"], rows["s-hoa"]["project"]), ("personal", "HomeOrganizerApp"))
        self.assertEqual(rows["s-hoa"]["account"], "work")
        self.assertEqual((rows["s-root"]["project"], rows["s-root"]["class"]), ("ai_vis", "personal"))
        self.assertEqual(rows["s-chat"]["class_source"], "account")
        self.assertEqual(rows["s-cowork"]["kind"], "cowork")
        self.assertEqual(rows["s-cowork"]["project"], "ai_vis")

    def test_desktop_join(self):
        _, rows, _, _ = self.collect()
        w = rows["s-work"]
        self.assertEqual(w["title"], "Payment logging")
        self.assertEqual(w["prs"][0]["number"], 1093)
        self.assertEqual(w["tickets"], ["ACME-1068"])
        self.assertEqual(w["last"], A.iso_ms("2026-10-01T12:02:00.000Z"))  # later of index and transcript
        self.assertEqual(rows["s-hoa"]["summary"], "HOA-84 fixed")
        self.assertEqual(rows["s-chat"]["title"], "Generated title")

    def test_turns_ignore_tool_results(self):
        _, rows, _, _ = self.collect()
        self.assertEqual(rows["s-chat"]["turns"], 1)

    def test_work_rows_carry_only_allow_listed_fields(self):
        payload, rows, _, _ = self.collect()
        blob = json.dumps(payload)
        self.assertNotIn(SECRET, blob)
        for r in payload["sessions"]:
            if r["class"] == "work":
                self.assertLessEqual(set(r), A.WORK_FIELDS)
                for p in r["prs"]:
                    self.assertLessEqual(set(p), A.WORK_PR_FIELDS)
        for lv in payload["live"]:
            if lv["class"] == "work":
                self.assertLessEqual(set(lv), A.WORK_LIVE_FIELDS)

    def test_personal_rows_keep_detail(self):
        _, rows, _, _ = self.collect()
        self.assertEqual(rows["s-hoa"]["first_prompt"], "work HOA-84")
        self.assertEqual(rows["s-hoa"]["last_reply"], "done")

    def test_live(self):
        payload, _, _, _ = self.collect()
        live = {lv["id"]: lv for lv in payload["live"]}
        self.assertEqual(live["s-work"]["status"], "waiting")
        self.assertEqual(live["s-work"]["waiting_for"], "permission")
        self.assertEqual(live["s-work"]["class"], "work")
        self.assertEqual(live["s-hoa"]["status"], "unknown")
        self.assertEqual(live["s-hoa"]["project"], "HomeOrganizerApp")
        self.assertNotIn("cwd", live["s-hoa"])

    def test_incremental(self):
        _, _, files, _ = self.collect()
        payload, rows, _, stats = self.collect(state={"files": files})
        self.assertEqual(rows, {})
        self.assertEqual(stats["scanned"], 0)
        p = os.path.join(self.claude, "projects", "-root", "s-chat.jsonl")
        with open(p, "a") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-10-02T00:00:00Z",
                                "message": {"role": "user", "content": "more"}}) + "\n")
        _, rows, _, _ = self.collect(state={"files": files})
        self.assertEqual(set(rows), {"s-chat"})
        self.assertEqual(rows["s-chat"]["turns"], 2)

    def test_config_override_moves_repo_to_work(self):
        self.cfg["work_repos"] = self.cfg["work_repos"] + ["ai_vis"]
        _, rows, _, _ = self.collect()
        self.assertEqual(rows["s-root"]["class"], "work")
        self.assertNotIn("first_prompt", rows["s-root"])


    def test_server_cannot_move_work_session_to_personal(self):
        payload, rows, _, _ = self.collect(state={"files": {}, "overrides": {"s-work": "personal"}})
        self.assertEqual(rows["s-work"]["class"], "work")
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertLessEqual(set(rows["s-work"]), A.WORK_FIELDS)

    def test_server_can_move_personal_session_to_work(self):
        _, rows, _, _ = self.collect(state={"files": {}, "overrides": {"s-hoa": "work"}})
        self.assertEqual(rows["s-hoa"]["class"], "work")
        self.assertNotIn("first_prompt", rows["s-hoa"])

    def test_local_override_can_move_work_session_to_personal(self):
        self.cfg["local_overrides"] = {"s-work": "personal"}
        _, rows, _, _ = self.collect(state={"files": {}, "overrides": {"s-work": "work"}})
        self.assertEqual(rows["s-work"]["class"], "personal")
        self.assertEqual(rows["s-work"]["class_source"], "override")

if __name__ == "__main__":
    unittest.main()

"""Session v1 (schema/session.v1.json) and its fixtures.

schema/fixtures/sessions/  Session v1 examples, as ccsessiond emits them, for consumers' tests
schema/fixtures/reader/    one case per reader rule: files under a placeholder home, the config,
                           and the live and index rows the reader must produce

Validation needs jsonschema (a dev dependency: `uv run python -m unittest discover -s tests`);
with plain python3 those tests are skipped and the reader-rule cases still run.
"""
import copy
import glob
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ccsession.reader as R  # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None

SCHEMA_DIR = os.path.join(os.path.dirname(__file__), "..", "schema")
FIXTURES = os.path.join(SCHEMA_DIR, "fixtures")
DEAD_PID = 999999  # above the pid ceiling on macOS and default Linux

# Filled in by ccsessiond (owner resolution, outcome and error detection), not by the reader.
CCSESSIOND_ONLY = {"owner", "outcome"}

needs_jsonschema = unittest.skipUnless(jsonschema, "jsonschema not installed (uv run installs the dev group)")


def load(path):
    with open(path) as f:
        return json.load(f)


def schema():
    return load(os.path.join(SCHEMA_DIR, "session.v1.json"))


def closed(s):
    """The schema with unknown keys refused, so a misspelt or undeclared field fails here
    (the published schema stays open so other schemas can extend Session)."""
    s = copy.deepcopy(s)
    s["$defs"]["session"]["unevaluatedProperties"] = False
    s["$defs"]["pr"]["additionalProperties"] = False
    s["$defs"]["live"]["additionalProperties"] = False
    return s


def without_ccsessiond_fields(node):
    """The schema minus the requirements only ccsessiond can meet, for checking the reader's rows."""
    if isinstance(node, dict):
        # "if" blocks are conditions, not requirements: leave them alone.
        return {k: (sorted(set(v) - CCSESSIOND_ONLY) if k == "required"
                    else v if k == "if" else without_ccsessiond_fields(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [without_ccsessiond_fields(v) for v in node]
    return node


def validator(s, ref=None):
    if ref:
        s = dict(s, **{"$ref": "#/$defs/" + ref})
    return jsonschema.Draft202012Validator(s)


def session_fixtures():
    return {os.path.splitext(os.path.basename(p))[0]: load(p)
            for p in sorted(glob.glob(os.path.join(FIXTURES, "sessions", "*.json")))
            if not p.endswith("live.json")}


def reader_cases():
    return sorted(glob.glob(os.path.join(FIXTURES, "reader", "*.json")))


def fill(case_path, home):
    """A reader case with its placeholders filled: {{HOME}}, {{PID}} (this process, alive),
    {{DEAD_PID}} and {{PROC_START}} (this process's start time, as Claude Code writes it)."""
    with open(case_path) as f:
        text = f.read()
    me = os.getpid()
    text = (text.replace('"{{PID}}"', str(me)).replace('"{{DEAD_PID}}"', str(DEAD_PID))
            .replace("{{PROC_START}}", R.real_start_utc(me)).replace("{{HOME}}", json.dumps(home)[1:-1]))
    return json.loads(text)


def run_case(case, home):
    """Lays the case's files out under home and reads them the way read_all does."""
    for d in case.get("dirs", []):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    for rel, content in case["files"].items():
        path = os.path.join(home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            if isinstance(content, list):
                f.write("".join(json.dumps(line) + "\n" for line in content))
            elif isinstance(content, str):
                f.write(content)
            else:
                json.dump(content, f)
    cfg = dict(R.DEFAULT_CONFIG, claude_dir=os.path.join(home, ".claude"),
               projects_root=os.path.join(home, "Projects"))
    cfg.update(case.get("config") or {})
    with mock.patch.object(R, "APP_SUPPORT", os.path.join(home, "Library", "Application Support")):
        return R.read_all(cfg)


class TestSchema(unittest.TestCase):
    @needs_jsonschema
    def test_schema_is_valid(self):
        jsonschema.Draft202012Validator.check_schema(schema())
        jsonschema.Draft202012Validator.check_schema(closed(schema()))

    @needs_jsonschema
    def test_session_fixtures_validate(self):
        v = validator(closed(schema()))
        fx = session_fixtures()
        self.assertEqual(set(fx), {"desktop", "cli", "cowork", "waiting", "error"})
        for name, s in fx.items():
            with self.subTest(name):
                self.assertEqual([e.message for e in v.iter_errors(s)], [])

    @needs_jsonschema
    def test_live_snapshot_validates(self):
        snap = load(os.path.join(FIXTURES, "sessions", "live.json"))
        self.assertEqual([e.message for e in validator(closed(schema()), "live").iter_errors(snap)], [])

    @needs_jsonschema
    def test_fixtures_cover_the_contract(self):
        fx = session_fixtures()
        self.assertEqual({s["kind"] for s in fx.values()}, {"code", "cowork", "cli"})
        self.assertEqual({s["outcome"] for s in fx.values()}, {"ok", "error", "interrupted"})
        self.assertTrue({"waiting", "error", "busy", "idle", None} <= {s["status"] for s in fx.values()})
        self.assertTrue(any(s["dormant"] for s in fx.values()))
        self.assertTrue(any(s["archived"] for s in fx.values()))
        self.assertTrue(any(s["owner"] and s["owner"]["pid"] is None for s in fx.values()))

    @needs_jsonschema
    def test_bad_sessions_fail(self):
        v = validator(schema())
        fx = session_fixtures()
        bad = {
            "error status without error fields": {k: v_ for k, v_ in fx["error"].items()
                                                  if k not in ("error_kind", "api_status", "resets_at")},
            "error outcome without error fields": dict({k: v_ for k, v_ in fx["cowork"].items()}, outcome="error"),
            "desktop session without owner": {k: v_ for k, v_ in fx["desktop"].items() if k != "owner"},
            "desktop session without instance": dict(fx["desktop"], instance=None),
            "cli session with an owner": dict(fx["cli"], owner={"data_dir": "/x", "pid": None}),
            "dormant cli session": dict(fx["cli"], status=None, pid=None, status_since=None,
                                        waiting_for=None, dormant=True),
            "dormant and live": dict(fx["desktop"], dormant=True),
            "not live but has a pid": dict(fx["cowork"], pid=42),
            "unknown kind": dict(fx["cli"], kind="ide"),
            "unknown status": dict(fx["cli"], status="sleeping"),
            "unknown outcome": dict(fx["cli"], outcome="maybe"),
            "seconds, not ms": dict(fx["cli"], created="2026-10-01T12:00:00Z"),
            "owner pid missing": dict(fx["desktop"], owner={"data_dir": "/x"}),
        }
        for name, s in bad.items():
            with self.subTest(name):
                self.assertFalse(v.is_valid(s))

    @needs_jsonschema
    def test_live_snapshot_needs_live_sessions(self):
        snap = load(os.path.join(FIXTURES, "sessions", "live.json"))
        snap["sessions"].append(session_fixtures()["cowork"])
        self.assertFalse(validator(schema(), "live").is_valid(snap))


class TestReaderRules(unittest.TestCase):
    """Each schema/fixtures/reader case run through the reader."""

    def test_cases_cover_the_rules(self):
        names = {os.path.splitext(os.path.basename(p))[0] for p in reader_cases()}
        self.assertEqual(names, {"live-without-proc-start", "proc-start-mismatch", "shared-account-two-instances",
                                 "dormant", "archived", "cowork", "discover-data-dirs"})

    def test_cases(self):
        for path in reader_cases():
            with self.subTest(os.path.basename(path)), tempfile.TemporaryDirectory() as home:
                case = fill(path, home)
                live, sessions, _ = run_case(case, home)
                got = {"live": {lv["id"]: lv for lv in live}, "sessions": {s["id"]: s for s in sessions}}
                for part in ("live", "sessions"):
                    self.assertEqual(set(got[part]), set(case["expect"][part]), part)
                    for sid, want in case["expect"][part].items():
                        self.assertEqual({k: got[part][sid].get(k) for k in want}, want, f"{part} {sid}")

    @needs_jsonschema
    def test_reader_rows_validate(self):
        v = validator(closed(without_ccsessiond_fields(schema())))
        for path in reader_cases():
            with self.subTest(os.path.basename(path)), tempfile.TemporaryDirectory() as home:
                live, sessions, _ = run_case(fill(path, home), home)
                for row in live + sessions:
                    self.assertEqual([e.message for e in v.iter_errors(row)], [], row["id"])


class TestFixturesScrubbed(unittest.TestCase):
    """ccsession is public: fixtures hold made-up content only."""
    TICKET = re.compile(r"\b[A-Z]{2,}[-_]?\d{3,}\b")
    HOME_PATH = re.compile(r"/(?:Users|home)/(?!dev/)[^/\s\"]+")

    def test_no_ticket_ids_or_real_home_paths(self):
        hits = []
        for path in glob.glob(os.path.join(FIXTURES, "**", "*.json"), recursive=True):
            with open(path) as f:
                for i, line in enumerate(f, 1):
                    for rx in (self.TICKET, self.HOME_PATH):
                        m = rx.search(line)
                        if m:
                            hits.append(f"{os.path.relpath(path, FIXTURES)}:{i}: {m.group(0)}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()

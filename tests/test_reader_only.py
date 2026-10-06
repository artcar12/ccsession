"""ccsession is a reader only: nothing in the package knows about a dashboard, a server or how
sessions get classified or redacted. Those live in the consumers."""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
FORBIDDEN = re.compile(
    r"linode|ingest|classif|redact|allow.?list|work_repos|work_fields|probe|ticket|\bssh\b|"
    r"push|upload|ccdash|claude-dash|dashboard|uplink",
    re.IGNORECASE)


def checked_files():
    for name in ("README.md", "pyproject.toml"):
        yield os.path.join(ROOT, name)
    for dirpath, _, names in os.walk(os.path.join(ROOT, "ccsession")):
        for n in names:
            if n.endswith(".py"):
                yield os.path.join(dirpath, n)


class TestReaderOnly(unittest.TestCase):
    def test_no_dashboard_words(self):
        hits = []
        for path in checked_files():
            with open(path) as f:
                for i, line in enumerate(f, 1):
                    m = FORBIDDEN.search(line)
                    if m:
                        hits.append(f"{os.path.relpath(path, ROOT)}:{i}: {m.group(0)}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()

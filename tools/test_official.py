"""python tools/test_official.py (also run by validate.yml)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import duo_index  # noqa: E402


class Official(unittest.TestCase):
    def setUp(self):
        self.original = duo_index.OFFICIAL_FILE

    def tearDown(self):
        duo_index.OFFICIAL_FILE = self.original

    def test_official_ids_ignores_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "OFFICIAL"
            path.write_text("# header\n\ngamenative-duo.a\n  gamenative-duo.b  # trailing comment\n", encoding="utf-8")
            duo_index.OFFICIAL_FILE = path
            self.assertEqual(duo_index.official_ids(), {"gamenative-duo.a", "gamenative-duo.b"})

    def test_missing_file_means_no_official_packages(self):
        with tempfile.TemporaryDirectory() as d:
            duo_index.OFFICIAL_FILE = Path(d) / "missing"
            self.assertEqual(duo_index.official_ids(), set())


if __name__ == "__main__":
    unittest.main()

"""python tools/test_rating_rules.py (also run by validate.yml)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rating_rules as rr  # noqa: E402


def users(*logins):
    return [{"login": login, "__typename": "User"} for login in logins]


class Votes(unittest.TestCase):
    def test_one_vote_per_account(self):
        # carol reacted both ways: counts as neither.
        self.assertEqual(rr.aggregate_votes(users("alice", "bob", "carol"), users("carol", "dave"), []), (2, 1))

    def test_authors_and_bots_do_not_count(self):
        up = users("Author", "alice") + [{"login": "github-actions[bot]", "__typename": "Bot"}, None]
        self.assertEqual(rr.aggregate_votes(up, users("AUTHOR"), ["author"]), (1, 0))

    def test_case_insensitive_and_deduplicated(self):
        self.assertEqual(rr.aggregate_votes(users("Alice", "alice"), [], []), (1, 0))


BODY = """### Package

gamenative-duo.isaac-minimap-below

### Version

1.0.0

### Device

ayn-thor

### Result

works

### What did you see?

_No response_
"""


def issue(login, created, body=BODY):
    return {"author": {"login": login, "__typename": "User"}, "createdAt": created, "body": body}


class Compat(unittest.TestCase):
    def test_parse_form(self):
        f = rr.parse_report(BODY)
        self.assertEqual(f["package"], "gamenative-duo.isaac-minimap-below")
        self.assertEqual(f["what did you see?"], "")

    def test_parse_form_with_windows_line_endings(self):
        self.assertEqual(rr.parse_report(BODY.replace("\n", "\r\n"))["device"], "ayn-thor")

    def test_newest_report_per_account_and_device(self):
        reports = [
            rr.report_from_issue(issue("alice", "2026-01-01T00:00:00Z")),
            rr.report_from_issue(issue("alice", "2026-02-01T00:00:00Z", BODY.replace("\nworks\n", "\nbroken\n"))),
            rr.report_from_issue(issue("bob", "2026-01-05T00:00:00Z")),
        ]
        self.assertEqual(
            rr.aggregate_compat(reports, "gamenative-duo.isaac-minimap-below", "1.0.0"),
            {"ayn-thor": {"works": 1, "issues": 0, "broken": 1}},
        )

    def test_other_versions_and_bad_reports_are_ignored(self):
        old = rr.report_from_issue(issue("carol", "2026-01-01T00:00:00Z", BODY.replace("1.0.0", "0.9.0")))
        self.assertEqual(rr.aggregate_compat([old], "gamenative-duo.isaac-minimap-below", "1.0.0"), {})
        self.assertIsNone(rr.report_from_issue(issue("dave", "x", BODY.replace("\nworks\n", "\nmaybe\n"))))
        self.assertIsNone(rr.report_from_issue(issue("erin", "x", BODY.replace("ayn-thor", "../etc"))))
        self.assertIsNone(rr.report_from_issue({"author": None, "createdAt": "x", "body": BODY}))
        bot = {"author": {"login": "spam[bot]", "__typename": "Bot"}, "createdAt": "x", "body": BODY}
        self.assertIsNone(rr.report_from_issue(bot))

    def test_result_with_description(self):
        r = rr.report_from_issue(issue("fay", "x", BODY.replace("\nworks\n", "\nissues: works with problems\n")))
        self.assertEqual(r["result"], "issues")


if __name__ == "__main__":
    unittest.main()

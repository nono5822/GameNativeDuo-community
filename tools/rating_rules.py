"""Rules that turn raw GitHub data into the ratings the app shows (pure functions, unit-tested).

Votes: 👍/👎 reactions on the package's discussion.
- One vote per GitHub account: an account that left both 👍 and 👎 counts as neither (it cannot say both).
- The package's own authors do not count (manifest `githubLogins`, filled by the share bot with the issue's author).
- Bots do not count.

Compatibility reports: issues from the "compat-report" form, one per account per package version (the newest wins),
counted per device profile for the package's current version only (an update starts from zero: old reports may no
longer apply). Authors' own reports count: they are often the only people with the hardware, and the form says so.
"""

import re

RULES_VERSION = 2  # the app shows "one vote per account" only when the index says rules >= 2
RESULTS = ("works", "issues", "broken")
DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def _login(user):
    """GraphQL user object (or None for deleted accounts) -> lower-case login, or None."""
    if not user or not user.get("login"):
        return None
    if user.get("__typename") == "Bot" or user["login"].endswith("[bot]"):
        return None
    return user["login"].lower()


def aggregate_votes(up_users, down_users, authors):
    """up_users/down_users: GraphQL user objects that reacted 👍/👎. authors: logins (any case). -> (up, down)."""
    excluded = {a.lower() for a in authors}
    up = {u for u in map(_login, up_users) if u and u not in excluded}
    down = {u for u in map(_login, down_users) if u and u not in excluded}
    both = up & down
    return len(up - both), len(down - both)


def parse_report(body):
    """Fields of an issue-form body ("### Label\\n\\nvalue" sections) keyed by lower-case label."""
    fields, label = {}, None
    for line in (body or "").replace("\r\n", "\n").splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            label = heading.group(1).strip().lower()
            fields[label] = []
        elif label is not None:
            fields[label].append(line)
    out = {}
    for key, lines in fields.items():
        value = "\n".join(lines).strip()
        out[key] = "" if value == "_No response_" else value
    return out


def report_from_issue(issue):
    """A compat-report issue -> {"login", "package", "version", "device", "result", "created"} or None if unusable."""
    login = _login(issue.get("author"))
    f = parse_report(issue.get("body"))
    result = f.get("result", "").strip().lower().split(" ")[0].rstrip(":,.")
    device = f.get("device", "").strip().lower()
    package = f.get("package", "").strip().lower()
    if not login or result not in RESULTS or not DEVICE_RE.match(device) or not package:
        return None
    return {
        "login": login,
        "package": package,
        "version": f.get("version", "").strip(),
        "device": device,
        "result": result,
        "created": issue.get("createdAt", ""),
    }


def aggregate_compat(reports, package_id, version):
    """reports: from report_from_issue. -> {device: {"works", "issues", "broken"}} for this package version."""
    latest = {}
    mine = (r for r in reports if r and r["package"] == package_id and r["version"] == version)
    for r in sorted(mine, key=lambda r: r["created"]):
        latest[(r["login"], r["device"])] = r  # newest report of each account per device wins
    out = {}
    for r in latest.values():
        counts = out.setdefault(r["device"], {k: 0 for k in RESULTS})
        counts[r["result"]] += 1
    return out

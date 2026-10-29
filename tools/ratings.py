#!/usr/bin/env python3
"""Ratings from GitHub Discussions and compatibility reports.

One discussion per package in the "Show and tell" category, created here if missing: 👍/👎 reactions on it are the
rating, its comments the reviews. Issues from the "Compatibility report" form say how a package runs per device.
The rules (one vote per account, authors and bots excluded, newest report per account and device) live in
rating_rules.py, which is unit-tested.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python tools/ratings.py --out ratings.json

Writes {package id: {"up", "down", "comments", "url", "rules", "reportUrl", "compat"}}. Only CI calls GitHub (with
its own token); the app reads the counts from the signed index. Never fails the build: on any API error it writes
what it has (possibly nothing).
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duo_index  # noqa: E402
import rating_rules  # noqa: E402

API = "https://api.github.com/graphql"
CATEGORY_SLUG = "show-and-tell"
TITLE_PREFIX = "[package] "
REPORT_LABEL = "compat-report"
REPORT_TEMPLATE = "compat-report.yml"

# The reacting accounts, not just counts: the rules need to know who voted.
VOTES = """
    up: reactions(first: 100, content: THUMBS_UP) { totalCount nodes { user { login __typename } } }
    down: reactions(first: 100, content: THUMBS_DOWN) { totalCount nodes { user { login __typename } } }
"""


def graphql(token, query, **variables):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def existing_discussions(token, owner, name):
    """{title: discussion} for every discussion in the repo (paginated)."""
    out, cursor = {}, None
    while True:
        d = graphql(token, """
            query($owner: String!, $name: String!, $cursor: String) {
              repository(owner: $owner, name: $name) {
                id
                discussionCategories(first: 25) { nodes { id slug } }
                discussions(first: 50, after: $cursor) {
                  pageInfo { hasNextPage endCursor }
                  nodes { id title url comments { totalCount } %s }
                }
              }
            }""" % VOTES, owner=owner, name=name, cursor=cursor)["repository"]
        for n in d["discussions"]["nodes"]:
            out[n["title"]] = n
        if not d["discussions"]["pageInfo"]["hasNextPage"]:
            return d["id"], d["discussionCategories"]["nodes"], out
        cursor = d["discussions"]["pageInfo"]["endCursor"]


def all_reactors(token, discussion, content, key):
    """Every account that left this reaction (the first page came with the discussion)."""
    first = discussion.get(key) or {}
    users = [n["user"] for n in first.get("nodes", [])]
    if first.get("totalCount", 0) <= len(users):
        return users
    users, cursor = [], None
    while True:
        d = graphql(token, """
            query($id: ID!, $content: ReactionContent!, $cursor: String) {
              node(id: $id) { ... on Discussion {
                reactions(first: 100, after: $cursor, content: $content) {
                  pageInfo { hasNextPage endCursor } nodes { user { login __typename } }
                }
              } }
            }""", id=discussion["id"], content=content, cursor=cursor)["node"]["reactions"]
        users += [n["user"] for n in d["nodes"]]
        if not d["pageInfo"]["hasNextPage"]:
            return users
        cursor = d["pageInfo"]["endCursor"]


def compat_reports(token, owner, name):
    """Every usable compatibility report (open or closed), parsed by rating_rules."""
    out, cursor = [], None
    while True:
        d = graphql(token, """
            query($owner: String!, $name: String!, $label: String!, $cursor: String) {
              repository(owner: $owner, name: $name) {
                issues(first: 100, after: $cursor, labels: [$label], states: [OPEN, CLOSED]) {
                  pageInfo { hasNextPage endCursor }
                  nodes { author { login __typename } createdAt body }
                }
              }
            }""", owner=owner, name=name, label=REPORT_LABEL, cursor=cursor)["repository"]["issues"]
        out += [r for r in map(rating_rules.report_from_issue, d["nodes"]) if r]
        if not d["pageInfo"]["hasNextPage"]:
            return out
        cursor = d["pageInfo"]["endCursor"]


def create(token, repo_id, category_id, manifest, folder, report_url):
    body = (
        f"**{manifest['title']}** ({manifest['kind']}) for {manifest.get('game', {}).get('title', 'devices')}\n\n"
        f"{manifest['description']}\n\n"
        f"React 👍 or 👎 (one vote per account; the authors' own votes are not counted) and comment with what you "
        f"saw. To say how it runs on your device, use the report form: {report_url}\n\n"
        f"The counts show in GameNative Duo after the nightly publish.\n\n"
        f"Package: `{folder}`"
    )
    d = graphql(token, """
        mutation($repo: ID!, $cat: ID!, $title: String!, $body: String!) {
          createDiscussion(input: {repositoryId: $repo, categoryId: $cat, title: $title, body: $body}) {
            discussion { id title url comments { totalCount } %s }
          }
        }""" % VOTES, repo=repo_id, cat=category_id, title=TITLE_PREFIX + manifest["id"], body=body)
    return d["createDiscussion"]["discussion"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-create", action="store_true", help="only read; do not create missing discussions")
    args = parser.parse_args()

    ratings = {}
    try:
        token = os.environ["GITHUB_TOKEN"]
        owner, name = os.environ["GITHUB_REPOSITORY"].split("/")
        report_url = f"https://github.com/{owner}/{name}/issues/new?template={REPORT_TEMPLATE}"
        repo_id, categories, discussions = existing_discussions(token, owner, name)
        try:
            reports = compat_reports(token, owner, name)
        except Exception as e:  # noqa: BLE001 - votes still publish without reports
            print(f"ratings: compat reports: {type(e).__name__}: {e}", file=sys.stderr)
            reports = []
        category = next((c["id"] for c in categories if c["slug"] == CATEGORY_SLUG), None)
        packages, _ = duo_index.validate()
        for _, folder, manifest in packages:
            pid = manifest["id"]
            discussion = discussions.get(TITLE_PREFIX + pid)
            if discussion is None and category and not args.no_create:
                rel = folder.relative_to(duo_index.ROOT).as_posix()
                discussion = create(token, repo_id, category, manifest, rel, report_url)
            if discussion is None:
                continue
            up, down = rating_rules.aggregate_votes(
                all_reactors(token, discussion, "THUMBS_UP", "up"),
                all_reactors(token, discussion, "THUMBS_DOWN", "down"),
                manifest.get("githubLogins", []),
            )
            ratings[pid] = {
                "up": up,
                "down": down,
                "comments": discussion["comments"]["totalCount"],
                "url": discussion["url"],
                "rules": rating_rules.RULES_VERSION,
                "reportUrl": report_url,
                "compat": rating_rules.aggregate_compat(reports, pid, manifest["packageVersion"]),
            }
    except Exception as e:  # noqa: BLE001 - ratings are optional; publishing must go on
        print(f"ratings: {type(e).__name__}: {e}", file=sys.stderr)
    args.out.write_text(json.dumps(ratings, indent=2, sort_keys=True) + "\n")
    print(f"ratings: {len(ratings)} package(s)")


if __name__ == "__main__":
    main()

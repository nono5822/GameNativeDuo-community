#!/usr/bin/env python3
"""Ratings from GitHub Discussions: one discussion per package in the "Show and tell" category, created here if
missing. 👍/👎 reactions on it are the rating, its comments the reviews.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python tools/ratings.py --out ratings.json

Writes {package id: {"up", "down", "comments", "url"}}. Only CI calls GitHub (with its own token); the app reads the
counts from the signed index. Never fails the build: on any API error it writes what it has (possibly nothing).
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duo_index  # noqa: E402

API = "https://api.github.com/graphql"
CATEGORY_SLUG = "show-and-tell"
TITLE_PREFIX = "[package] "


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
                discussions(first: 100, after: $cursor) {
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    title url
                    comments { totalCount }
                    reactionGroups { content reactors { totalCount } }
                  }
                }
              }
            }""", owner=owner, name=name, cursor=cursor)["repository"]
        for n in d["discussions"]["nodes"]:
            out[n["title"]] = n
        if not d["discussions"]["pageInfo"]["hasNextPage"]:
            return d["id"], d["discussionCategories"]["nodes"], out
        cursor = d["discussions"]["pageInfo"]["endCursor"]


def create(token, repo_id, category_id, manifest, folder):
    body = (
        f"**{manifest['title']}** ({manifest['kind']}) for {manifest.get('game', {}).get('title', 'devices')}\n\n"
        f"{manifest['description']}\n\n"
        f"React with 👍 if it works well for you, 👎 if it does not, and comment with your device and what you saw. "
        f"The counts show in GameNative Duo.\n\n"
        f"Package: `{folder}`"
    )
    d = graphql(token, """
        mutation($repo: ID!, $cat: ID!, $title: String!, $body: String!) {
          createDiscussion(input: {repositoryId: $repo, categoryId: $cat, title: $title, body: $body}) {
            discussion { title url comments { totalCount } reactionGroups { content reactors { totalCount } } }
          }
        }""", repo=repo_id, cat=category_id, title=TITLE_PREFIX + manifest["id"], body=body)
    return d["createDiscussion"]["discussion"]


def counts(discussion):
    reactions = {g["content"]: g["reactors"]["totalCount"] for g in discussion.get("reactionGroups") or []}
    return {
        "up": reactions.get("THUMBS_UP", 0),
        "down": reactions.get("THUMBS_DOWN", 0),
        "comments": discussion["comments"]["totalCount"],
        "url": discussion["url"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-create", action="store_true", help="only read; do not create missing discussions")
    args = parser.parse_args()

    ratings = {}
    try:
        token = os.environ["GITHUB_TOKEN"]
        owner, name = os.environ["GITHUB_REPOSITORY"].split("/")
        repo_id, categories, discussions = existing_discussions(token, owner, name)
        category = next((c["id"] for c in categories if c["slug"] == CATEGORY_SLUG), None)
        packages, _ = duo_index.validate()
        for _, folder, manifest in packages:
            discussion = discussions.get(TITLE_PREFIX + manifest["id"])
            if discussion is None and category and not args.no_create:
                discussion = create(token, repo_id, category, manifest, folder.relative_to(duo_index.ROOT).as_posix())
            if discussion is not None:
                ratings[manifest["id"]] = counts(discussion)
    except Exception as e:  # noqa: BLE001 - ratings are optional; publishing must go on
        print(f"ratings: {type(e).__name__}: {e}", file=sys.stderr)
    args.out.write_text(json.dumps(ratings, indent=2, sort_keys=True) + "\n")
    print(f"ratings: {len(ratings)} package(s)")


if __name__ == "__main__":
    main()

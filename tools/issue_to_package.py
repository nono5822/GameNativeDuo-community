#!/usr/bin/env python3
"""Turns a "Share a UI layout" issue (issue form, see .github/ISSUE_TEMPLATE/share-layout.yml) into a package folder.

    ISSUE_BODY=... ISSUE_NUMBER=12 python tools/issue_to_package.py

Prints the package folder on success. On failure exits 1 and writes the reasons to $RESULT_FILE (default:
issue-result.md) for the bot's comment. The issue body is untrusted: it is only parsed, never executed, and the
package it produces must pass the same validation as any pull request.

Sharing again with the same author and layout name updates that package (new minor version).
"""

import contextlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import duo_index  # noqa: E402

ROOT = duo_index.ROOT
NO_RESPONSE = "_No response_"


def sections(body):
    """Issue forms render each field as '### <label>' followed by its value."""
    out, current, lines = {}, None, []
    for line in body.replace("\r\n", "\n").split("\n"):
        m = re.match(r"^### (.+?)\s*$", line)
        if m:
            if current is not None:
                out[current] = "\n".join(lines).strip()
            current, lines = m.group(1), []
        else:
            lines.append(line)
    if current is not None:
        out[current] = "\n".join(lines).strip()
    return {k: ("" if v == NO_RESPONSE else v) for k, v in out.items()}


def slug(text, limit):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-")


def fail(message):
    Path(os.environ.get("RESULT_FILE", "issue-result.md")).write_text(message + "\n", encoding="utf-8")
    print(message, file=sys.stderr)
    sys.exit(1)


def main():
    f = sections(os.environ.get("ISSUE_BODY", ""))
    game = f.get("Game", "").strip()
    m = re.fullmatch(r"(STEAM|EPIC|GOG|AMAZON)_([A-Za-z0-9_.-]{1,64})", game, re.IGNORECASE)
    if not m:
        fail(f"**Game** must look like `STEAM_433340` (got `{game[:80]}`).")
    store, app_id = m.group(1).lower(), m.group(2)

    layout_text = f.get("Layout", "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", layout_text, re.DOTALL)
    if fence:
        layout_text = fence.group(1)
    try:
        layout = json.loads(layout_text)
    except ValueError as e:
        fail(f"**Layout** is not valid JSON: {e}")
    if not isinstance(layout, dict) or not layout.get("elements"):
        fail("**Layout** has no elements: change something in the layout editor first.")
    layout["game"] = f"{store.upper()}_{app_id}"

    author = slug(f.get("Author", ""), 39)
    name_slug = slug(f.get("Layout name", ""), 63)
    if not author or not name_slug:
        fail("**Author** and **Layout name** need at least one letter or number.")
    pid = f"{author}.{name_slug}"
    folder = ROOT / "games" / f"{store}-{app_id.lower()}" / pid

    version = "1.0.0"
    if (folder / "manifest.json").is_file():
        old = json.loads((folder / "manifest.json").read_text(encoding="utf-8")).get("packageVersion", "1.0.0")
        major, minor, _ = (int(x) for x in old.split("."))
        version = f"{major}.{minor + 1}.0"

    manifest = {
        "$schema": "../../../schema/package.schema.json",
        "format": "gamenative-duo/package",
        "version": 1,
        "id": pid,
        "kind": "ui-layout",
        "packageVersion": version,
        "game": {"store": store, "appId": app_id, "title": f.get("Game title", "").strip()[:120] or game},
        "title": f.get("Layout name", "").strip()[:60],
        "description": f.get("Description", "").strip()[:600],
        "authors": [f.get("Author", "").strip()[:60]],
        "license": f.get("Licence", "CC0-1.0").strip(),
        "files": [{"path": "ui-layout.json", "sha256": ""}],
    }
    engine = layout.get("engine")
    if isinstance(engine, str) and re.fullmatch(r"[a-z0-9-]{1,40}", engine):
        manifest["requires"] = {"adapter": engine}
    device = slug(f.get("Device", ""), 40)
    if device:
        manifest["testedOn"] = [device]
    build = f.get("Game version", "").strip()[:40]
    if build:
        manifest["game"]["builds"] = [build]
    if os.environ.get("ISSUE_NUMBER"):
        manifest["tags"] = ["shared"]

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ui-layout.json").write_text(json.dumps(layout, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # The workflow reads the folder from stdout: everything else goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        duo_index.rehash(folder)
        _, problems = duo_index.validate()
    mine = [p for p in problems.items if p.startswith(folder.relative_to(ROOT).as_posix())]
    if mine:
        fail("The layout could not be turned into a package:\n\n" + "\n".join(f"- {p}" for p in mine))
    print(folder.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()

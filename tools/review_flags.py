#!/usr/bin/env python3
"""Review hints for a pull request: what changed in code mods, and lines a reviewer should look at.

    python tools/review_flags.py --base BASE_CHECKOUT --head HEAD_CHECKOUT [--diff-out FILE] > comment.md

Reads both trees as data (never builds or runs anything from the pull request), so review.yml can run it with a token
that may comment. For each changed mod package: the source diff since the last approved version (the package's src/,
or the named repository between the old and the new commit), flagged added lines, and manifest changes that widen what
the mod may do. Flags are hints for the maintainer, not a verdict. --diff-out writes the full diff for ai_review.py.
"""

import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_EXTENSIONS = {".cs", ".csproj", ".props", ".targets", ".lua", ".c", ".h", ".cpp", ".hpp", ".toml", ".json", ".xml", ".py", ".ps1", ".bat", ".cmd", ".sh"}

RULES = [
    ("Network", re.compile(r"\bSystem\.Net\b|\bHttpClient\b|\bWebClient\b|\bWebRequest\b|\bTcpClient\b|\bTcpListener\b|"
                           r"\bUdpClient\b|\bnew\s+Socket\b|\bUnityWebRequest\b|\bnew\s+WWW\b|require\s*\(?\s*[\"']socket|"
                           r"\bWSAStartup\b|\bgetaddrinfo\b|\bInternetOpen|\bWinHttp")),
    ("Starts processes", re.compile(r"\bProcess\.Start\b|\bProcessStartInfo\b|\bos\.execute\b|\bio\.popen\b|\bpopen\s*\(|"
                                    r"\bsystem\s*\(|\bCreateProcess|\bShellExecute|\bWinExec\b")),
    ("Native code / P/Invoke", re.compile(r"\bDllImport\b|\bLibraryImport\b|\bstatic\s+extern\b|\bGetProcAddress\b|"
                                          r"\bLoadLibrary|\bMarshal\.(GetDelegateForFunctionPointer|AllocHGlobal|Copy)\b|"
                                          r"\bffi\.(cdef|load)\b|\bVirtualProtect\b|\bVirtualAlloc\b")),
    ("Files outside the game", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\{1,2}(?![nrt0\"\\])|%[A-Za-z]+%|\bGetFolderPath\b|"
                                          r"\bSpecialFolder\b|\.\./|\.\.\\{1,2}|"
                                          r"/sdcard|/data/|\bGetTempPath\b|\bHOME\b|\bUSERPROFILE\b")),
    ("Store sign-ins / secrets", re.compile(r"(?i)password|passwd|\btoken\b|cookie|ssfn|loginusers\.vdf|config\.vdf|"
                                            r"credential|refresh_token|access_token|keystore|\.ssh\b|shared_prefs")),
    ("Loads code at run time", re.compile(r"\bAssembly\.(Load|LoadFrom|LoadFile|UnsafeLoadFrom)\b|\bAppDomain\b|"
                                          r"\bActivator\.CreateInstance\b|\bType\.GetType\s*\(|System\.Reflection\.Emit|"
                                          r"\bDynamicMethod\b|\bCSharpCodeProvider\b|\bloadstring\b|\bdofile\b|"
                                          r"(?<![\w.])load\s*\(")),
    ("Obfuscation", re.compile(r"\bFromBase64String\b|[A-Za-z0-9+/]{120,}={0,2}|(\\x[0-9A-Fa-f]{2}){8,}|"
                               r"(\\u[0-9A-Fa-f]{4}){8,}|string\.char\s*\(\s*\d+(\s*,\s*\d+){8,}")),
]
LONG_LINE = 400
# Copies of the Duo SDK talk to the app over the bridge (UDP 127.0.0.1): their network lines are expected.
SDK_MARKERS = ("namespace GameNativeDuo.Sdk", "GameNative Duo SDK", "DUO_IMPLEMENTATION")


def flag_line(text):
    """Categories an added line falls into."""
    found = [name for name, rx in RULES if rx.search(text)]
    if len(text) > LONG_LINE:
        found.append("Obfuscation")
    return found


def package_dirs(root):
    for pattern in ("games/*/*", "devices/*"):
        for d in sorted(root.glob(pattern)):
            if d.is_dir() and (d / "manifest.json").is_file():
                yield d.relative_to(root).as_posix()


def tree_hash(folder):
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(folder).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def changed_packages(base, head):
    """(added/changed rel paths, removed rel paths)."""
    before, after = set(package_dirs(base)), set(package_dirs(head))
    changed = [r for r in sorted(after) if r not in before or tree_hash(base / r) != tree_hash(head / r)]
    return changed, sorted(before - after)


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def file_diffs(old_root, new_root):
    """{path: (old lines, new lines)} for source files that differ (old missing = added)."""
    out = {}
    paths = set()
    for root in (old_root, new_root):
        if root and root.is_dir():
            paths |= {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    for rel in sorted(paths):
        if Path(rel).suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        old = read_text(old_root / rel) if old_root and (old_root / rel).is_file() else []
        new = read_text(new_root / rel) if new_root and (new_root / rel).is_file() else []
        if old != new:
            out[rel] = (old, new)
    return out


def checkout_repo(repo, commit, dest):
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "fetch", "-q", "--depth", "1", repo, commit], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest, check=True, capture_output=True)
    return dest


def review_package(rel, base, head, work):
    """Markdown lines and the unified diff for one changed package."""
    m_new = json.loads((head / rel / "manifest.json").read_text(encoding="utf-8"))
    old_path = base / rel / "manifest.json"
    m_old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.is_file() else None
    kind = m_new.get("kind")
    status = "new" if m_old is None else f"{m_old.get('packageVersion')} → {m_new.get('packageVersion')}"
    lines = [f"### `{m_new.get('id')}` ({kind}, {status})", ""]
    lines += manifest_changes(m_old, m_new)
    if kind != "mod":
        return lines + ["Data only: no code.", ""], ""

    build_new, build_old = m_new.get("build"), (m_old or {}).get("build")
    old_src = new_src = None
    if build_new and "source" in build_new:
        s = build_new["source"]
        new_src = checkout_repo(s["repo"], s["commit"], work / "new")
        if s.get("path"):
            new_src = new_src / s["path"]
        if build_old and "source" in build_old and build_old["source"]["repo"] == s["repo"]:
            o = build_old["source"]
            old_src = checkout_repo(o["repo"], o["commit"], work / "old") / o.get("path", "")
            lines.append(f"Source: {s['repo']}/compare/{o['commit']}...{s['commit']}")
        else:
            lines.append(f"Source: {s['repo']}/tree/{s['commit']} (first review of this repository: read all of it)")
    else:
        new_src = head / rel / "src"
        old_src = base / rel / "src" if m_old else None
        lines.append("Source: the package's `src/` folder (the pull request's diff).")
    lines.append("")

    diffs = file_diffs(old_src, new_src)
    unified, flagged = [], []
    for path, (old, new) in diffs.items():
        unified += difflib.unified_diff(old, new, f"a/{path}", f"b/{path}", lineterm="")
        sdk = any(marker in "\n".join(new[:40]) for marker in SDK_MARKERS)
        matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
        for tag, _, _, j1, j2 in matcher.get_opcodes():
            if tag not in ("replace", "insert"):
                continue
            for n in range(j1, j2):
                cats = [c for c in flag_line(new[n]) if not (sdk and c == "Network")]
                if cats:
                    flagged.append((", ".join(cats), path, n + 1, new[n].strip()[:160]))
    lines.append(f"{len(diffs)} source file(s) changed.")
    if flagged:
        lines += ["", "| Flag | Where | Line |", "|---|---|---|"]
        for cats, path, n, text in flagged[:200]:
            lines.append(f"| {cats} | `{path}:{n}` | `{text.replace('|', '\\|').replace('`', '´')}` |")
        if len(flagged) > 200:
            lines.append(f"| … | {len(flagged) - 200} more | |")
    else:
        lines.append("No flagged lines.")
    lines.append("")
    return lines, "\n".join(unified)


def manifest_changes(old, new):
    """What the update widens: files written, DLL overrides, bridge features, the author key."""
    out = []
    o_install, n_install = (old or {}).get("install", {}), new.get("install", {})
    for key, label in (("gameFiles", "writes into the game folder"), ("prefixFiles", "writes into the Wine prefix")):
        added = sorted(set(n_install.get(key, {}).values()) - set(o_install.get(key, {}).values()))
        if added:
            out.append(f"- **New:** {label}: " + ", ".join(f"`{a or '(game folder root)'}`" for a in added))
    added = sorted(set(n_install.get("dllOverrides", [])) - set(o_install.get("dllOverrides", [])))
    if added:
        out.append("- **New:** Wine DLL overrides: " + ", ".join(f"`{a}`" for a in added))
    o_bridge, n_bridge = (old or {}).get("bridge", {}), new.get("bridge", {})
    for key in ("capabilities", "commands"):
        added = sorted(set(n_bridge.get(key, [])) - set(o_bridge.get(key, [])))
        if added:
            out.append(f"- **New:** bridge {key}: " + ", ".join(f"`{a}`" for a in added))
    o_key = ((old or {}).get("author") or {}).get("key")
    n_key = (new.get("author") or {}).get("key")
    if old is not None and o_key != n_key:
        out.append("- **Author key changed**: the app will ask users to agree again (\"This mod changed owner\").")
    return out + ([""] if out else [])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--diff-out", type=Path)
    args = parser.parse_args()

    changed, removed = changed_packages(args.base, args.head)
    out = ["## Review hints", "",
           "Automatic hints for the maintainer (`tools/review_flags.py`): flags point at lines worth reading, they are "
           "not a verdict. Code from this pull request was not run to produce them.", ""]
    diffs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, rel in enumerate(changed):
            work = Path(tmp) / str(i)
            work.mkdir()
            try:
                lines, diff = review_package(rel, args.base, args.head, work)
            except (subprocess.CalledProcessError, ValueError, KeyError) as e:
                lines, diff = [f"### `{rel}`", "", f"Could not review automatically: {e}", ""], ""
            out += lines
            if diff:
                diffs.append(f"===== {rel} =====\n{diff}")
    for rel in removed:
        out += [f"### `{rel}` removed", ""]
    if not changed and not removed:
        out.append("No package changed.")
    print("\n".join(out))
    if args.diff_out:
        args.diff_out.write_text("\n\n".join(diffs), encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Builds every code mod from source, the way CI does: the index lists only these outputs, never package binaries.

    python tools/build_mods.py --out built                   # every mod with a build section
    python tools/build_mods.py --out built --reproducible    # build twice in different folders, compare hashes
    python tools/build_mods.py --out built --refs-dir DIR    # take reference sets from DIR/<id>.zip (else download)
    python tools/build_mods.py --out built --package ID      # one package
    python tools/build_mods.py --out built --repo-mirror https://github.com/me/mod=../mod   # unpushed commit

Writes built/built.json ({package id: {"packageVersion", "source", "outputs": {package path: {"sha256", "size"}}}}) and
built/files/<sha256> for each output. duo_index.py build --built built publishes them.

Builds run in their own CI job with no secrets (MSBuild runs code while it builds): the job that signs the index only
hashes what this job hands over. Determinism: a pinned SDK (DOTNET_SDK, global.json in the work folder), Deterministic,
ContinuousIntegrationBuild and a PathMap of the work folder, so anyone can rebuild and compare.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import duo_index  # noqa: E402

# The SDK CI installs (publish.yml / validate.yml setup-dotnet). A change is a maintainer PR: outputs change with it.
DOTNET_SDK = "10.0.112"


class BuildError(Exception):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def run(cmd, cwd, quiet=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-25:])
        raise BuildError(f"{' '.join(cmd[:3])} ... failed:\n{tail}")
    return r.stdout


def fetch_ref(ref, refs_dir, dest):
    """Extracts reference set `ref` (a refs.json entry) into dest, after checking its sha256."""
    if refs_dir:
        path = Path(refs_dir) / f"{ref['id']}.zip"
        if not path.is_file():
            raise BuildError(f"reference set {ref['id']}: {path} not found")
        data = path.read_bytes()
    else:
        if "url" not in ref:
            raise BuildError(f"reference set {ref['id']} has no url (pass --refs-dir)")
        with urllib.request.urlopen(ref["url"], timeout=120) as r:
            data = r.read()
    if sha256_bytes(data) != ref["sha256"]:
        raise BuildError(f"reference set {ref['id']}: sha256 does not match refs.json")
    archive = dest.with_suffix(".zip")
    archive.write_bytes(data)
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise BuildError(f"reference set {ref['id']}: bad path {name}")
        z.extractall(dest)
    archive.unlink()


def checkout(build, folder, work, mirrors=None):
    """(source root, description): the package's src/ copied into work, or the named repository at the named commit.

    A repository is built in its checkout, so a project may use files outside build.source.path (shared code). mirrors
    maps a repository URL to a local clone to fetch from instead (offline builds, an unpushed commit); the commit is
    checked either way."""
    if "source" not in build:
        dest = work / "src"
        shutil.copytree(folder / duo_index.SOURCE_DIR, dest)
        return dest, "package"
    src = build["source"]
    repo = work / "repo"
    run(["git", "init", "-q", str(repo)], cwd=work)
    remote = (mirrors or {}).get(src["repo"], src["repo"])
    run(["git", "fetch", "-q", "--depth", "1", remote, src["commit"]], cwd=repo)
    run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=repo)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    if head != src["commit"]:
        raise BuildError(f"checked out {head}, the manifest names {src['commit']}")
    shutil.rmtree(repo / ".git")
    root = duo_index.inside(repo, src.get("path", "."))
    if root is None or not root.is_dir():
        raise BuildError(f"build.source.path {src.get('path')} not found in the repository")
    return root, f"{src['repo']}@{src['commit']}"


def build_one(folder, manifest, work, refs_dir, mirrors=None):
    """Builds one package in work; returns (source, {package path: bytes})."""
    build = manifest["build"]
    src, source = checkout(build, folder, work, mirrors)
    known = duo_index.ref_sets()
    ref_dirs = {}
    for ref_id in build.get("refs", []):
        if ref_id not in known:
            raise BuildError(f"reference set {ref_id} is not in refs.json")
        ref_dirs[ref_id] = work / "refs" / ref_id
        ref_dirs[ref_id].parent.mkdir(exist_ok=True)
        fetch_ref(known[ref_id], refs_dir, ref_dirs[ref_id])

    if build["engine"] == "dotnet":
        (work / "global.json").write_text(json.dumps({"sdk": {"version": DOTNET_SDK, "rollForward": "disable"}}))
        props = {}
        for name, value in build.get("properties", {}).items():
            for ref_id, path in ref_dirs.items():
                value = value.replace("{refs:" + ref_id + "}", str(path))
            props[name] = value.replace("{source}", str(src))
        cmd = ["dotnet", "build", str(src / build["project"]), "-c", build.get("configuration", "Release"), "-nologo",
               "-p:Deterministic=true", "-p:ContinuousIntegrationBuild=true", f"-p:PathMap={work}=/_/",
               "-p:RestoreLockedMode=true", "-p:GenerateDocumentationFile=false"]
        cmd += [f"-p:{k}={v}" for k, v in sorted(props.items())]
        run(cmd, cwd=work)

    outputs = {}
    for out, package_path in build["outputs"].items():
        path = duo_index.inside(src, out)
        if path is None or not path.is_file():
            raise BuildError(f"build.outputs: {out} was not produced")
        outputs[package_path] = path.read_bytes()
    return source, outputs


def mods(only=None):
    packages, problems = duo_index.validate()
    if problems:
        duo_index.report(problems)
        sys.exit(1)
    return [(f, m) for _, f, m in packages if m["kind"] == "mod" and "build" in m and (only is None or m["id"] == only)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--refs-dir", help="folder with <id>.zip reference sets (default: download from refs.json)")
    parser.add_argument("--package", help="build only this package id")
    parser.add_argument("--reproducible", action="store_true", help="build twice in different folders and compare")
    parser.add_argument("--repo-mirror", action="append", default=[], metavar="URL=PATH",
                        help="fetch this repository from a local clone (testing an unpushed commit, offline builds)")
    args = parser.parse_args()
    mirrors = dict(m.split("=", 1) for m in args.repo_mirror)

    if args.out.exists():
        shutil.rmtree(args.out)
    (args.out / "files").mkdir(parents=True)
    result, failed = {}, []
    for folder, manifest in mods(args.package):
        pid = manifest["id"]
        try:
            with tempfile.TemporaryDirectory(prefix="duo-build-") as a:
                source, outputs = build_one(folder, manifest, Path(a), args.refs_dir, mirrors)
            if args.reproducible:
                with tempfile.TemporaryDirectory(prefix="duo-rebuild-") as b:
                    _, again = build_one(folder, manifest, Path(b), args.refs_dir, mirrors)
                for path, data in outputs.items():
                    if sha256_bytes(again[path]) != sha256_bytes(data):
                        raise BuildError(f"{path} is not reproducible: two builds gave different bytes")
        except BuildError as e:
            failed.append(f"{pid}: {e}")
            continue
        entry = {"packageVersion": manifest["packageVersion"], "source": source, "outputs": {}}
        for path, data in sorted(outputs.items()):
            digest = sha256_bytes(data)
            (args.out / "files" / digest).write_bytes(data)
            entry["outputs"][path] = {"sha256": digest, "size": len(data)}
            print(f"{pid}: {path} {digest}")
        result[pid] = entry
    (args.out / "built.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if failed:
        print(f"{len(failed)} build(s) failed:", file=sys.stderr)
        for line in failed:
            print("  " + line, file=sys.stderr)
        sys.exit(1)
    print(f"Built {len(result)} mod(s) into {args.out}")


if __name__ == "__main__":
    main()

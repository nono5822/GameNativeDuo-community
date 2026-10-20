#!/usr/bin/env python3
"""GameNative Duo community packages: validate the repo, build the signed index, verify a built index.

    python tools/duo_index.py validate                      # every PR
    python tools/duo_index.py build --out site --base-url URL   # on main; signs with $DUO_INDEX_SIGNING_KEY
    python tools/duo_index.py verify --site site            # checks every signature and blob hash
    python tools/duo_index.py rehash games/steam-433340/me.my-layout   # fill in files[].sha256 after editing
    python tools/duo_index.py keygen                        # once: prints a new private key (keep it secret)

Layout of the repo:
    games/<store>-<appId>/<package-id>/manifest.json (+ files)   layouts, settings, mods for one game
    devices/<package-id>/manifest.json (+ files)                 device profiles

Published site (v1):
    v1/catalog.json (+ .sig)            every game with packages, and every device profile
    v1/games/<store>-<appId>.json (+ .sig)
    v1/blobs/<sha256>                   package files and screenshots, content-addressed
    v1/public-key.txt                   base64 raw ed25519 public key
Signatures are base64 raw ed25519 over the exact bytes of the JSON file.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import jsonschema
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
INDEX_VERSION = "v1"

# What each kind must contain, and the schema its main file follows.
MAIN_FILE = {
    "ui-layout": ("ui-layout.json", "ui-layout.schema.json"),
    "duo-settings": ("duo-settings.json", "duo-settings.schema.json"),
    "device-profile": ("device-profile.json", "device-profile.schema.json"),
}
DATA_KINDS = set(MAIN_FILE)
DATA_EXTENSIONS = {".json"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_DATA_FILE = 256 * 1024
MAX_IMAGE = 1024 * 1024
MAX_MOD_FILE = 20 * 1024 * 1024  # bigger mod binaries go to GitHub Releases (files[].url)


class Problems:
    def __init__(self):
        self.items = []

    def add(self, where, message):
        self.items.append(f"{where}: {message}")

    def __bool__(self):
        return bool(self.items)


def load_schema(name):
    return jsonschema.Draft202012Validator(json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8")))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def game_key(game):
    return f"{game['store']}-{game['appId'].lower()}"


def package_dirs():
    """(kind of folder, path) for every package folder in the repo."""
    for game_dir in sorted((ROOT / "games").glob("*")):
        if game_dir.is_dir():
            for pkg in sorted(game_dir.iterdir()):
                if pkg.is_dir():
                    yield "game", pkg
    for pkg in sorted((ROOT / "devices").glob("*")):
        if pkg.is_dir():
            yield "device", pkg


def inside(folder, rel):
    """The file at rel inside folder, or None when rel escapes it."""
    target = (folder / rel).resolve()
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return None
    return target


# ---- validate ---------------------------------------------------------------------------------------------------

def validate(fetch_remote=False):
    """Returns (packages, problems). packages: list of (folder kind, folder, manifest)."""
    problems = Problems()
    manifest_schema = load_schema("package.schema.json")
    file_schemas = {kind: load_schema(schema) for kind, (_, schema) in MAIN_FILE.items()}
    packages, seen_ids = [], {}

    for where, folder in package_dirs():
        rel = folder.relative_to(ROOT).as_posix()
        manifest_path = folder / "manifest.json"
        if not manifest_path.is_file():
            problems.add(rel, "no manifest.json")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            problems.add(rel, f"manifest.json is not valid JSON: {e}")
            continue
        errors = sorted(manifest_schema.iter_errors(manifest), key=lambda e: list(e.path))
        for e in errors:
            problems.add(rel, f"manifest: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}")
        if errors:
            continue

        kind, pid = manifest["kind"], manifest["id"]
        if folder.name != pid:
            problems.add(rel, f"folder name must be the package id '{pid}'")
        if pid in seen_ids:
            problems.add(rel, f"id '{pid}' already used by {seen_ids[pid]}")
        seen_ids[pid] = rel
        if where == "device" and kind != "device-profile":
            problems.add(rel, "only device-profile packages go in devices/")
        if where == "game":
            if kind == "device-profile":
                problems.add(rel, "device profiles go in devices/<id>/")
            elif folder.parent.name != game_key(manifest["game"]):
                problems.add(rel, f"must be in games/{game_key(manifest['game'])}/ (store-appId, lower case)")

        declared = {f["path"] for f in manifest["files"]}
        screenshots = manifest.get("screenshots", [])
        listed = declared | set(screenshots) | {"manifest.json"}

        # Every file in the folder is declared: nothing rides along unseen.
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            if path.relative_to(folder).as_posix() not in listed:
                problems.add(rel, f"undeclared file {path.relative_to(folder).as_posix()} (list it in files or screenshots, or remove it)")

        for f in manifest["files"]:
            check_file(problems, rel, folder, kind, f, fetch_remote)
        for shot in screenshots:
            path = inside(folder, shot)
            if path is None or not path.is_file():
                problems.add(rel, f"screenshot {shot} not found")
            elif path.suffix.lower() not in IMAGE_EXTENSIONS:
                problems.add(rel, f"screenshot {shot} must be {', '.join(sorted(IMAGE_EXTENSIONS))}")
            elif path.stat().st_size > MAX_IMAGE:
                problems.add(rel, f"screenshot {shot} is over {MAX_IMAGE // 1024} KB")

        if kind in MAIN_FILE:
            name, _ = MAIN_FILE[kind]
            path = folder / name
            if name not in declared or not path.is_file():
                problems.add(rel, f"a {kind} package needs {name} (in the folder and in files)")
            else:
                try:
                    content = json.loads(path.read_text(encoding="utf-8"))
                    for e in file_schemas[kind].iter_errors(content):
                        problems.add(rel, f"{name}: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}")
                except (ValueError, UnicodeDecodeError) as e:
                    problems.add(rel, f"{name} is not valid JSON: {e}")
        if kind == "mod":
            install = manifest["install"]
            for section in ("gameFiles", "prefixFiles"):
                for src in install.get(section, {}):
                    if not any(p == src or p.startswith(src.rstrip("/") + "/") for p in declared):
                        problems.add(rel, f"install.{section}: '{src}' matches no entry of files")
        packages.append((where, folder, manifest))
    return packages, problems


def check_file(problems, rel, folder, kind, f, fetch_remote):
    path, expected = f["path"], f["sha256"]
    ext = Path(path).suffix.lower()
    if kind in DATA_KINDS:
        if "url" in f:
            problems.add(rel, f"{path}: data packages keep their files in the folder (no url)")
            return
        if ext not in DATA_EXTENSIONS | IMAGE_EXTENSIONS:
            problems.add(rel, f"{path}: a {kind} package may only contain JSON and images (code needs kind 'mod')")
    if "url" in f:
        if fetch_remote:
            try:
                with urllib.request.urlopen(f["url"], timeout=60) as r:
                    data = r.read(MAX_MOD_FILE * 10 + 1)
                if hashlib.sha256(data).hexdigest() != expected:
                    problems.add(rel, f"{path}: sha256 of {f['url']} does not match")
            except OSError as e:
                problems.add(rel, f"{path}: cannot download {f['url']}: {e}")
        return
    target = inside(folder, path)
    if target is None:
        problems.add(rel, f"{path}: escapes the package folder")
        return
    if not target.is_file():
        problems.add(rel, f"{path}: not found")
        return
    size = target.stat().st_size
    limit = MAX_MOD_FILE if kind == "mod" else (MAX_IMAGE if ext in IMAGE_EXTENSIONS else MAX_DATA_FILE)
    if size > limit:
        problems.add(rel, f"{path}: {size} bytes is over the {limit // 1024} KB limit")
    actual = sha256_file(target)
    if actual != expected:
        problems.add(rel, f"{path}: sha256 is {actual}, manifest says {expected}")


# ---- build --------------------------------------------------------------------------------------------------------

def git_date(path):
    """Last commit date touching path (ISO 8601), so 'newest' sorting follows real changes; empty outside git."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cI", "--", str(path)], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        return out
    except (OSError, subprocess.CalledProcessError):
        return ""


def dump(obj):
    # Compact and deterministic: the same repo state always gives the same bytes (and signature).
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build(out, base_url, signing_key, ratings=None):
    packages, problems = validate(fetch_remote=True)
    if problems:
        report(problems)
        sys.exit(1)
    if out.exists():
        shutil.rmtree(out)
    site = out / INDEX_VERSION
    (site / "games").mkdir(parents=True)
    (site / "blobs").mkdir()
    base = base_url.rstrip("/") + "/" + INDEX_VERSION

    def blob(path):
        digest = sha256_file(path)
        shutil.copyfile(path, site / "blobs" / digest)
        return digest, f"{base}/blobs/{digest}", path.stat().st_size

    games, devices = {}, []
    for where, folder, m in packages:
        entry = {k: v for k, v in m.items() if k not in ("files", "screenshots", "$schema", "format", "version")}
        entry["updated"] = git_date(folder)
        if ratings and m["id"] in ratings:
            entry["rating"] = ratings[m["id"]]
        files = []
        for f in m["files"]:
            if "url" in f:
                files.append({"path": f["path"], "sha256": f["sha256"], "url": f["url"]})
            else:
                digest, url, size = blob(folder / f["path"])
                files.append({"path": f["path"], "sha256": digest, "url": url, "size": size})
        entry["files"] = files
        entry["screenshots"] = [blob(folder / s)[1] for s in m.get("screenshots", [])]
        if where == "device":
            devices.append(entry)
        else:
            games.setdefault(game_key(m["game"]), []).append(entry)

    catalog_games = []
    for key, entries in sorted(games.items()):
        entries.sort(key=lambda e: e["id"])
        newest = max(entries, key=lambda e: e["updated"])
        game = dict(newest["game"])
        game.pop("builds", None)
        write_signed(site / "games" / f"{key}.json", {
            "format": "gamenative-duo/index-game", "version": 1, "key": key, "game": game, "packages": entries,
        }, signing_key)
        catalog_games.append({
            "key": key, "game": game, "packages": len(entries),
            "kinds": sorted({e["kind"] for e in entries}), "updated": newest["updated"],
        })
    devices.sort(key=lambda e: e["id"])
    write_signed(site / "catalog.json", {
        "format": "gamenative-duo/index-catalog", "version": 1, "generated": git_date(ROOT),
        "games": catalog_games, "devices": devices,
    }, signing_key)

    public = signing_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    (site / "public-key.txt").write_text(base64.b64encode(public).decode() + "\n")
    shutil.copytree(SCHEMA_DIR, out / "schema")
    (out / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (out / ".nojekyll").write_text("")
    print(f"Built {sum(len(v) for v in games.values())} package(s) for {len(games)} game(s), {len(devices)} device profile(s) into {out}")


def write_signed(path, obj, private):
    data = dump(obj)
    path.write_bytes(data)
    Path(str(path) + ".sig").write_text(base64.b64encode(private.sign(data)).decode() + "\n")


def load_private_key():
    raw = os.environ.get("DUO_INDEX_SIGNING_KEY", "").strip()
    if not raw:
        sys.exit("DUO_INDEX_SIGNING_KEY is not set (base64 raw 32-byte ed25519 private key; see 'keygen')")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))


# ---- verify -------------------------------------------------------------------------------------------------------

def verify(site_root, public_b64=None):
    """Checks every signature and every blob: what the app does before trusting anything."""
    site = site_root / INDEX_VERSION
    public_b64 = public_b64 or (site / "public-key.txt").read_text().strip()
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
    problems = Problems()
    signed = [site / "catalog.json"] + sorted((site / "games").glob("*.json"))
    for path in signed:
        try:
            public.verify(base64.b64decode(Path(str(path) + ".sig").read_text().strip()), path.read_bytes())
        except Exception as e:  # noqa: BLE001 - any failure means untrusted
            problems.add(path.name, f"bad signature ({type(e).__name__})")
    for path in (site / "blobs").glob("*"):
        if sha256_file(path) != path.name:
            problems.add(f"blobs/{path.name}", "content does not match its name")
    for path in sorted((site / "games").glob("*.json")):
        for p in json.loads(path.read_bytes())["packages"]:
            for f in p["files"]:
                if "size" in f and not (site / "blobs" / f["sha256"]).is_file():
                    problems.add(path.name, f"{p['id']}: blob {f['sha256']} missing")
    if problems:
        report(problems)
        sys.exit(1)
    print(f"Verified {len(signed)} signed file(s) and their blobs")


def rehash(folder):
    """Writes the current sha256 of every local file into the package's manifest."""
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for f in manifest.get("files", []):
        target = inside(folder, f["path"])
        if "url" not in f and target is not None and target.is_file():
            f["sha256"] = sha256_file(target)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {path}")


def report(problems):
    print(f"{len(problems.items)} problem(s):", file=sys.stderr)
    for item in problems.items:
        print("  " + item, file=sys.stderr)


INDEX_HTML = """<!doctype html><meta charset="utf-8"><title>GameNative Duo community packages</title>
<p>Machine-readable index of <a href="https://github.com/nono5822/GameNativeDuo-community">GameNative Duo community
packages</a>, read by the app. Start at <a href="v1/catalog.json">v1/catalog.json</a>.</p>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--fetch-remote", action="store_true", help="also download release assets and check their sha256")
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, default=ROOT / "site")
    b.add_argument("--base-url", default="https://nono5822.github.io/GameNativeDuo-community")
    b.add_argument("--ratings", type=Path, help="ratings.json from tools/ratings.py")
    r = sub.add_parser("verify")
    r.add_argument("--site", type=Path, default=ROOT / "site")
    r.add_argument("--public-key", help="base64 public key to check against (default: the site's own)")
    h = sub.add_parser("rehash")
    h.add_argument("package", type=Path, help="package folder")
    sub.add_parser("keygen")
    args = parser.parse_args()

    if args.command == "validate":
        packages, problems = validate(args.fetch_remote)
        if problems:
            report(problems)
            sys.exit(1)
        print(f"OK: {len(packages)} package(s)")
    elif args.command == "build":
        ratings = json.loads(args.ratings.read_text()) if args.ratings and args.ratings.is_file() else None
        build(args.out, args.base_url, load_private_key(), ratings)
    elif args.command == "verify":
        verify(args.site, args.public_key)
    elif args.command == "rehash":
        rehash(args.package)
    elif args.command == "keygen":
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print("private (secret DUO_INDEX_SIGNING_KEY):", base64.b64encode(raw).decode())
        print("public  (pin in the app):              ", base64.b64encode(pub).decode())


if __name__ == "__main__":
    main()

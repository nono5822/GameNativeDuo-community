#!/usr/bin/env python3
"""GameNative Duo community packages: validate the repo, build the signed index, verify a built index.

    python tools/duo_index.py validate                      # every PR
    python tools/duo_index.py build --out site --built built    # on main; signs with $DUO_INDEX_SIGNING_KEY
    python tools/duo_index.py verify --site site            # checks every signature and blob hash
    python tools/duo_index.py rehash games/steam-433340/me.my-layout   # fill in files[].sha256 after editing
    python tools/duo_index.py sign games/steam-367520/me.my-mod --key ~/.ssh/id_ed25519   # author signature, after rehash
    python tools/duo_index.py handover PKG --old-key OLD --new-key NEW.pub                 # move a package to a new key
    python tools/duo_index.py keygen                        # once: prints a new index signing key (maintainers)

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
import tempfile
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
    # Configures the official Unity adapter (GameNative Duo duo/docs/game-profiles.md): data, no code review.
    "game-profile": ("profile.json", "game-profile.schema.json"),
}
DATA_KINDS = set(MAIN_FILE)
DATA_EXTENSIONS = {".json"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_DATA_FILE = 256 * 1024
MAX_IMAGE = 1024 * 1024
MAX_MOD_FILE = 20 * 1024 * 1024  # bigger mod binaries go to GitHub Releases (files[].url)
# Maintainer-owned list of official package ids (CODEOWNERS). Never read from manifests: the index's `official`
# list comes only from here, and the gamenative-duo. namespace is reserved for ids listed here.
OFFICIAL_FILE = ROOT / "OFFICIAL"
RESERVED_PREFIX = "gamenative-duo."
# Maintainer-owned pins (CODEOWNERS): the only binaries a package may contain, and the reference sets builds use.
LOADERS_FILE = ROOT / "loaders.json"
REFS_FILE = ROOT / "refs.json"
# Code is built by CI from source (build in the manifest); these never come from a package unless pinned in loaders.json.
BINARY_EXTENSIONS = {".dll", ".exe", ".so", ".dylib", ".asi", ".bin", ".pyd", ".node", ".jar", ".class", ".wasm"}
BINARY_MAGIC = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\x00asm", b"PK\x03\x04")
SCRIPT_EXTENSIONS = {".lua", ".toml", ".json", ".txt", ".md", ".cfg", ".ini", ".xml", ".yaml", ".yml"}
SOURCE_DIR = "src"
# Author signatures (ssh-keygen -Y): the manifest, and an old key handing a package over to a new one.
SIG_FILE = "manifest.json.sig"
SIG_NAMESPACE = "gamenative-duo-package"
HANDOVER_NAMESPACE = "gamenative-duo-key-handover"
MAX_SOURCE = 5 * 1024 * 1024


def official_ids():
    """Package ids in OFFICIAL (one per line, '#' comments and blank lines ignored)."""
    if not OFFICIAL_FILE.is_file():
        return set()
    lines = (line.split("#", 1)[0].strip() for line in OFFICIAL_FILE.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line}


def loader_hashes():
    """sha256 -> "loader version path" for every pinned loader file."""
    if not LOADERS_FILE.is_file():
        return {}
    data = json.loads(LOADERS_FILE.read_text(encoding="utf-8"))
    return {sha: f"{l['name']} {l['version']} {path}" for l in data["loaders"] for path, sha in l["files"].items()}


def ref_sets():
    """id -> entry of refs.json."""
    if not REFS_FILE.is_file():
        return {}
    return {r["id"]: r for r in json.loads(REFS_FILE.read_text(encoding="utf-8"))["refs"]}


def is_binary(path):
    """By extension or by content: executables, libraries and archives (an archive could carry either)."""
    if Path(path).suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return any(head.startswith(m) for m in BINARY_MAGIC)


def ssh_verify(key, namespace, data, signature):
    """True when signature (armored SSHSIG) is key's signature over data in namespace."""
    with tempfile.TemporaryDirectory() as d:
        allowed, sig = Path(d) / "allowed", Path(d) / "sig"
        allowed.write_text(f"author {key}\n")
        sig.write_text(signature if signature.endswith("\n") else signature + "\n")
        try:
            r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "author", "-n", namespace, "-s", str(sig)],
                               input=data, capture_output=True)
        except OSError:
            return False
        return r.returncode == 0


def ssh_sign(private_key, namespace, data):
    """Armored SSHSIG of data by private_key (ssh-keygen asks for its passphrase if it has one)."""
    with tempfile.TemporaryDirectory() as d:
        msg = Path(d) / "msg"
        msg.write_bytes(data)
        subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(private_key), "-n", namespace, str(msg)], check=True)
        return (Path(d) / "msg.sig").read_text()


def author_chain(manifest):
    """Every key of the package, oldest first, the current one last; empty without an author."""
    author = manifest.get("author")
    if not author:
        return []
    return [h["key"] for h in author.get("history", [])] + [author["key"]]


def check_author(problems, rel, folder, manifest, base=None):
    """The manifest signature, the handover chain, and continuity with the approved version (base)."""
    author = manifest.get("author")
    base_chain = author_chain(base or {})
    if not author:
        if manifest["kind"] == "mod" and "build" in manifest:
            problems.add(rel, "a code mod needs an author key: author.key and manifest.json.sig (duo keygen, duo pack --sign)")
        if base_chain:
            problems.add(rel, "the approved version has an author key: it cannot be removed")
        if (folder / SIG_FILE).exists():
            problems.add(rel, f"{SIG_FILE} without author.key")
        return
    chain = author_chain(manifest)
    if len(set(chain)) != len(chain):
        problems.add(rel, "author.history repeats a key")
    sig = folder / SIG_FILE
    if not sig.is_file():
        problems.add(rel, f"author.key is set but {SIG_FILE} is missing (duo pack --sign)")
    elif not ssh_verify(author["key"], SIG_NAMESPACE, (folder / "manifest.json").read_bytes(), sig.read_text()):
        problems.add(rel, f"{SIG_FILE} is not author.key's signature of this manifest.json (sign again after every change)")
    for i, h in enumerate(author.get("history", [])):
        if not ssh_verify(h["key"], HANDOVER_NAMESPACE, (chain[i + 1] + "\n").encode(), h["handover"]):
            problems.add(rel, f"author.history[{i}]: the handover is not signed by that key")
    if base_chain and base_chain[-1] != chain[-1]:
        if base_chain[-1] not in chain[:-1]:
            problems.add(rel, "the author key changed without a handover from the approved key (tools/duo_index.py handover)")
        elif chain[:len(base_chain)] != base_chain:
            problems.add(rel, "author.history must keep the approved version's history and extend it")


def base_manifest(rel, ref):
    """The manifest at rel in git ref (the approved version), or None when it is new."""
    if not ref:
        return None
    r = subprocess.run(["git", "show", f"{ref}:{rel}/manifest.json"], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


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

def validate(fetch_remote=False, base_ref=None):
    """Returns (packages, problems). packages: list of (folder kind, folder, manifest)."""
    problems = Problems()
    manifest_schema = load_schema("package.schema.json")
    file_schemas = {kind: load_schema(schema) for kind, (_, schema) in MAIN_FILE.items()}
    packages, seen_ids = [], {}
    official = official_ids()

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
        if pid.startswith(RESERVED_PREFIX) and pid not in official:
            problems.add(rel, f"the '{RESERVED_PREFIX}' namespace is reserved for official packages (listed in OFFICIAL)")
        if where == "device" and kind != "device-profile":
            problems.add(rel, "only device-profile packages go in devices/")
        if where == "game":
            if kind == "device-profile":
                problems.add(rel, "device profiles go in devices/<id>/")
            elif folder.parent.name != game_key(manifest["game"]):
                problems.add(rel, f"must be in games/{game_key(manifest['game'])}/ (store-appId, lower case)")

        declared = {f["path"] for f in manifest["files"]}
        screenshots = manifest.get("screenshots", [])
        listed = declared | set(screenshots) | {"manifest.json", SIG_FILE}
        build = manifest.get("build")
        own_source = build is not None and "source" not in build

        # Every file in the folder is declared: nothing rides along unseen. A built mod's src/ is checked by check_build.
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            relpath = path.relative_to(folder).as_posix()
            if own_source and relpath.startswith(SOURCE_DIR + "/"):
                continue
            if relpath not in listed:
                problems.add(rel, f"undeclared file {path.relative_to(folder).as_posix()} (list it in files or screenshots, or remove it)")

        check_author(problems, rel, folder, manifest, base_manifest(rel, base_ref))
        loaders = loader_hashes()
        for f in manifest["files"]:
            check_file(problems, rel, folder, kind, f, fetch_remote, loaders)
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
            outputs = set((build or {}).get("outputs", {}).values())
            if build is not None:
                check_build(problems, rel, folder, build, declared)
            install = manifest["install"]
            for section in ("gameFiles", "prefixFiles"):
                for src in install.get(section, {}):
                    if not any(p == src or p.startswith(src.rstrip("/") + "/") for p in declared | outputs):
                        problems.add(rel, f"install.{section}: '{src}' matches no entry of files or build.outputs")
        packages.append((where, folder, manifest))
    for pid in sorted(official - set(seen_ids)):
        problems.add("OFFICIAL", f"'{pid}' is not a package in this repo")
    return packages, problems


def check_build(problems, rel, folder, build, declared):
    """A mod's build section: its source, its references, its outputs. CI builds it (tools/build_mods.py)."""
    src = folder / SOURCE_DIR
    clash = declared & set(build["outputs"].values())
    for path in sorted(clash):
        problems.add(rel, f"build.outputs: '{path}' is also in files (a built file comes only from the build)")
    known = ref_sets()
    for ref in build.get("refs", []):
        if ref not in known:
            problems.add(rel, f"build.refs: '{ref}' is not in refs.json")
    for name, value in build.get("properties", {}).items():
        for ref in [part.split("}", 1)[0] for part in value.split("{refs:")[1:]]:
            if ref not in build.get("refs", []):
                problems.add(rel, f"build.properties.{name}: {{refs:{ref}}} is not listed in build.refs")
    if "source" in build:
        if src.exists():
            problems.add(rel, f"build.source names a repository: remove the {SOURCE_DIR}/ folder (one source only)")
        return
    if not src.is_dir():
        problems.add(rel, f"a built mod needs its source in {SOURCE_DIR}/ (or build.source: a repository and commit)")
        return
    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    if total > MAX_SOURCE:
        problems.add(rel, f"{SOURCE_DIR}/ is {total // 1024} KB, over {MAX_SOURCE // 1024} KB")
    for path in sorted(files):
        if is_binary(path):
            problems.add(rel, f"{path.relative_to(folder).as_posix()}: binaries are not source (dependencies come from NuGet with a lock file, game assemblies from refs.json)")
    if build["engine"] == "dotnet":
        project = inside(src, build["project"])
        if project is None or not project.is_file():
            problems.add(rel, f"build.project: {SOURCE_DIR}/{build['project']} not found")
        elif "<PackageReference" in project.read_text(encoding="utf-8", errors="replace") and not (project.parent / "packages.lock.json").is_file():
            problems.add(rel, f"build.project uses NuGet packages: commit packages.lock.json next to it (RestorePackagesWithLockFile)")
    else:
        for out in build["outputs"]:
            target = inside(src, out)
            if target is None or not target.is_file():
                problems.add(rel, f"build.outputs: {SOURCE_DIR}/{out} not found")
            elif Path(out).suffix.lower() not in SCRIPT_EXTENSIONS:
                problems.add(rel, f"build.outputs: {out}: a scripts mod ships only {', '.join(sorted(SCRIPT_EXTENSIONS))}")


def check_file(problems, rel, folder, kind, f, fetch_remote, loaders=None):
    path, expected = f["path"], f["sha256"]
    ext = Path(path).suffix.lower()
    loaders = loaders if loaders is not None else loader_hashes()
    pinned = expected in loaders
    if kind == "mod" and ext in BINARY_EXTENSIONS and not pinned:
        problems.add(rel, f"{path}: binaries come from the build (build in the manifest), or must be a pinned loader file (loaders.json)")
        return
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
                elif kind == "mod" and not pinned and any(data.startswith(m) for m in BINARY_MAGIC):
                    problems.add(rel, f"{path}: {f['url']} is a binary: binaries come from the build or loaders.json")
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
    elif kind == "mod" and not pinned and is_binary(target):
        problems.add(rel, f"{path}: binaries come from the build (build in the manifest), or must be a pinned loader file (loaders.json)")


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


def build(out, base_url, signing_key, ratings=None, built_dir=None):
    packages, problems = validate(fetch_remote=True)
    if problems:
        report(problems)
        sys.exit(1)
    built = json.loads((built_dir / "built.json").read_text()) if built_dir else {}
    for _, _, m in packages:
        if m["kind"] == "mod" and "build" in m:
            b = built.get(m["id"])
            if b is None or b["packageVersion"] != m["packageVersion"]:
                problems.add(m["id"], "not built for this version (run tools/build_mods.py and pass --built)")
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
    official = official_ids()
    for where, folder, m in packages:
        entry = {k: v for k, v in m.items() if k not in ("files", "screenshots", "$schema", "format", "version", "author")}
        if author_chain(m):
            # Checked by validate (signature, handovers, continuity): the app pins authorKey and accepts any of authorKeys.
            entry["authorKey"] = m["author"]["key"]
            entry["authorKeys"] = author_chain(m)
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
        if m["id"] in built:
            # CI-built outputs (tools/build_mods.py, a job without secrets): the only code the index lists.
            for path, o in sorted(built[m["id"]]["outputs"].items()):
                digest, url, size = blob(built_dir / "files" / o["sha256"])
                if digest != o["sha256"]:
                    sys.exit(f"{m['id']}: built file {path} does not match built.json")
                files.append({"path": path, "sha256": digest, "url": url, "size": size})
            entry["builtFrom"] = built[m["id"]]["source"]
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
            "official": sorted(e["id"] for e in entries if e["id"] in official),
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


def public_key_line(path):
    """'ssh-ed25519 AAAA...' from a .pub file (the comment dropped)."""
    parts = Path(path).read_text().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        sys.exit(f"{path}: not an ssh-ed25519 public key")
    return f"{parts[0]} {parts[1]}"


def sign(folder, private_key):
    """Writes manifest.json.sig; sets author.key from the key's .pub when the manifest has none."""
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    key = public_key_line(str(private_key) + ".pub")
    current = (manifest.get("author") or {}).get("key")
    if current is None:
        manifest["author"] = {"key": key}
        path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    elif current != key:
        sys.exit(f"{private_key} is not author.key: sign with the package's key, or hand it over first (handover)")
    (folder / SIG_FILE).write_text(ssh_sign(private_key, SIG_NAMESPACE, path.read_bytes()))
    print(f"Signed {path} ({SIG_FILE})")


def handover(folder, old_key, new_pub):
    """The current key signs the new key; the new key becomes author.key. Sign again with the new key afterwards."""
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    author = manifest.get("author") or sys.exit("the package has no author.key: just sign it (sign)")
    if public_key_line(str(old_key) + ".pub") != author["key"]:
        sys.exit(f"{old_key} is not the current author.key")
    new = public_key_line(new_pub)
    signature = ssh_sign(old_key, HANDOVER_NAMESPACE, (new + "\n").encode())
    author.setdefault("history", []).append({"key": author["key"], "handover": signature})
    author["key"] = new
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Handed {manifest['id']} over to the new key. Now sign it with the new key: duo_index.py sign {folder} --key <new private key>")


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
    v.add_argument("--base-ref", help="git ref of the approved state (a PR's base): author keys must continue from it")
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, default=ROOT / "site")
    b.add_argument("--base-url", default="https://nono5822.github.io/GameNativeDuo-community")
    b.add_argument("--ratings", type=Path, help="ratings.json from tools/ratings.py")
    b.add_argument("--built", type=Path, help="output folder of tools/build_mods.py (needed when mods have a build)")
    r = sub.add_parser("verify")
    r.add_argument("--site", type=Path, default=ROOT / "site")
    r.add_argument("--public-key", help="base64 public key to check against (default: the site's own)")
    h = sub.add_parser("rehash")
    h.add_argument("package", type=Path, help="package folder")
    g = sub.add_parser("sign", help="sign a package's manifest.json with the author's SSH key (after rehash)")
    g.add_argument("package", type=Path)
    g.add_argument("--key", type=Path, required=True, help="private key file (its .pub must be author.key)")
    o = sub.add_parser("handover", help="hand a package over to a new key, signed by the current one")
    o.add_argument("package", type=Path)
    o.add_argument("--old-key", type=Path, required=True, help="private key file of the current author.key")
    o.add_argument("--new-key", type=Path, required=True, help="public key file (.pub) of the new key")
    sub.add_parser("keygen")
    args = parser.parse_args()

    if args.command == "validate":
        packages, problems = validate(args.fetch_remote, args.base_ref)
        if problems:
            report(problems)
            sys.exit(1)
        print(f"OK: {len(packages)} package(s)")
    elif args.command == "build":
        ratings = json.loads(args.ratings.read_text()) if args.ratings and args.ratings.is_file() else None
        build(args.out, args.base_url, load_private_key(), ratings, args.built)
    elif args.command == "verify":
        verify(args.site, args.public_key)
    elif args.command == "rehash":
        rehash(args.package)
    elif args.command == "sign":
        sign(args.package, args.key)
    elif args.command == "handover":
        handover(args.package, args.old_key, args.new_key)
    elif args.command == "keygen":
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print("private (secret DUO_INDEX_SIGNING_KEY):", base64.b64encode(raw).decode())
        print("public  (pin in the app):              ", base64.b64encode(pub).decode())


if __name__ == "__main__":
    main()

"""python tools/test_code_trust.py (also run by validate.yml): source packages, pinned loaders, no stray binaries."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import duo_index  # noqa: E402

CSPROJ = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net46</TargetFramework></PropertyGroup></Project>'


class SourcePackages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "me.mod"
        (self.folder / "src").mkdir(parents=True)
        self.original = (duo_index.LOADERS_FILE, duo_index.REFS_FILE)
        duo_index.REFS_FILE = Path(self.tmp.name) / "refs.json"
        duo_index.REFS_FILE.write_text(json.dumps({"refs": [{"id": "unity-5.6", "sha256": "0" * 64}]}))
        duo_index.LOADERS_FILE = Path(self.tmp.name) / "loaders.json"
        duo_index.LOADERS_FILE.write_text(json.dumps({"loaders": []}))

    def tearDown(self):
        duo_index.LOADERS_FILE, duo_index.REFS_FILE = self.original
        self.tmp.cleanup()

    def problems(self, build, declared=()):
        p = duo_index.Problems()
        duo_index.check_build(p, "me.mod", self.folder, build, set(declared))
        return p.items

    def dotnet(self, **extra):
        return {"engine": "dotnet", "project": "Mod.csproj", "outputs": {"bin/Release/Mod.dll": "plugin/Mod.dll"}, **extra}

    def test_folder_source_with_project_is_fine(self):
        (self.folder / "src/Mod.csproj").write_text(CSPROJ)
        (self.folder / "src/Plugin.cs").write_text("class P {}")
        self.assertEqual(self.problems(self.dotnet(refs=["unity-5.6"], properties={"ManagedPath": "{refs:unity-5.6}"})), [])

    def test_missing_project_and_unknown_refs(self):
        items = self.problems(self.dotnet(refs=["nope"], properties={"X": "{refs:other}"}))
        self.assertTrue(any("not in refs.json" in i for i in items))
        self.assertTrue(any("not listed in build.refs" in i for i in items))
        self.assertTrue(any("Mod.csproj not found" in i for i in items))

    def test_binaries_in_source_are_refused(self):
        (self.folder / "src/Mod.csproj").write_text(CSPROJ)
        (self.folder / "src/lib").mkdir()
        (self.folder / "src/lib/Helper.dll").write_bytes(b"MZ\x90\x00")
        (self.folder / "src/innocent.txt").write_bytes(b"MZ\x90\x00 really a PE file")
        items = self.problems(self.dotnet())
        self.assertEqual(sum("binaries are not source" in i for i in items), 2)

    def test_nuget_needs_a_lock_file(self):
        (self.folder / "src/Mod.csproj").write_text(CSPROJ.replace("</Project>", '<ItemGroup><PackageReference Include="X" Version="1.0" /></ItemGroup></Project>'))
        self.assertTrue(any("packages.lock.json" in i for i in self.problems(self.dotnet())))
        (self.folder / "src/packages.lock.json").write_text("{}")
        self.assertEqual(self.problems(self.dotnet()), [])

    def test_repository_source_excludes_a_src_folder(self):
        build = self.dotnet(source={"repo": "https://github.com/me/mod", "commit": "a" * 40})
        self.assertTrue(any("one source only" in i for i in self.problems(build)))
        (self.folder / "src").rmdir()
        self.assertEqual(self.problems(build), [])

    def test_outputs_may_not_shadow_files(self):
        (self.folder / "src/Mod.csproj").write_text(CSPROJ)
        self.assertTrue(any("also in files" in i for i in self.problems(self.dotnet(), declared={"plugin/Mod.dll"})))

    def test_scripts_ship_only_text(self):
        (self.folder / "src/main.lua").write_text("return {}")
        ok = {"engine": "scripts", "outputs": {"main.lua": "mod/main.lua"}}
        self.assertEqual(self.problems(ok), [])
        bad = {"engine": "scripts", "outputs": {"main.lua": "mod/main.lua", "missing.lua": "mod/missing.lua"}}
        self.assertTrue(any("missing.lua not found" in i for i in self.problems(bad)))


class Binaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.dll = self.folder / "winmm.dll"
        self.dll.write_bytes(b"MZ loader bytes")
        self.sha = hashlib.sha256(self.dll.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, f, loaders, kind="mod"):
        p = duo_index.Problems()
        duo_index.check_file(p, "pkg", self.folder, kind, f, False, loaders)
        return p.items

    def test_pinned_loader_file_is_allowed(self):
        self.assertEqual(self.check({"path": "winmm.dll", "sha256": self.sha}, {self.sha: "Lovely 0.10.0 winmm.dll"}), [])

    def test_unpinned_binary_is_refused(self):
        self.assertTrue(any("binaries come from the build" in i for i in self.check({"path": "winmm.dll", "sha256": self.sha}, {})))

    def test_renamed_binary_is_refused_by_content(self):
        (self.folder / "notes.txt").write_bytes(self.dll.read_bytes())
        self.assertTrue(any("binaries come from the build" in i for i in self.check({"path": "notes.txt", "sha256": self.sha}, {})))

    def test_binary_release_asset_needs_a_pin(self):
        f = {"path": "big.dll", "sha256": self.sha, "url": "https://github.com/nono5822/GameNativeDuo-community/releases/download/x/big.dll"}
        self.assertTrue(any("binaries come from the build" in i for i in self.check(f, {})))

    def test_real_loaders_file_pins_bepinex_and_lovely(self):
        names = set(duo_index.loader_hashes().values())
        self.assertTrue(any(n.startswith("BepInEx 5.4.23.5 winhttp.dll") for n in names))
        self.assertTrue(any(n.startswith("Lovely Injector 0.10.0 winmm.dll") for n in names))


if __name__ == "__main__":
    unittest.main()

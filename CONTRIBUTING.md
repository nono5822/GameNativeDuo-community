# Contributing a package

## Share a UI layout

1. In the game, hold two fingers on the bottom screen → **Edit UI layout**, and arrange the UI.
2. Get the layout the editor saved. The app keeps it in its private storage as `files/duo/ui/<STORE>_<appId>.json`;
   copy it with adb: `adb exec-out run-as app.gamenative.duo cat files/duo/ui/STEAM_433340.json > ui-layout.json`.
   A "Share layout" button in the app is planned.
3. Make a folder `games/<store>-<appId>/<your-name>.<layout-name>/` (lower case, e.g.
   `games/steam-433340/alex.compact-hud/`) and put the file in it as `ui-layout.json`.
4. Add a `manifest.json` (copy one from another package and edit it):

   ```json
   {
     "$schema": "../../../schema/package.schema.json",
     "format": "gamenative-duo/package",
     "version": 1,
     "id": "alex.compact-hud",
     "kind": "ui-layout",
     "packageVersion": "1.0.0",
     "game": { "store": "steam", "appId": "433340", "title": "Slime Rancher", "builds": ["1.4.4"] },
     "requires": { "adapter": "unity-ugui>=0.2" },
     "testedOn": ["ayn-thor"],
     "title": "Compact HUD",
     "description": "Hotbar in the middle, health and energy in the corner, crosshair on top.",
     "authors": ["alex"],
     "githubLogins": ["alex-gh"],
     "license": "CC0-1.0",
     "screenshots": ["top.jpg", "bottom.jpg"],
     "files": [{ "path": "ui-layout.json", "sha256": "" }]
   }
   ```

5. Screenshots are welcome: one of each screen, under 1 MB each (`adb exec-out screencap -p -d <display id>`).
6. Fill in the hashes and check everything:

   ```bash
   pip install -r tools/requirements.txt
   python tools/duo_index.py rehash games/steam-433340/alex.compact-hud
   python tools/duo_index.py validate
   ```

7. Open a pull request. CI runs the same check.

## Other kinds

- **`duo-settings`**: a `duo-settings.json` (schema: `schema/duo-settings.schema.json`).
- **`device-profile`**: goes in `devices/<your-name>.<device>/` with a `device-profile.json`; no `game`. Get the real
  screen data from the app's `Android/data/app.gamenative.duo/files/duo/displays.json` after running any game.
- **`mod`**: code. Needs `requires.adapter` and an `install` map (where each file goes, plus any Wine DLL overrides).
  **Code is built from source by CI, never uploaded as a binary.** Put the source in the package's `src/` folder, or
  name a public repository and a full commit hash in `build.source`, and describe the build:

  ```json
  "build": {
    "engine": "dotnet",
    "project": "MyMod.csproj",
    "refs": ["hollow-knight-1.5.78"],
    "properties": { "ManagedPath": "{refs:hollow-knight-1.5.78}" },
    "outputs": { "bin/Release/net472/MyMod.dll": "plugin/MyMod.dll" }
  }
  ```

  `outputs` maps what the build produces to the package paths `install` uses; CI builds the mod in a clean runner and
  the index lists only the files it built (`tools/build_mods.py`). Game and engine assemblies come from `refs.json`
  (reference stubs: public API only, never game files). NuGet packages need a committed `packages.lock.json`. A Lua
  mod uses `"engine": "scripts"`: its source files are the mod. The only binaries a package may contain are the pinned
  loader files in `loaders.json` (BepInEx, Lovely). A maintainer reviews the code before merge. State the licence of
  everything you include.

## Updating a package

Change its files, bump `packageVersion` (semantic versioning), add a `changelog` entry (`{version, date, notes}`,
newest first; the app shows the newest five), run `rehash` and `validate`, open a pull request.
Keep the `id`: the app tracks updates by it.

## Ratings and reports

Every package gets a discussion; the nightly publish folds its 👍/👎 into the index with these rules
(`tools/rating_rules.py`, unit-tested in CI):

- one vote per GitHub account: an account with both 👍 and 👎 counts as neither;
- the package's own authors (`githubLogins`; the share bot fills it in) and bots are not counted;
- compatibility reports (the "Compatibility report" issue form, opened pre-filled from the app) count once per
  account and device for the package's current version, newest report wins. An update starts from zero.

Package ids starting with `gamenative-duo.` are reserved for official packages: they must be listed in `OFFICIAL`,
which only maintainers change. The app shows the official badge only for those.

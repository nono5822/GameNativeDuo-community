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
  Files up to 20 MB live in the folder; bigger ones are attached to a release of this repo and referenced by `url`
  and `sha256`. A maintainer reviews the code before merge. State the licence of everything you include.

## Updating a package

Change its files, bump `packageVersion` (semantic versioning), run `rehash` and `validate`, open a pull request.
Keep the `id`: the app tracks updates by it.

# GameNative Duo community packages

Dual-screen layouts, settings, device profiles and mods for [GameNative Duo](https://github.com/nono5822/GameNativeDuo),
shared by the community. The app lists them on each game's page; a tap applies one.

| Kind | What it is | In the app |
|---|---|---|
| `ui-layout` | Which UI element goes on which screen, its size and position: the file the in-game layout editor saves | Applied in one tap |
| `duo-settings` | A game's GameNative Duo settings (screen split, native touch…) | Applied in one tap |
| `device-profile` | How a dual-screen handheld's screens are laid out and lined up | Applied in one tap |
| `mod` | Code that makes a game dual-screen (BepInEx plugin, SMAPI mod, Lovely mod…) | Asks first; reviewed before merge |

## How it works

```
games/<store>-<appId>/<author>.<name>/   one package for one game: manifest.json + its files
devices/<author>.<name>/                 one device profile
schema/                                  JSON Schemas for every file
tools/duo_index.py                       validator and index builder
```

Every pull request is checked by CI (schemas, file hashes, sizes, paths, licences; layouts and settings may not contain
code). Each merge into `main` compiles the packages into a signed index on GitHub Pages:

- `https://nono5822.github.io/GameNativeDuo-community/v1/catalog.json`: every game with packages
- `…/v1/games/<store>-<appId>.json`: one game's packages
- `…/v1/blobs/<sha256>`: the files, content-addressed

Each JSON file has an ed25519 signature next to it (`.sig`). The app only trusts files signed with the key in
[`keys/index-public-key.txt`](keys/index-public-key.txt), and every file it downloads must match its sha256.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Sharing a layout takes a manifest and the `ui-layout.json` the editor saved.

## Rules

- Only for games you own. Never game files, never anything that touches DRM.
- Every package states its licence; mods are redistributed only when their licence allows it.
- Code (`mod` packages) runs inside the game with the app's permissions, so it is reviewed by a maintainer before
  merge.

Format details for adapter authors: [engine adapter contract](https://github.com/nono5822/GameNativeDuo/blob/duo/slime-rancher/duo/docs/engine-adapter-contract.md),
[community packages design](https://github.com/nono5822/GameNativeDuo/blob/duo/slime-rancher/duo/docs/community-packages.md).

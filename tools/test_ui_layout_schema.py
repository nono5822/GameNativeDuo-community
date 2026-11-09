"""python tools/test_ui_layout_schema.py: what the app's layout editors save must validate, mistakes must not."""

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schema" / "ui-layout.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


def layout(**extra):
    return {"format": "gamenative-duo/ui-layout", "version": 1, "game": "STEAM_413150", "elements": {}, **extra}


def ok(doc):
    return not list(VALIDATOR.iter_errors(doc))


class UiLayoutSchema(unittest.TestCase):
    def test_hotkeys_only_layout_is_valid(self):
        # What the app writes for a game without a dual-screen mod: no elements, just hotkeys.
        doc = layout(hotkeys={
            "columns": 3, "size": "m", "preset": "stardew",
            "buttons": [
                {"label": "Map", "icon": "🗺", "keys": "M"},
                {"label": "Run", "keys": "Shift", "mode": "toggle"},
                {"label": "Aim", "mouse": "right", "mode": "hold"},
                {"label": "Pause", "pad": "start", "span": 2},
                {"label": "Next", "command": "sdv.toolbar.next", "keys": "Tab"},
            ],
        })
        self.assertTrue(ok(doc), list(VALIDATOR.iter_errors(doc)))

    def test_background_widgets_and_options_are_valid(self):
        doc = layout(
            elements={"Hud/Map": {"screen": "bottom", "scale": 1.2, "options": {"zoom": "knight"}}},
            background={"mode": "color", "color": "#101322", "dim": 0.3, "blur": 0.5},
            widgets=[{"type": "fps", "x": 0.03, "y": 0.04}, {"type": "battery", "x": 0.8, "y": 0.9, "scale": 1.5}],
        )
        self.assertTrue(ok(doc), list(VALIDATOR.iter_errors(doc)))

    def test_empty_layout_is_refused(self):
        self.assertFalse(ok(layout()))

    def test_bad_hotkeys_are_refused(self):
        def button(**b):
            return layout(hotkeys={"buttons": [b]})

        self.assertFalse(ok(button(label="Two actions", keys="M", mouse="left")))
        self.assertFalse(ok(button(label="Nothing")))
        self.assertFalse(ok(button(label="Bad", command="Not A Command")))
        self.assertFalse(ok(button(label="Bad", pad="lt")))
        self.assertFalse(ok(button(label="Bad", keys="M", mode="sometimes")))
        self.assertFalse(ok(layout(hotkeys={"columns": 9, "buttons": []})))

    def test_shared_images_are_refused(self):
        # Images stay private: the app shares them as a color.
        self.assertFalse(ok(layout(background={"mode": "image", "image": "background.jpg"})))


if __name__ == "__main__":
    unittest.main()

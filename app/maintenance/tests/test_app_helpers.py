import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app


class AppHelperTests(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(app.sanitize_filename('  bad:name?.MP3  '), "bad_name_")
        self.assertEqual(app.sanitize_filename("CON"), "CON_")
        self.assertEqual(len(app.sanitize_filename("a" * 250)), 180)

    def test_progress_and_duration_formatting(self):
        self.assertEqual(app.parse_percent(" 42.5% "), 42.5)
        self.assertEqual(app.parse_percent("not available"), 0)
        self.assertEqual(app.format_duration(65), "1:05")
        self.assertEqual(app.format_duration(3661), "1:01:01")

    def test_format_accent_palettes(self):
        self.assertEqual(
            app.format_accent_palette("mp3"),
            (app.BLUE, app.BLUE_DARK, app.BLUE_HOVER, app.BLUE_HOVER_DARK),
        )
        self.assertEqual(
            app.format_accent_palette("mp4"),
            (app.RED, app.RED_DARK, app.RED_HOVER, app.RED_HOVER_DARK),
        )

    def test_accent_interpolation_is_clamped_and_reaches_both_ends(self):
        self.assertEqual(app.interpolate_hex_color("#000000", "#FFFFFF", -1), "#000000")
        self.assertEqual(app.interpolate_hex_color("#000000", "#FFFFFF", 0.5), "#808080")
        self.assertEqual(app.interpolate_hex_color("#000000", "#FFFFFF", 2), "#FFFFFF")
        self.assertEqual(
            app.interpolate_accent_palette(
                app.format_accent_palette("mp3"),
                app.format_accent_palette("mp4"),
                1,
            ),
            app.format_accent_palette("mp4"),
        )

    def test_history_pruning(self):
        now = datetime(2026, 7, 27, tzinfo=timezone.utc)
        recent = (now - timedelta(days=2)).isoformat()
        old = (now - timedelta(days=31)).isoformat()
        entries = [
            {"title": "recent", "created_at": recent},
            {"title": "old", "created_at": old},
            "invalid",
        ]
        self.assertEqual(
            [entry["title"] for entry in app.prune_history_entries(entries, now)],
            ["recent"],
        )

    def test_protected_video_guidance_distinguishes_age_gate_from_locked_browser(self):
        self.assertIn("Login fallback", app.protected_video_guidance("Off"))
        locked = app.protected_video_guidance("Brave", cookie_unavailable=True)
        self.assertIn("Fully close Brave", locked)
        self.assertIn("choose Brave again", locked)

    def test_legacy_browser_setting_resets_to_optional_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            settings_path = Path(folder) / "settings.json"
            settings_path.write_text(
                json.dumps({"use_brave_cookies": True}),
                encoding="utf-8",
            )
            with patch.object(app, "SETTINGS_PATH", settings_path):
                settings = app.load_settings()
        self.assertEqual(settings["youtube_browser"], "Off")
        self.assertEqual(settings["settings_version"], 2)
        self.assertNotIn("use_brave_cookies", settings)

    def test_removed_browser_setting_resets_to_off(self):
        with tempfile.TemporaryDirectory() as folder:
            settings_path = Path(folder) / "settings.json"
            settings_path.write_text(
                json.dumps({"settings_version": 2, "youtube_browser": "Vivaldi"}),
                encoding="utf-8",
            )
            with patch.object(app, "SETTINGS_PATH", settings_path):
                settings = app.load_settings()
        self.assertEqual(settings["youtube_browser"], "Off")

    def test_settings_are_saved_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            settings_dir = Path(folder) / "data"
            settings_path = settings_dir / "settings.json"
            with (
                patch.object(app, "SETTINGS_DIR", settings_dir),
                patch.object(app, "SETTINGS_PATH", settings_path),
            ):
                app.save_settings({"theme": "Dark"})
            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8")),
                {"theme": "Dark"},
            )
            self.assertFalse(settings_path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()

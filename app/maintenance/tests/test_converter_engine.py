import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import converter_engine
from converter_engine import (
    BrowserCookieError,
    ConverterEngine,
    DownloadRequest,
    EngineLog,
    EngineUpdateError,
    engine_update_commands,
    friendly_error,
    inspect_engine,
    is_cookie_error,
    is_login_error,
    is_transient_error,
    is_youtube_video_url,
    run_engine_update,
)


class FakeYoutubeDL:
    failures = []
    options_seen = []

    def __init__(self, options):
        self.options = options
        self.__class__.options_seen.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download):
        if self.__class__.failures:
            failure = self.__class__.failures.pop(0)
            if failure is not None:
                raise failure
        if download:
            template = self.options["outtmpl"]
            final_path = Path(template.replace("%(ext)s", "mp3"))
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"test")
        return {"title": "Test title"}

    def prepare_filename(self, _info):
        return self.options["outtmpl"].replace("%(ext)s", "webm")


class ConverterEngineTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.failures = []
        FakeYoutubeDL.options_seen = []
        self.fake_module = SimpleNamespace(YoutubeDL=FakeYoutubeDL)

    def test_engine_health_has_all_required_components(self):
        health = inspect_engine()
        self.assertTrue(health.ready, health.details())

    def test_error_classification_and_messages(self):
        login = "ERROR: Sign in to confirm you’re not a bot"
        self.assertTrue(is_login_error(login))
        self.assertIn("blocked this anonymous request", friendly_error(login, "Off"))
        age_gate = "ERROR: Sign in to confirm your age"
        self.assertTrue(is_login_error(age_gate))
        self.assertIn("Login fallback", friendly_error(age_gate, "Off"))
        self.assertTrue(is_cookie_error("ERROR: Could not copy Chrome cookie database"))
        self.assertTrue(is_transient_error("ERROR: HTTP Error 403: Forbidden"))

    def test_youtube_video_url_validation(self):
        valid = (
            "https://www.youtube.com/watch?v=RqJVa0fl01w",
            "https://youtu.be/RqJVa0fl01w?t=4",
            "https://www.youtube.com/shorts/RqJVa0fl01w",
            "https://music.youtube.com/watch?v=RqJVa0fl01w",
        )
        invalid = (
            "https://youtube.com/",
            "https://youtube.com/playlist?list=test",
            "https://example.com/watch?v=RqJVa0fl01w",
            "not a URL",
        )
        self.assertTrue(all(is_youtube_video_url(url) for url in valid))
        self.assertFalse(any(is_youtube_video_url(url) for url in invalid))

    def test_download_retries_temporary_403_errors(self):
        FakeYoutubeDL.failures = [
            RuntimeError("HTTP Error 403"),
            RuntimeError("HTTP Error 403"),
            None,
        ]
        statuses = []
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = Path(folder) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"test")
            engine = ConverterEngine(Path(folder) / "data")
            request = DownloadRequest(
                url="https://www.youtube.com/watch?v=test",
                media_format="mp3",
                target_dir=Path(folder) / "output",
                custom_name="clip",
                quality="192",
            )
            with (
                patch.object(engine, "_load_yt_dlp", return_value=self.fake_module),
                patch.object(converter_engine, "find_ffmpeg", return_value=ffmpeg),
            ):
                result = engine.download(request, "Off", lambda _data: None, statuses.append)

        self.assertEqual(result.title, "clip")
        self.assertEqual(result.output_path.name, "clip.mp3")
        self.assertEqual(len(statuses), 2)

    def test_cookie_read_failure_falls_back_to_anonymous(self):
        class CookieAwareYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download):
                if "cookiesfrombrowser" in self.options:
                    raise RuntimeError("ERROR: Failed to load cookies")
                return super().extract_info(url, download)

        fake_module = SimpleNamespace(YoutubeDL=CookieAwareYoutubeDL)
        statuses = []
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = Path(folder) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"test")
            engine = ConverterEngine(Path(folder) / "data")
            request = DownloadRequest(
                url="https://www.youtube.com/watch?v=test",
                media_format="mp3",
                target_dir=Path(folder) / "output",
                custom_name="clip",
                quality="192",
            )
            with (
                patch.object(engine, "_load_yt_dlp", return_value=fake_module),
                patch.object(converter_engine, "find_ffmpeg", return_value=ffmpeg),
            ):
                result = engine.download(request, "Brave", lambda _data: None, statuses.append)

        self.assertEqual(result.output_path.name, "clip.mp3")
        self.assertTrue(any("Retrying anonymously" in status for status in statuses))

    def test_locked_browser_cookies_are_preserved_when_anonymous_preview_needs_login(self):
        FakeYoutubeDL.failures = [
            RuntimeError("ERROR: Could not copy Chrome cookie database"),
            RuntimeError("ERROR: Sign in to confirm your age"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            engine = ConverterEngine(Path(folder) / "data")
            with patch.object(engine, "_load_yt_dlp", return_value=self.fake_module):
                with self.assertRaises(BrowserCookieError) as raised:
                    engine.fetch_metadata(
                        "https://www.youtube.com/watch?v=test",
                        "Brave",
                    )

        self.assertEqual(raised.exception.browser_name, "Brave")
        self.assertIn("Failed to load cookies from Brave", str(raised.exception))
        self.assertIn("cookiesfrombrowser", FakeYoutubeDL.options_seen[0])
        self.assertNotIn("cookiesfrombrowser", FakeYoutubeDL.options_seen[1])

    def test_locked_browser_cookies_are_preserved_when_anonymous_download_needs_login(self):
        FakeYoutubeDL.failures = [
            RuntimeError("ERROR: Failed to load cookies"),
            RuntimeError("ERROR: Sign in to confirm your age"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = Path(folder) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"test")
            engine = ConverterEngine(Path(folder) / "data")
            request = DownloadRequest(
                url="https://www.youtube.com/watch?v=test",
                media_format="mp3",
                target_dir=Path(folder) / "output",
                custom_name="clip",
                quality="192",
            )
            with (
                patch.object(engine, "_load_yt_dlp", return_value=self.fake_module),
                patch.object(converter_engine, "find_ffmpeg", return_value=ffmpeg),
            ):
                with self.assertRaises(BrowserCookieError):
                    engine.download(request, "Brave", lambda _data: None, lambda _message: None)

    def test_existing_custom_output_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = Path(folder) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"test")
            output = Path(folder) / "output"
            output.mkdir()
            (output / "clip.mp3").write_bytes(b"existing")
            engine = ConverterEngine(Path(folder) / "data")
            request = DownloadRequest(
                url="https://www.youtube.com/watch?v=test",
                media_format="mp3",
                target_dir=output,
                custom_name="clip",
                quality="192",
            )
            with patch.object(converter_engine, "find_ffmpeg", return_value=ffmpeg):
                with self.assertRaisesRegex(FileExistsError, "already exists"):
                    engine.download(request, "Off", lambda _data: None, lambda _message: None)

    def test_engine_update_uses_local_python_and_expected_packages(self):
        python = Path("runtime") / "python.exe"
        commands = engine_update_commands(python)
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0][1][0].endswith("runtime\\python.exe"))
        self.assertIn("imageio-ffmpeg", commands[0][1])
        self.assertIn("yt-dlp[default,deno,curl-cffi]", commands[1][1])
        self.assertIn("--pre", commands[1][1])

    def test_engine_update_reports_success_and_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "python.exe"
            python.write_bytes(b"test")
            statuses = []
            log = EngineLog(root / "logs")
            healthy = SimpleNamespace(ready=True, details=lambda: "healthy")
            completed = SimpleNamespace(returncode=0, stdout="updated", stderr="")
            with (
                patch.object(converter_engine.subprocess, "run", return_value=completed) as runner,
                patch.object(converter_engine, "inspect_engine", return_value=healthy),
            ):
                result = run_engine_update(python, root, log, statuses.append)
            self.assertIs(result, healthy)
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(statuses[-1], "Checking updated engine...")

            failed = SimpleNamespace(returncode=1, stdout="", stderr="network unavailable")
            with patch.object(converter_engine.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(EngineUpdateError, "network unavailable"):
                    run_engine_update(python, root, log, statuses.append)


if __name__ == "__main__":
    unittest.main()

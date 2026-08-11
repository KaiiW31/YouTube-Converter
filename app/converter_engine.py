from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


BROWSER_COOKIE_OPTIONS = {
    "Off": None,
    "Brave": "brave",
    "Chrome": "chrome",
    "Microsoft Edge": "edge",
    "Firefox": "firefox",
    "Opera": "opera",
}

TRANSIENT_ERROR_MARKERS = (
    "http error 403",
    "http error 429",
    "read timed out",
    "remote end closed connection",
    "temporary failure",
    "unable to download video data",
    "the read operation timed out",
)

COOKIE_ERROR_MARKERS = (
    "failed to load cookies",
    "could not copy",
    "could not decrypt",
    "cookie database",
)

LOGIN_ERROR_MARKERS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm your age",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "login_required",
)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class DownloadCancelled(Exception):
    """Raised from a yt-dlp progress hook when the user cancels a job."""


class BrowserCookieError(RuntimeError):
    """Raised when protected media needs browser cookies that could not be read."""

    def __init__(self, browser_name: str, cookie_error: Exception):
        self.browser_name = browser_name
        self.cookie_error = cookie_error
        super().__init__(
            f"Failed to load cookies from {browser_name}. {clean_error(str(cookie_error))}"
        )


class EngineUpdateError(RuntimeError):
    """Raised when the local conversion engine cannot be updated."""


@dataclass(frozen=True)
class EngineHealth:
    yt_dlp_version: str
    deno_version: str
    ejs_version: str
    ffmpeg_path: str
    curl_cffi_version: str
    ready: bool
    problems: tuple[str, ...]

    def summary(self) -> str:
        if self.ready:
            return "Engine ready"
        return "Engine needs repair"

    def details(self) -> str:
        lines = [
            f"yt-dlp: {self.yt_dlp_version or 'missing'}",
            f"Deno: {self.deno_version or 'missing'}",
            f"EJS challenge solver: {self.ejs_version or 'missing'}",
            f"FFmpeg: {self.ffmpeg_path or 'missing'}",
            f"Browser networking: {self.curl_cffi_version or 'missing'}",
        ]
        if self.problems:
            lines.extend(("", "Problems:", *(f"- {problem}" for problem in self.problems)))
        else:
            lines.extend(("", "All required conversion components are available."))
        return "\n".join(lines)


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    media_format: str
    target_dir: Path
    custom_name: str
    quality: str


@dataclass(frozen=True)
class DownloadResult:
    title: str
    output_path: Path


def package_version(distribution_name: str) -> str:
    try:
        return importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def find_deno() -> Path | None:
    bundle_dir = Path(getattr(sys, "_MEIPASS", ""))
    bundled_path = bundle_dir / "deno.exe" if bundle_dir else None
    if bundled_path is not None and bundled_path.is_file():
        return bundled_path
    try:
        import deno

        path = Path(deno.find_deno_bin())
        return path if path.is_file() else None
    except Exception:
        path = shutil.which("deno")
        return Path(path) if path else None


def find_ffmpeg() -> Path | None:
    try:
        import imageio_ffmpeg

        path = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if path.is_file():
            return path
    except Exception:
        pass

    path = shutil.which("ffmpeg")
    return Path(path) if path else None


def inspect_engine() -> EngineHealth:
    yt_dlp_version = package_version("yt-dlp")
    deno_version = package_version("deno")
    ejs_version = package_version("yt-dlp-ejs")
    curl_cffi_version = package_version("curl-cffi")
    ffmpeg = find_ffmpeg()
    problems = []
    if not yt_dlp_version:
        problems.append("The yt-dlp download engine is missing.")
    if not deno_version or find_deno() is None:
        problems.append("The Deno JavaScript runtime is missing.")
    if not ejs_version:
        problems.append("The YouTube EJS challenge solver is missing.")
    if ffmpeg is None:
        problems.append("FFmpeg is missing.")
    if not curl_cffi_version:
        problems.append("Browser-compatible networking support is missing.")
    return EngineHealth(
        yt_dlp_version=yt_dlp_version,
        deno_version=deno_version,
        ejs_version=ejs_version,
        ffmpeg_path=str(ffmpeg or ""),
        curl_cffi_version=curl_cffi_version,
        ready=not problems,
        problems=tuple(problems),
    )


def engine_update_commands(python_executable: Path) -> tuple[tuple[str, list[str]], ...]:
    python = str(Path(python_executable).resolve())
    shared = [
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
    ]
    return (
        ("Updating FFmpeg support...", [*shared, "imageio-ffmpeg"]),
        (
            "Updating YouTube compatibility...",
            [*shared, "--pre", "yt-dlp[default,deno,curl-cffi]"],
        ),
    )


def run_engine_update(
    python_executable: Path,
    working_directory: Path,
    log: "EngineLog",
    status_hook: Callable[[str], None],
) -> EngineHealth:
    python = Path(python_executable).resolve()
    if not python.is_file():
        raise EngineUpdateError(f"The local Python runtime is missing: {python}")

    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

    for status, command in engine_update_commands(python):
        status_hook(status)
        log.info(f"Engine update step: {status}")
        try:
            completed = subprocess.run(
                command,
                cwd=str(Path(working_directory).resolve()),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                creationflags=creation_flags,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            log.error(f"Engine update timed out: {exc}")
            raise EngineUpdateError(
                "The update took longer than 15 minutes and was stopped."
            ) from exc
        except OSError as exc:
            log.error(f"Could not start engine update: {exc}")
            raise EngineUpdateError(f"Windows could not start the updater: {exc}") from exc

        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        if output:
            log.info(f"{status}\n{output}")
        if completed.returncode != 0:
            useful_lines = [line.strip() for line in output.splitlines() if line.strip()]
            detail = "\n".join(useful_lines[-8:])
            raise EngineUpdateError(
                "The engine update failed."
                + (f"\n\nLast updater messages:\n{detail}" if detail else "")
            )

    status_hook("Checking updated engine...")
    importlib.invalidate_caches()
    health = inspect_engine()
    if not health.ready:
        log.error(f"Engine remained unhealthy after update:\n{health.details()}")
        raise EngineUpdateError(
            "The update finished, but one or more required components are still missing."
        )
    log.info(f"Engine update completed successfully.\n{health.details()}")
    return health


def clean_error(value: str) -> str:
    return ANSI_ESCAPE.sub("", value).strip()


def normalized_error(value: str) -> str:
    return clean_error(value).casefold().replace("\u2019", "'")


def is_cookie_error(value: str) -> bool:
    message = normalized_error(value)
    return "cookie" in message and any(marker in message for marker in COOKIE_ERROR_MARKERS)


def is_login_error(value: str) -> bool:
    message = normalized_error(value)
    return any(marker in message for marker in LOGIN_ERROR_MARKERS)


def is_transient_error(value: str) -> bool:
    message = normalized_error(value)
    return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)


def is_youtube_video_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.hostname or "").casefold()
    video_id = ""
    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    ):
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.path.rstrip("/") == "/watch":
            video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        elif len(path_parts) >= 2 and path_parts[0] in ("shorts", "embed", "live"):
            video_id = path_parts[1]
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id))


def friendly_error(value: str, browser_name: str = "Off") -> str:
    error = clean_error(value)
    message = normalized_error(error)
    if any(marker in message for marker in LOGIN_ERROR_MARKERS):
        if browser_name == "Off":
            return (
                "YouTube blocked this anonymous request.\n\n"
                "First click Update Engine at the top of the converter. If the engine is "
                "already current, sign in to YouTube in your browser, select it under "
                "\"Login fallback\", and retry."
            )
        return (
            f"YouTube could not verify the {browser_name} session.\n\n"
            f"Open YouTube in {browser_name}, make sure you are signed in, then retry. "
            "If it still fails, fully close the browser or select Firefox."
        )
    if is_cookie_error(error):
        return (
            f"The converter could not read cookies from {browser_name}.\n\n"
            "The app retried anonymously, but YouTube still rejected the request. Fully close "
            "that browser and retry, or select a different signed-in browser."
        )
    if "requested format is not available" in message:
        return (
            "YouTube did not provide the selected quality for this video.\n\n"
            "Choose a lower quality or Best available, then retry."
        )
    if "unable to download" in message and ("webpage" in message or "api page" in message):
        return (
            "The converter could not reach YouTube.\n\n"
            "Check your connection, VPN, firewall, or security software, then retry."
        )
    if "ffmpeg" in message and ("not found" in message or "missing" in message):
        return "FFmpeg is missing. Click Update Engine at the top of the converter."
    if "output file already exists" in message:
        return "A file with that name already exists. Choose a different file name and retry."
    return error


def find_output_path(
    target_dir: Path,
    prepared_path: str,
    custom_name: str,
    media_format: str,
) -> Path:
    if custom_name:
        chosen_path = target_dir / f"{custom_name}.{media_format}"
        if chosen_path.is_file():
            return chosen_path.resolve()

    prepared = Path(prepared_path)
    expected = prepared.with_suffix(f".{media_format}")
    if expected.is_file():
        return expected.resolve()

    stem = custom_name or expected.stem
    matches = sorted(
        (
            path
            for path in target_dir.glob(f"{stem}.*")
            if path.is_file() and path.suffix.lower() == f".{media_format}"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return (matches[0] if matches else expected).resolve()


class EngineLog:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.log_dir / "engine.log"

    def _write(self, level: str, message: str) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                if self.path.exists() and self.path.stat().st_size > 2_000_000:
                    backup = self.log_dir / "engine.previous.log"
                    if backup.exists():
                        backup.unlink()
                    self.path.replace(backup)
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                with self.path.open("a", encoding="utf-8") as log_file:
                    log_file.write(f"{timestamp} [{level}] {clean_error(str(message))}\n")
        except OSError:
            pass

    def debug(self, message: str) -> None:
        if str(message).startswith("[debug]"):
            self._write("DEBUG", message)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARNING", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def exception(self, context: str) -> None:
        self._write("CRASH", f"{context}\n{traceback.format_exc()}")


class ConverterEngine:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "cache"
        self.log = EngineLog(self.data_dir / "logs")

    def common_options(self, browser_name: str, *, quiet: bool) -> dict:
        options = {
            "cachedir": str(self.cache_dir),
            "logger": self.log,
            "noplaylist": True,
            "quiet": quiet,
            "no_warnings": quiet,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 5,
            "file_access_retries": 3,
            "socket_timeout": 30,
        }
        deno_path = find_deno()
        if deno_path is not None:
            options["js_runtimes"] = {"deno": {"path": str(deno_path)}}
        browser = BROWSER_COOKIE_OPTIONS.get(browser_name)
        if browser:
            options["cookiesfrombrowser"] = (browser,)
        return options

    def fetch_metadata(self, url: str, browser_name: str) -> dict:
        yt_dlp = self._load_yt_dlp()
        options = self.common_options(browser_name, quiet=True)
        options.update({"skip_download": True, "extract_flat": False})
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                return downloader.extract_info(url, download=False)
        except Exception as exc:
            if browser_name != "Off" and is_cookie_error(str(exc)):
                self.log.warning(
                    f"Could not use {browser_name} cookies for preview; retrying anonymously."
                )
                anonymous_options = self.common_options("Off", quiet=True)
                anonymous_options.update({"skip_download": True, "extract_flat": False})
                try:
                    with yt_dlp.YoutubeDL(anonymous_options) as downloader:
                        return downloader.extract_info(url, download=False)
                except Exception as anonymous_exc:
                    if is_login_error(str(anonymous_exc)):
                        raise BrowserCookieError(browser_name, exc) from anonymous_exc
                    raise
            raise

    def download(
        self,
        request: DownloadRequest,
        browser_name: str,
        progress_hook: Callable[[dict], None],
        status_hook: Callable[[str], None],
    ) -> DownloadResult:
        yt_dlp = self._load_yt_dlp()
        target_dir = request.target_dir.expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        output_name = (
            f"{request.custom_name}.%(ext)s"
            if request.custom_name
            else "%(title).180s.%(ext)s"
        )
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("FFmpeg is missing")
        if request.custom_name:
            intended_output = target_dir / f"{request.custom_name}.{request.media_format}"
            if intended_output.exists():
                raise FileExistsError(f"Output file already exists: {intended_output}")

        browser_attempts = [browser_name]
        if browser_name != "Off":
            browser_attempts.append("Off")
        last_error: Exception | None = None
        browser_cookie_error: Exception | None = None

        for active_browser in browser_attempts:
            options = self.common_options(active_browser, quiet=False)
            options.update(
                {
                    "outtmpl": str(target_dir / output_name),
                    "restrictfilenames": False,
                    "windowsfilenames": True,
                    "overwrites": False,
                    "progress_hooks": [progress_hook],
                    "ffmpeg_location": str(ffmpeg),
                }
            )
            if request.media_format == "mp3":
                options.update(
                    {
                        "format": "bestaudio/best",
                        "postprocessors": [
                            {
                                "key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3",
                                "preferredquality": request.quality,
                            }
                        ],
                    }
                )
            else:
                options.update(
                    {
                        "format": request.quality,
                        "merge_output_format": "mp4",
                    }
                )

            for attempt in range(3):
                try:
                    with yt_dlp.YoutubeDL(options) as downloader:
                        info = downloader.extract_info(request.url, download=True)
                        prepared_path = downloader.prepare_filename(info)
                    output_path = find_output_path(
                        target_dir,
                        prepared_path,
                        request.custom_name,
                        request.media_format,
                    )
                    if not output_path.is_file():
                        raise RuntimeError(
                            f"Conversion finished but the output file was not found: {output_path}"
                        )
                    return DownloadResult(
                        title=request.custom_name or info.get("title") or "Untitled conversion",
                        output_path=output_path,
                    )
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    last_error = exc
                    error_text = str(exc)
                    if (
                        active_browser != "Off"
                        and is_cookie_error(error_text)
                        and attempt == 0
                    ):
                        browser_cookie_error = exc
                        status_hook(
                            f"Could not read {active_browser} cookies. Retrying anonymously..."
                        )
                        break
                    if (
                        active_browser == "Off"
                        and browser_cookie_error is not None
                        and is_login_error(error_text)
                    ):
                        raise BrowserCookieError(browser_name, browser_cookie_error) from exc
                    if is_transient_error(error_text) and attempt < 2:
                        status_hook(
                            f"YouTube returned a temporary error. Retrying ({attempt + 2}/3)..."
                        )
                        continue
                    raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("The conversion could not be started.")

    @staticmethod
    def _load_yt_dlp():
        import yt_dlp

        return yt_dlp

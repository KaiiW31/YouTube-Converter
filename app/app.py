import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

# The portable runtime is intentionally kept inside this project. Tcl/Tk's
# Windows bootstrap can reject an absolute library path containing spaces, but
# resolves these paths correctly relative to the launcher's working directory.
if not getattr(sys, "frozen", False) and (Path.cwd() / "runtime" / "tcl" / "tcl8.6").is_dir():
    os.environ.setdefault("TCL_LIBRARY", "runtime/tcl/tcl8.6")
    os.environ.setdefault("TK_LIBRARY", "runtime/tcl/tk8.6")

from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter, ImageGrab
from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

from converter_engine import (
    BROWSER_COOKIE_OPTIONS,
    BrowserCookieError,
    ConverterEngine,
    DownloadCancelled,
    DownloadRequest,
    EngineUpdateError,
    friendly_error as friendly_download_error,
    inspect_engine,
    is_youtube_video_url,
    run_engine_update,
)


if not hasattr(ScalingTracker, "_youtube_converter_original_window_scaling_update"):
    ScalingTracker._youtube_converter_original_window_scaling_update = (
        ScalingTracker.update_scaling_callbacks_for_window.__func__
    )

    def _defer_window_scaling_update_while_moving(cls, window):
        if getattr(window, "_defer_dpi_redraw", False):
            window._dpi_redraw_pending = True
            return
        cls._youtube_converter_original_window_scaling_update(cls, window)

    ScalingTracker.update_scaling_callbacks_for_window = classmethod(
        _defer_window_scaling_update_while_moving
    )


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
ICON_PATH = BUNDLE_DIR / "assets" / "app_icon.ico"
SETTINGS_DIR = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "YouTube Converter"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"

RED = "#E62117"
RED_DARK = "#B91C1C"
RED_HOVER = "#C81E1E"
RED_HOVER_DARK = "#7F1D1D"
BLUE = "#2563EB"
BLUE_DARK = "#1D4ED8"
BLUE_HOVER = "#1D4ED8"
BLUE_HOVER_DARK = "#1E40AF"
SUCCESS = "#16A34A"
SUCCESS_DARK = "#15803D"
SUCCESS_HOVER = "#15803D"
SUCCESS_HOVER_DARK = "#166534"

ACCENT_TRANSITION_MS = 210
ACCENT_TRANSITION_FRAMES = 12
HISTORY_SCROLL_INCREMENT = 4

AUDIO_QUALITIES = {
    "Best audio": "0",
    "320 kbps": "320",
    "256 kbps": "256",
    "192 kbps": "192",
    "128 kbps": "128",
}

VIDEO_QUALITIES = {
    "Best available": None,
    "4K / 2160p": 2160,
    "2K / 1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

def set_windows_app_identity():
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Kai.YouTubeConverter")
    except Exception:
        pass


class ResponsiveScrollableFrame(ctk.CTkScrollableFrame):
    """Scrollable frame with a practical mouse-wheel step on Windows."""

    def _set_scroll_increments(self):
        super()._set_scroll_increments()
        if sys.platform.startswith("win"):
            # CustomTkinter's Windows default advances only 20 px for a normal
            # wheel notch. Four-pixel units make a notch move roughly two
            # history rows while retaining smooth high-resolution scrolling.
            self._parent_canvas.configure(
                xscrollincrement=HISTORY_SCROLL_INCREMENT,
                yscrollincrement=HISTORY_SCROLL_INCREMENT,
            )


class YouTubeConverterApp(ctk.CTk):
    def __init__(self):
        set_windows_app_identity()
        super().__init__()
        self.settings = load_settings()
        save_settings(self.settings)
        self.engine = ConverterEngine(SETTINGS_DIR)
        self.engine_health = inspect_engine()
        saved_theme = self.settings.get("theme", "System")
        if saved_theme not in ("System", "Light", "Dark"):
            saved_theme = "System"
        ctk.set_appearance_mode(saved_theme)
        ctk.set_default_color_theme("blue")
        ctk.ThemeManager.theme["CTkFont"].update({"family": "Arial", "size": 15})
        ctk.set_widget_scaling(1.05)

        self.title("YouTube Converter")
        window_width = min(1000, self.winfo_screenwidth() - 60)
        window_height = min(920, self.winfo_screenheight() - 100)
        self.geometry(f"{window_width}x{window_height}")
        # Stay narrow enough to fit a half-screen snap on laptops and on
        # high-DPI displays, where 820 logical pixels can exceed half of the
        # available desktop width.
        self.minsize(620, 650)
        if ICON_PATH.exists():
            self._apply_window_icon()
            self.after(300, self._apply_window_icon)
            self.after(1200, self._apply_window_icon)

        self.url_var = ctk.StringVar()
        self.format_var = ctk.StringVar(value="")
        self.quality_var = ctk.StringVar(value="")
        self.folder_var = ctk.StringVar(value=self._saved_folder_for("mp3"))
        self.filename_var = ctk.StringVar()
        self.theme_var = ctk.StringVar(value=saved_theme)
        saved_browser = self.settings.get("youtube_browser", "Off")
        if saved_browser not in BROWSER_COOKIE_OPTIONS:
            saved_browser = "Off"
        self.youtube_browser_var = ctk.StringVar(value=saved_browser)
        self.status_var = ctk.StringVar(value="Paste a YouTube link to begin.")
        self.preview_title_var = ctk.StringVar(value="Paste a YouTube link to preview it.")
        self.preview_meta_var = ctk.StringVar(value="")
        self.progress_var = ctk.DoubleVar(value=0)
        self.cancel_event = threading.Event()
        self.is_busy = False
        self.is_updating = False
        self.conversion_completed = False
        self.preview_after_id = None
        self.preview_image = None
        self.preview_cache = {}
        self.preview_image_cache = {}
        self.preview_pending = False
        self.current_video_restricted = False
        self.current_preview_block_message = ""
        self.current_preview_url = ""
        self.last_preview_title = ""
        self.conversion_controls = []
        self.history_containers = {}
        self.current_view = "Convert"
        self.view_sync_after_id = None
        self.dpi_redraw_after_id = None
        self._defer_dpi_redraw = False
        self._dpi_redraw_pending = False
        self._displayed_accent_palette = format_accent_palette("")
        self._accent_animation_target = self._displayed_accent_palette
        self._accent_animation_after_id = None
        self._control_style_state = (False, False, False)
        self.report_callback_exception = self.handle_ui_exception

        self._build_ui()
        self.url_var.trace_add("write", self.schedule_preview)
        self.filename_var.trace_add("write", self.conversion_detail_changed)
        self.quality_var.trace_add("write", self.conversion_detail_changed)
        self.folder_var.trace_add("write", self.conversion_detail_changed)
        self.bind("<Configure>", self._schedule_view_sync, add="+")
        self.protocol("WM_DELETE_WINDOW", self.request_close)
        self.update_control_states()

    def _apply_window_icon(self):
        if ICON_PATH.exists() and self.winfo_exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

    def handle_ui_exception(self, exception_type, exception_value, exception_traceback):
        import traceback

        details = "".join(
            traceback.format_exception(exception_type, exception_value, exception_traceback)
        )
        self.engine.log.error(f"Unhandled UI exception\n{details}")
        messagebox.showerror(
            "YouTube Converter error",
            "The app hit an unexpected error. A diagnostic log was saved in:\n\n"
            f"{self.engine.log.path}",
        )

    def start_engine_update(self):
        if self.is_updating:
            return
        if self.is_busy:
            messagebox.showinfo(
                "Conversion in progress",
                "Finish or cancel the current conversion before updating the engine.",
            )
            return
        if self.preview_pending:
            messagebox.showinfo(
                "Checking video",
                "Wait for the current video check to finish before updating the engine.",
            )
            return
        if getattr(sys, "frozen", False):
            messagebox.showwarning(
                "Update unavailable",
                "This standalone build cannot update its embedded engine. Use the maintained "
                "YouTube Converter folder instead.",
            )
            return

        self.engine_health = inspect_engine()
        if not messagebox.askyesno(
            "Update conversion engine",
            "Check for the newest YouTube compatibility fixes now?\n\n"
            "An internet connection is required. The converter will restart automatically "
            "after a successful update.\n\n"
            f"Current status: {self.engine_health.summary()}",
        ):
            return

        self.is_updating = True
        self.update_engine_button.configure(state="disabled", text="Updating...")
        self.theme_menu.configure(state="disabled")
        for control in self.conversion_controls:
            control.configure(state="disabled")
        self.convert_button.configure(state="disabled", text="Updating...")
        self.style_control_states(False, False, False)
        self.progress_var.set(0)
        self.restore_status_style()
        self.status_var.set("Preparing engine update...")
        threading.Thread(target=self._engine_update_worker, daemon=True).start()

    def _engine_update_worker(self):
        try:
            python = APP_DIR / "runtime" / "python.exe"
            health = run_engine_update(
                python,
                APP_DIR,
                self.engine.log,
                lambda status: self.after(0, self._show_engine_update_status, status),
            )
        except Exception as exc:
            self.engine.log.exception("In-app engine update failed")
            self.after(0, self._finish_engine_update_error, exc)
            return
        self.after(0, self._finish_engine_update_success, health)

    def _show_engine_update_status(self, status):
        if self.is_updating:
            self.status_var.set(status)

    def _restore_after_engine_update(self):
        self.is_updating = False
        self.update_engine_button.configure(state="normal", text="Update Engine")
        self.theme_menu.configure(state="normal")
        self.update_control_states()

    def _finish_engine_update_error(self, error):
        self._restore_after_engine_update()
        self.progress_var.set(0)
        self.status_var.set("Engine update failed — your existing engine was kept.")
        if isinstance(error, EngineUpdateError):
            detail = str(error)
        else:
            detail = f"{type(error).__name__}: {error}"
        messagebox.showerror(
            "Engine update failed",
            f"{detail}\n\nDiagnostic log:\n{self.engine.log.path}",
        )

    def _finish_engine_update_success(self, health):
        self.engine_health = health
        self._restore_after_engine_update()
        self.progress_var.set(1)
        self.status_label.configure(
            text_color=("#15803D", "#4ADE80"),
            font=app_font(15, "bold"),
        )
        self.status_var.set("✓ Engine updated successfully. Restarting...")
        messagebox.showinfo(
            "Engine updated",
            "The conversion engine is up to date and healthy.\n\n"
            "YouTube Converter will restart now.",
        )
        self.after(100, self.restart_application)

    def restart_application(self):
        python = APP_DIR / "runtime" / "pythonw.exe"
        script = APP_DIR / "app.py"
        try:
            environment = os.environ.copy()
            environment.update(
                {
                    "TCL_LIBRARY": "runtime/tcl/tcl8.6",
                    "TK_LIBRARY": "runtime/tcl/tk8.6",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONUTF8": "1",
                }
            )
            executable = python if python.is_file() else Path(sys.executable)
            subprocess.Popen(
                [str(executable), str(script)],
                cwd=str(APP_DIR),
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
            )
        except OSError as exc:
            self.engine.log.error(f"Could not restart after engine update: {exc}")
            messagebox.showwarning(
                "Restart needed",
                "The update succeeded, but Windows could not reopen the converter. "
                "Please open the YouTube Converter shortcut manually.",
            )
        self.destroy()

    def request_close(self):
        if self.is_updating:
            messagebox.showinfo(
                "Update in progress",
                "Please wait for the engine update to finish before closing the converter.",
            )
            return
        self.destroy()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.grid(row=0, column=0, sticky="nsew", padx=32, pady=28)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(shell, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(header, text="YouTube Converter", font=app_font(32, "bold"))
        title.grid(row=0, column=0, sticky="w")

        self.update_engine_button = red_button(
            header,
            text="Update Engine",
            width=150,
            height=32,
            command=self.start_engine_update,
        )
        self.update_engine_button.grid(row=0, column=1, sticky="e", padx=(12, 10))

        self.theme_menu = red_option_menu(
            header,
            variable=self.theme_var,
            values=["System", "Light", "Dark"],
            width=120,
            command=self.change_theme,
        )
        self.theme_menu.grid(row=0, column=2, sticky="e")

        note = ctk.CTkLabel(
            shell,
            text="Convert permitted YouTube videos to MP3 audio or MP4 video on your own PC.",
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        note.grid(row=1, column=0, sticky="ew", pady=(6, 22))

        panel = ctk.CTkFrame(shell, corner_radius=18, border_width=1)
        panel.grid(row=2, column=0, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        self.view_switch = ctk.CTkSegmentedButton(
            panel,
            values=["Convert", "History"],
            command=self.show_view,
            height=34,
            selected_color=(RED, RED_DARK),
            selected_hover_color=(RED_HOVER, RED_HOVER_DARK),
            unselected_hover_color=("gray82", "gray25"),
        )
        self.view_switch.grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))
        self.view_switch.set("Convert")

        view_stack = ctk.CTkFrame(panel, fg_color="transparent")
        view_stack.grid(row=1, column=0, sticky="nsew", padx=20, pady=(6, 16))
        view_stack.grid_columnconfigure(0, weight=1)
        view_stack.grid_rowconfigure(0, weight=1)

        content = ctk.CTkFrame(view_stack, fg_color="transparent")
        content.grid(row=0, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        self.convert_view = content

        ctk.CTkLabel(content, text="YouTube link", font=app_font(16, "bold")).grid(row=0, column=0, sticky="w")
        link_row = ctk.CTkFrame(content, fg_color="transparent")
        link_row.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        link_row.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            link_row,
            textvariable=self.url_var,
            height=44,
            placeholder_text="https://www.youtube.com/watch?v=...",
        )
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.focus()
        self.url_undo = EntryUndoManager(self.url_entry)
        self.conversion_controls.append(self.url_entry)

        preview = ctk.CTkFrame(content, corner_radius=14, fg_color=("gray92", "gray17"))
        preview.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        preview.grid_columnconfigure(0, weight=1)

        preview_body = ctk.CTkFrame(preview, fg_color="transparent")
        preview_body.grid(row=0, column=0, pady=10)

        self.thumbnail_label = ctk.CTkLabel(preview_body, text="", width=220, height=124, corner_radius=10, fg_color=("gray82", "gray25"))
        self.thumbnail_label.grid(row=0, column=0, sticky="w")

        preview_text = ctk.CTkFrame(preview_body, fg_color="transparent")
        preview_text.grid(row=0, column=1, sticky="nsew", padx=(18, 0))
        preview_text.grid_columnconfigure(0, weight=1)

        self.preview_title = ctk.CTkLabel(
            preview_text,
            textvariable=self.preview_title_var,
            font=app_font(20, "bold"),
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self.preview_title.grid(row=0, column=0, sticky="ew")

        self.preview_meta = ctk.CTkLabel(
            preview_text,
            textvariable=self.preview_meta_var,
            text_color=("gray35", "gray70"),
            font=app_font(15),
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self.preview_meta.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        options = ctk.CTkFrame(content, fg_color="transparent")
        options.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        # Keep all three controls in fixed, equal lanes. CTkOptionMenu normally
        # changes its requested width when its text changes (for example,
        # "Best audio" -> "Best available"), which made the entire row move.
        options.grid_columnconfigure((0, 1, 2), weight=1, uniform="conversion_option")
        self.options_row = options

        self.format_label = ctk.CTkLabel(options, text="Format", font=app_font(16, "bold"))
        self.format_label.grid(row=0, column=0, sticky="w")
        self.format_segment = ctk.CTkSegmentedButton(
            options,
            values=["MP3", "MP4"],
            command=self.change_format,
            height=40,
            selected_color=(RED, RED_DARK),
            selected_hover_color=(RED_HOVER, RED_HOVER_DARK),
            unselected_hover_color=("gray75", "gray30"),
        )
        self.format_segment.grid(row=1, column=0, sticky="ew", pady=(8, 0), padx=(0, 8))
        self.conversion_controls.append(self.format_segment)

        self.quality_label = ctk.CTkLabel(options, text="Quality", font=app_font(16, "bold"))
        self.quality_label.grid(row=0, column=1, sticky="w")
        self.quality_menu = red_option_menu(options, variable=self.quality_var, values=list(AUDIO_QUALITIES), height=40)
        self.quality_menu.grid(row=1, column=1, sticky="ew", pady=(8, 0), padx=8)
        self.conversion_controls.append(self.quality_menu)

        self.youtube_browser_label = ctk.CTkLabel(
            options,
            text="Login fallback",
            font=app_font(16, "bold"),
        )
        self.youtube_browser_label.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.youtube_browser_menu = red_option_menu(
            options,
            variable=self.youtube_browser_var,
            values=list(BROWSER_COOKIE_OPTIONS),
            height=40,
            command=self.change_youtube_browser,
        )
        self.youtube_browser_menu.grid(row=1, column=2, sticky="ew", pady=(8, 0), padx=(8, 0))
        self.conversion_controls.append(self.youtube_browser_menu)

        self.filename_label = ctk.CTkLabel(content, text="File name", font=app_font(16, "bold"))
        self.filename_label.grid(row=4, column=0, sticky="w")
        self.filename_entry = ctk.CTkEntry(
            content,
            textvariable=self.filename_var,
            height=42,
            placeholder_text="Uses the video title if left blank",
        )
        self.filename_entry.grid(row=5, column=0, sticky="ew", pady=(6, 12))
        self.filename_undo = EntryUndoManager(self.filename_entry)
        self.conversion_controls.append(self.filename_entry)

        self.folder_label = ctk.CTkLabel(content, text="Save folder", font=app_font(16, "bold"))
        self.folder_label.grid(row=6, column=0, sticky="w")
        folder_row = ctk.CTkFrame(content, fg_color="transparent")
        folder_row.grid(row=7, column=0, sticky="ew", pady=(6, 12))
        folder_row.grid_columnconfigure(0, weight=1)

        self.folder_entry = ctk.CTkEntry(folder_row, textvariable=self.folder_var, height=42)
        self.folder_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.browse_button = red_button(folder_row, text="Browse", width=110, height=42, command=self.choose_folder)
        self.browse_button.grid(row=0, column=1)
        self.conversion_controls.extend((self.folder_entry, self.browse_button))

        progress_row = ctk.CTkFrame(content, fg_color="transparent")
        progress_row.grid(row=8, column=0, sticky="ew", pady=(2, 6))
        progress_row.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(progress_row, variable=self.progress_var, height=12, progress_color=(RED, RED_DARK))
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 14))

        self.convert_button = red_button(progress_row, text="Convert", width=132, height=42, command=self.start_download)
        self.convert_button.grid(row=0, column=1, sticky="e")
        self.conversion_controls.append(self.convert_button)

        self.status_label = ctk.CTkLabel(content, textvariable=self.status_var, text_color=("gray30", "gray70"), anchor="w")
        self.status_label.grid(row=9, column=0, sticky="ew")

        self.history_view = ctk.CTkFrame(view_stack, fg_color="transparent")
        self.history_view.grid(row=0, column=0, sticky="nsew")
        self.history_view.grid_columnconfigure(0, weight=1)
        self.history_view.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self.history_view,
            text="Recent conversions",
            font=app_font(22, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(4, 2))
        ctk.CTkLabel(
            self.history_view,
            text="Conversions are kept for 30 days. Click an item to locate its file.",
            text_color=("gray35", "gray70"),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 14))

        history = ResponsiveScrollableFrame(
            self.history_view,
            fg_color=("#E8E8E8", "#202225"),
            corner_radius=14,
            border_width=1,
            border_color=("gray72", "#4A4D52"),
            scrollbar_button_color=("gray58", "#666A70"),
            scrollbar_button_hover_color=("gray48", "#7A7F86"),
        )
        self.history_scroll = history
        history.grid(row=2, column=0, sticky="nsew")
        history.grid_columnconfigure((0, 1), weight=1, uniform="history")
        for column, media_format in enumerate(("mp3", "mp4")):
            side = ctk.CTkFrame(history, fg_color="transparent")
            side.grid(row=0, column=column, sticky="nsew", padx=18, pady=18)
            side.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(side, text=f"{media_format.upper()} history", font=app_font(16, "bold"), anchor="w").grid(
                row=0, column=0, sticky="ew", pady=(0, 7)
            )
            item_list = ctk.CTkFrame(side, fg_color="transparent")
            item_list.grid(row=1, column=0, sticky="nsew")
            item_list.grid_columnconfigure(0, weight=1)
            self.history_containers[media_format] = item_list
        self.show_view("Convert")
        self.refresh_history()

    def show_view(self, selected):
        self.current_view = "History" if selected == "History" else "Convert"
        if self.current_view == "History":
            self.refresh_history()
        self._sync_current_view()

    def _sync_current_view(self):
        self.view_sync_after_id = None
        if not hasattr(self, "convert_view") or not self.winfo_exists():
            return
        if self.current_view == "History":
            self.convert_view.grid_remove()
            self.history_view.grid()
            self.history_view.lift()
        else:
            self.history_view.grid_remove()
            self.convert_view.grid()
            self.convert_view.lift()
        self.view_switch.set(self.current_view)

    def _schedule_view_sync(self, event=None):
        if event is not None and event.widget is not self:
            return
        if not hasattr(self, "convert_view"):
            return
        self._defer_dpi_redraw_during_window_move()
        if self.view_sync_after_id is not None:
            self.after_cancel(self.view_sync_after_id)
        self.view_sync_after_id = self.after(120, self._sync_current_view)

    def _defer_dpi_redraw_during_window_move(self):
        self._defer_dpi_redraw = True
        if self.dpi_redraw_after_id is not None:
            self.after_cancel(self.dpi_redraw_after_id)
        self.dpi_redraw_after_id = self.after(220, self._finish_deferred_dpi_redraw)

    def _finish_deferred_dpi_redraw(self):
        self.dpi_redraw_after_id = None
        self._defer_dpi_redraw = False
        if not self._dpi_redraw_pending or not self.winfo_exists():
            return
        self._dpi_redraw_pending = False
        self.block_update_dimensions_event()
        try:
            ScalingTracker.update_scaling_callbacks_for_window(self)
        finally:
            self.unblock_update_dimensions_event()

    def _set_scaling(self, new_widget_scaling, new_window_scaling):
        super()._set_scaling(new_widget_scaling, new_window_scaling)
        if hasattr(self, "convert_view"):
            self._schedule_view_sync()
            self.after(1100, self._sync_current_view)

    def set_conversion_controls_enabled(self, enabled):
        if enabled:
            self.update_control_states()
            return
        for control in self.conversion_controls:
            control.configure(state="disabled")
        self.convert_button.configure(
            state="normal",
            text="Cancel",
            command=self.cancel_download,
            fg_color=("gray45", "gray35"),
            hover_color=("gray35", "gray28"),
            text_color=("white", "white"),
        )
        self.style_control_states(False, False, False)

    def update_control_states(self):
        if self.is_busy or self.is_updating:
            return
        valid_url = self._looks_like_youtube_url(self.url_var.get().strip())
        has_format = self.format_var.get() in ("mp3", "mp4")
        format_allowed = (
            valid_url
            and not self.current_video_restricted
            and not self.current_preview_block_message
        )
        self.url_entry.configure(state="normal")
        self.format_segment.configure(state="normal" if format_allowed else "disabled")
        selection_available = format_allowed and has_format
        selection_state = "normal" if selection_available else "disabled"
        self.quality_menu.configure(state=selection_state)
        self.filename_entry.configure(state=selection_state)
        can_convert = selection_available and not self.preview_pending and not self.conversion_completed
        self.convert_button.configure(
            state="normal" if can_convert else "disabled",
            text="✓ Converted" if self.conversion_completed else "Convert",
            command=self.start_download,
        )
        self.folder_entry.configure(state="disabled")
        self.browse_button.configure(state=selection_state)
        self.youtube_browser_menu.configure(state="normal")
        self.style_control_states(True, format_allowed, selection_available)
        if self.conversion_completed:
            self.convert_button.configure(
                fg_color=(SUCCESS, SUCCESS_DARK),
                hover_color=(SUCCESS_HOVER, SUCCESS_HOVER_DARK),
                text_color=("white", "white"),
                text_color_disabled=("white", "white"),
            )

    def style_control_states(self, url_enabled, format_enabled, details_enabled):
        active_text = ("gray10", "gray90")
        muted_text = ("gray55", "gray45")
        disabled_fill = ("gray78", "gray25")
        disabled_button = ("gray68", "gray31")
        self._control_style_state = (url_enabled, format_enabled, details_enabled)

        self.url_entry.configure(fg_color=("gray95", "gray14") if url_enabled else disabled_fill)
        self.format_label.configure(text_color=active_text if format_enabled else muted_text)
        self.format_segment.configure(
            fg_color=("gray86", "gray20") if format_enabled else disabled_fill,
            unselected_color=("gray86", "gray20") if format_enabled else disabled_fill,
            text_color=active_text if format_enabled else muted_text,
        )
        selected_format = self.format_var.get().upper()
        for value, button in getattr(self.format_segment, "_buttons_dict", {}).items():
            button.configure(
                text_color=("white", "white") if format_enabled and value == selected_format else (
                    active_text if format_enabled else muted_text
                )
            )

        detail_text = active_text if details_enabled else muted_text
        detail_fill = ("gray95", "gray14") if details_enabled else disabled_fill
        for label in (self.quality_label, self.filename_label, self.folder_label):
            label.configure(text_color=detail_text)
        self.quality_menu.configure(
            text_color=("white", "white") if details_enabled else detail_text,
        )
        self.filename_entry.configure(fg_color=detail_fill, text_color=detail_text)
        self.folder_entry.configure(fg_color=detail_fill, text_color=detail_text)
        self.browse_button.configure(text_color=("white", "white") if details_enabled else detail_text)
        self.convert_button.configure(
            text_color=("white", "white") if details_enabled else detail_text,
            text_color_disabled=("gray45", "gray55"),
        )
        self._apply_accent_palette(self._displayed_accent_palette)
        self._animate_accent_to(self.accent_colors())

    def _apply_accent_palette(self, palette):
        if not self.winfo_exists():
            return
        accent, accent_dark, accent_hover, accent_hover_dark = palette
        _url_enabled, format_enabled, details_enabled = self._control_style_state
        disabled_fill = ("gray78", "gray25")
        disabled_button = ("gray68", "gray31")

        self._configure_if_changed(
            self.update_engine_button,
            fg_color=(accent, accent_dark),
            hover_color=(accent_hover, accent_hover_dark),
        )
        self._configure_if_changed(
            self.theme_menu,
            fg_color=(accent, accent_dark),
            button_color=(accent_dark, accent_dark),
            button_hover_color=(accent_hover, accent_hover_dark),
            dropdown_hover_color=(accent_hover, accent_hover_dark),
        )
        self._configure_if_changed(
            self.view_switch,
            selected_color=(accent, accent_dark),
            selected_hover_color=(accent_hover, accent_hover_dark),
        )
        self._configure_if_changed(
            self.browse_button,
            fg_color=(accent, accent_dark) if details_enabled else disabled_button,
            hover_color=(accent_hover, accent_hover_dark),
        )
        if self.conversion_completed:
            self._configure_if_changed(
                self.convert_button,
                fg_color=(SUCCESS, SUCCESS_DARK),
                hover_color=(SUCCESS_HOVER, SUCCESS_HOVER_DARK),
            )
        else:
            self._configure_if_changed(
                self.convert_button,
                fg_color=(accent, accent_dark) if details_enabled else disabled_button,
                hover_color=(accent_hover, accent_hover_dark),
            )
        self._configure_if_changed(self.progress, progress_color=(accent, accent_dark))
        self._displayed_accent_palette = palette

    def _apply_option_row_palette(self, palette):
        accent, accent_dark, accent_hover, accent_hover_dark = palette
        _url_enabled, format_enabled, details_enabled = self._control_style_state
        disabled_fill = ("gray78", "gray25")
        disabled_button = ("gray68", "gray31")
        self._configure_if_changed(
            self.format_segment,
            selected_color=(accent, accent_dark) if format_enabled else disabled_button,
            selected_hover_color=(accent_hover, accent_hover_dark),
        )
        self._configure_if_changed(
            self.quality_menu,
            fg_color=(accent, accent_dark) if details_enabled else disabled_fill,
            button_color=(accent_dark, accent_dark) if details_enabled else disabled_button,
            button_hover_color=(accent_hover, accent_hover_dark),
            dropdown_hover_color=(accent_hover, accent_hover_dark),
        )
        self._configure_if_changed(
            self.youtube_browser_menu,
            fg_color=(accent, accent_dark),
            button_color=(accent_dark, accent_dark),
            button_hover_color=(accent_hover, accent_hover_dark),
            dropdown_hover_color=(accent_hover, accent_hover_dark),
        )

    @staticmethod
    def _configure_if_changed(widget, **options):
        changed = {
            name: value
            for name, value in options.items()
            if widget.cget(name) != value
        }
        if changed:
            widget.configure(**changed)

    def _animate_accent_to(self, target_palette):
        target_palette = tuple(target_palette)
        if (
            target_palette == self._accent_animation_target
            and self._accent_animation_after_id is not None
        ):
            self._apply_option_row_palette(target_palette)
            return
        if self._accent_animation_after_id is not None:
            self.after_cancel(self._accent_animation_after_id)
            self._accent_animation_after_id = None

        start_palette = self._displayed_accent_palette
        self._accent_animation_target = target_palette
        # The option row must never participate in the multi-frame colour
        # tween: each CTk menu redraw also recalculates its text geometry. Set
        # that row once, while the surrounding accent surfaces still animate.
        self._apply_option_row_palette(target_palette)
        if start_palette == target_palette:
            return

        frame_delay = max(1, ACCENT_TRANSITION_MS // ACCENT_TRANSITION_FRAMES)

        def draw_frame(frame):
            if not self.winfo_exists() or target_palette != self._accent_animation_target:
                return
            progress = min(1.0, frame / ACCENT_TRANSITION_FRAMES)
            eased = progress * progress * (3.0 - 2.0 * progress)
            palette = interpolate_accent_palette(start_palette, target_palette, eased)
            self._apply_accent_palette(palette)
            if frame < ACCENT_TRANSITION_FRAMES:
                self._accent_animation_after_id = self.after(
                    frame_delay,
                    draw_frame,
                    frame + 1,
                )
            else:
                self._accent_animation_after_id = None

        draw_frame(1)

    def accent_colors(self):
        return format_accent_palette(self.format_var.get())

    def refresh_history(self):
        settings_changed = False
        for media_format, container in self.history_containers.items():
            for child in container.winfo_children():
                child.destroy()

            key = f"{media_format}_history"
            original_entries = self.settings.get(key, [])
            entries = prune_history_entries(original_entries)
            if entries != original_entries:
                self.settings[key] = entries
                settings_changed = True

            if not entries:
                ctk.CTkLabel(
                    container,
                    text="No conversions in the last 30 days.",
                    height=42,
                    anchor="w",
                    text_color=("gray45", "gray55"),
                ).grid(row=0, column=0, sticky="ew")
                continue

            for index, entry in enumerate(entries):
                title = entry.get("title", "Untitled")
                display_title = title if len(title) <= 42 else title[:39] + "..."
                file_path = self.history_file_path(media_format, entry)
                history_button = ctk.CTkButton(
                    container,
                    text=f"{index + 1}. {display_title}  ·  {entry.get('time', '')}",
                    anchor="w",
                    height=42,
                    fg_color="transparent",
                    hover_color=("gray82", "gray25"),
                    text_color=("gray25", "gray75"),
                    text_color_disabled=("gray55", "gray45"),
                    command=(lambda path=file_path: reveal_file(path)) if file_path else None,
                    state="normal" if file_path else "disabled",
                )
                history_button.grid(row=index, column=0, sticky="ew", pady=1)

        if settings_changed:
            save_settings(self.settings)

    def history_file_path(self, media_format, entry):
        stored_path = entry.get("path", "")
        if stored_path and Path(stored_path).is_file():
            return stored_path
        folder = Path(self._saved_folder_for(media_format))
        fallback = folder / f"{sanitize_filename(entry.get('title', ''))}.{media_format}"
        if fallback.is_file():
            return str(fallback.resolve())
        wanted_stem = sanitize_filename(entry.get("title", "")).casefold()
        for candidate in folder.glob(f"*.{media_format}") if folder.is_dir() else ():
            if candidate.stem.casefold() == wanted_stem:
                return str(candidate.resolve())
        return None

    def add_history(self, media_format, title, file_path):
        key = f"{media_format}_history"
        entries = self.settings.get(key, [])
        if not isinstance(entries, list):
            entries = []
        entries.insert(0, {
            "title": title[:80],
            "time": datetime.now().strftime("%d %b, %H:%M"),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "path": str(file_path),
        })
        self.settings[key] = prune_history_entries(entries)
        save_settings(self.settings)
        self.refresh_history()

    def change_theme(self, theme):
        placement = self._capture_windows_placement()
        # CustomTkinter normally withdraws and reopens a Windows window while
        # recolouring its title bar. That breaks Windows snap layouts. Apply
        # the widget theme without that workaround, then recolour the title
        # bar directly so the current snapped/maximized placement is retained.
        header_setting = getattr(self, "_deactivate_windows_window_header_manipulation", False)
        self._deactivate_windows_window_header_manipulation = True
        try:
            ctk.set_appearance_mode(theme)
        finally:
            self._deactivate_windows_window_header_manipulation = header_setting
        self._set_windows_titlebar_appearance()
        self._theme_change_generation = getattr(self, "_theme_change_generation", 0) + 1
        generation = self._theme_change_generation
        self._restore_windows_placement(placement)
        self.after_idle(lambda: self._restore_theme_placement(placement, generation))
        self.after(80, lambda: self._restore_theme_placement(placement, generation))
        self.after(180, lambda: self._restore_theme_placement(placement, generation))
        self.settings["theme"] = theme
        save_settings(self.settings)

    def _capture_windows_placement(self):
        if not sys.platform.startswith("win"):
            return None
        try:
            import ctypes
            from ctypes import wintypes

            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            rect = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            return (
                hwnd,
                rect.left,
                rect.top,
                rect.right - rect.left,
                rect.bottom - rect.top,
                bool(ctypes.windll.user32.IsZoomed(hwnd)),
            )
        except Exception:
            return None

    def _restore_theme_placement(self, placement, generation):
        if generation == getattr(self, "_theme_change_generation", None):
            self._restore_windows_placement(placement)

    def _restore_windows_placement(self, placement):
        if not placement:
            return
        try:
            import ctypes

            hwnd, x, y, width, height, maximized = placement
            if maximized:
                ctypes.windll.user32.ShowWindow(hwnd, 3)
                return
            flags = 0x0004 | 0x0010 | 0x0200  # NOZORDER | NOACTIVATE | NOOWNERZORDER
            ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, width, height, flags)
        except Exception:
            pass

    def _set_windows_titlebar_appearance(self):
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1 if ctk.get_appearance_mode() == "Dark" else 0)
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attribute,
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                ) == 0:
                    break
        except Exception:
            pass

    def change_format(self, selected):
        self.clear_completed_conversion()
        self.format_var.set(selected.lower())
        self.folder_var.set(self._saved_folder_for(self.format_var.get()))
        self.update_quality_choices()
        self.update_control_states()
        self.status_var.set("Loading video information..." if self.preview_title_var.get() == "Loading preview..." else "Ready to convert.")

    def change_youtube_browser(self, selected):
        previous = self.settings.get("youtube_browser", "Off")
        if selected != "Off":
            confirmed = messagebox.askyesno(
                "Use browser login?",
                f"Use your signed-in {selected} session for YouTube?\n\n"
                "The converter will read YouTube cookies from this browser locally. "
                "It does not save them or send them anywhere other than YouTube.\n\n"
                f"If {selected} is still open and its cookies cannot be read, fully close "
                "the browser and choose it again.",
            )
            if not confirmed:
                self.youtube_browser_var.set(previous if previous in BROWSER_COOKIE_OPTIONS else "Off")
                return

        self.settings["youtube_browser"] = selected
        save_settings(self.settings)
        self.preview_cache.clear()
        self.clear_completed_conversion()
        if self._looks_like_youtube_url(self.url_var.get().strip()):
            self.schedule_preview()

    def cancel_download(self):
        if not self.is_busy:
            return
        self.cancel_event.set()
        self.convert_button.configure(state="disabled", text="Cancelling...")
        self.status_var.set("Cancelling safely...")

    def _saved_folder_for(self, media_format):
        return self.settings.get(f"{media_format}_download_folder", str(DEFAULT_DOWNLOAD_DIR))

    def update_quality_choices(self):
        if self.format_var.get() == "mp3":
            values = list(AUDIO_QUALITIES)
            default = "Best audio"
        else:
            values = list(VIDEO_QUALITIES)
            default = "Best available"

        self.quality_menu.configure(values=values)
        if self.quality_var.get() not in values:
            self.quality_var.set(default)

    def choose_folder(self):
        selected = filedialog.askdirectory(initialdir=self.folder_var.get() or str(DEFAULT_DOWNLOAD_DIR))
        if selected:
            self.clear_completed_conversion()
            self.folder_var.set(selected)
            self.settings[f"{self.format_var.get()}_download_folder"] = selected
            save_settings(self.settings)

    def schedule_preview(self, *_args):
        self.clear_completed_conversion()
        if self.preview_after_id is not None:
            self.after_cancel(self.preview_after_id)

        url = self.url_var.get().strip()
        if not url:
            self.preview_pending = False
            self.current_video_restricted = False
            self.current_preview_block_message = ""
            self.clear_format_selection()
            self.clear_preview("Paste a YouTube link to preview it.")
            self.status_var.set("Paste a YouTube link to begin.")
            return

        if not self._looks_like_youtube_url(url):
            self.preview_pending = False
            self.current_video_restricted = False
            self.current_preview_block_message = ""
            self.clear_format_selection()
            self.clear_preview("Waiting for a valid YouTube link.")
            self.status_var.set("Enter a valid YouTube link to continue.")
            return

        if url != self.current_preview_url:
            # A filename belongs to the video that supplied it. Clear both an
            # old automatic title and any old custom title when a genuinely
            # different link is pasted, then let the new preview provide its
            # own default name.
            self.filename_var.set("")
            self.last_preview_title = ""

        self.preview_pending = True
        self.current_video_restricted = False
        self.current_preview_block_message = ""
        self.update_control_states()
        self.status_var.set("Choose MP3 or MP4 to continue." if not self.format_var.get() else "Loading video information...")

        cached_preview = self.preview_cache.get(url)
        if cached_preview:
            self.apply_preview(url, *cached_preview)
            return

        self.preview_title_var.set("Loading preview...")
        self.preview_meta_var.set("")
        self.preview_after_id = self.after(700, self.start_preview_fetch, url)

    def clear_preview(self, title):
        self.preview_title_var.set(title)
        self.filename_var.set("")
        self.current_preview_url = ""
        self.last_preview_title = ""
        self.preview_meta_var.set("")
        self.thumbnail_label.configure(image=None, text="")
        # CustomTkinter can retain the last Tk image even after its public image
        # option is cleared. Clear the underlying label while keeping our cached
        # CTkImage alive for a future re-paste of the same URL.
        self.thumbnail_label._label.configure(image="")
        self.preview_image = None
        self.current_preview_block_message = ""

    def clear_format_selection(self):
        self.format_var.set("")
        self.quality_var.set("")
        self.format_segment.set("")
        self.update_control_states()

    def start_preview_fetch(self, url):
        self.preview_after_id = None
        threading.Thread(target=self.fetch_preview, args=(url,), daemon=True).start()

    def fetch_preview(self, url):
        try:
            info = self.engine.fetch_metadata(url, self.youtube_browser_var.get())

            title = info.get("title") or "Untitled video"
            channel = info.get("uploader") or info.get("channel") or "Unknown channel"
            duration = format_duration(info.get("duration"))
            resolution = best_resolution(info)
            thumbnail_url = info.get("thumbnail")
            image = safe_fetch_thumbnail(thumbnail_url) if thumbnail_url else None
            meta = " - ".join(value for value in [channel, duration, resolution] if value)
            self.after(0, self.apply_preview, url, title, meta, image)
        except Exception as exc:
            self.engine.log.exception("Preview extraction failed")
            restricted = is_age_restriction_error(str(exc))
            block_message = ""
            if isinstance(exc, BrowserCookieError):
                block_message = protected_video_guidance(
                    exc.browser_name,
                    cookie_unavailable=True,
                )
            try:
                preview = fetch_oembed_preview(url)
                image = safe_fetch_thumbnail(preview.get("thumbnail_url"))
                self.after(
                    0,
                    self.apply_preview,
                    url,
                    preview.get("title") or "Untitled video",
                    preview.get("author_name") or "YouTube video",
                    image,
                    restricted,
                    block_message,
                )
            except Exception:
                self.after(0, self.apply_preview_error, url)

    def apply_preview(
        self,
        url,
        title,
        meta,
        image,
        restricted=False,
        block_message="",
    ):
        if self.url_var.get().strip() != url:
            return

        self.preview_pending = False
        self.current_video_restricted = restricted
        self.current_preview_block_message = block_message
        self.preview_title_var.set(title)
        if block_message:
            preview_meta = f"{meta} - Browser login unavailable"
        elif restricted:
            preview_meta = f"{meta} - Age verification required"
        else:
            preview_meta = meta
        self.preview_meta_var.set(preview_meta)
        cached = self.preview_cache.get(url)
        if image is None and cached:
            image = cached[2]
        self.preview_cache[url] = (title, meta, image, restricted, block_message)
        default_filename = sanitize_filename(title)
        if not self.filename_var.get().strip() or self.filename_var.get() == self.last_preview_title:
            self.filename_var.set(default_filename)
        self.current_preview_url = url
        self.last_preview_title = default_filename
        if block_message or restricted:
            self.format_var.set("")
            self.quality_var.set("")
            self.format_segment.set("")
            self.status_var.set(
                block_message
                or protected_video_guidance(self.youtube_browser_var.get())
            )
        elif self.format_var.get() in ("mp3", "mp4"):
            self.status_var.set("Ready to convert.")
        self.update_control_states()
        if image is not None:
            if url not in self.preview_image_cache:
                self.preview_image_cache[url] = ctk.CTkImage(light_image=image, dark_image=image, size=(220, 124))
            self.preview_image = self.preview_image_cache[url]
            self.thumbnail_label.configure(image=self.preview_image, text="")
        else:
            self.thumbnail_label.configure(image=None, text="")
            self.preview_image = None

    def apply_preview_error(self, url):
        if self.url_var.get().strip() != url:
            return
        self.preview_pending = False
        self.current_video_restricted = False
        self.current_preview_block_message = ""
        self.clear_preview("Could not load preview. Check the link or try converting directly.")
        self.update_control_states()

    def start_download(self):
        if self.is_busy:
            return

        url = self.url_var.get().strip()
        if not self._looks_like_youtube_url(url):
            messagebox.showwarning("Add a YouTube link", "Paste a valid YouTube video link first.")
            return
        if self.current_preview_block_message:
            messagebox.showwarning(
                "Browser login unavailable",
                self.current_preview_block_message,
            )
            return
        if self.current_video_restricted:
            messagebox.showwarning(
                "Age verification required",
                protected_video_guidance(self.youtube_browser_var.get()),
            )
            return
        if self.preview_pending:
            messagebox.showinfo("Checking video", "Wait for the video information to finish loading first.")
            return

        media_format = self.format_var.get()
        target_folder = self.folder_var.get().strip() or str(DEFAULT_DOWNLOAD_DIR)
        custom_name = sanitize_filename(self.filename_var.get())
        display_name = custom_name or sanitize_filename(self.preview_title_var.get()) or "Original video title"
        confirmed = ask_conversion_confirmation(
            self,
            media_format,
            f"{display_name}.{media_format}",
            target_folder,
        )
        if not confirmed:
            return

        self.conversion_completed = False
        self.is_busy = True
        self.cancel_event.clear()
        self.after(
            16,
            self.begin_confirmed_download,
            url,
            media_format,
            target_folder,
            custom_name,
        )

    def begin_confirmed_download(self, url, media_format, target_folder, custom_name):
        download_quality = self.audio_quality() if media_format == "mp3" else self.video_format_selector()
        self.progress_var.set(0)
        self.status_var.set("Starting...")
        self.set_conversion_controls_enabled(False)
        self.folder_var.set(target_folder)
        self.settings[f"{media_format}_download_folder"] = target_folder
        save_settings(self.settings)

        self.update_idletasks()
        self.after(
            16,
            lambda: threading.Thread(
                target=self.download,
                args=(url, media_format, target_folder, custom_name, download_quality),
                daemon=True,
            ).start(),
        )

    def download(self, url, media_format, target_folder, custom_name, download_quality):
        try:
            request = DownloadRequest(
                url=url,
                media_format=media_format,
                target_dir=Path(target_folder),
                custom_name=custom_name,
                quality=download_quality,
            )
            result = self.engine.download(
                request,
                self.youtube_browser_var.get(),
                self.progress_hook,
                lambda message: self.after(0, self.status_var.set, message),
            )
            self.after(
                0,
                self.finish_success,
                media_format,
                result.title,
                str(result.output_path),
            )
        except DownloadCancelled:
            self.after(0, self.finish_cancelled)
        except Exception as exc:
            self.engine.log.exception("Download failed")
            self.after(
                0,
                self.finish_error,
                friendly_download_error(str(exc), self.youtube_browser_var.get()),
            )

    def audio_quality(self):
        return AUDIO_QUALITIES.get(self.quality_var.get(), "0")

    def video_format_selector(self):
        height = VIDEO_QUALITIES.get(self.quality_var.get())
        if height is None:
            return "bv*+ba/best"
        return f"bv*[height<={height}]+ba/b[height<={height}]/best[height<={height}]"

    def progress_hook(self, data):
        if self.cancel_event.is_set():
            raise DownloadCancelled()
        status = data.get("status")
        if status == "downloading":
            percent = parse_percent(data.get("_percent_str", ""))
            speed = data.get("_speed_str", "").strip()
            eta = data.get("_eta_str", "").strip()
            message = "Downloading"
            details = []
            if speed:
                details.append(speed)
            if eta:
                details.append(f"ETA {eta}")
            if details:
                message += " - " + " - ".join(details)

            self.after(0, self.progress_var.set, percent / 100)
            self.after(0, self.status_var.set, message)
        elif status == "finished":
            self.after(0, self.progress_var.set, 1)
            self.after(0, self.status_var.set, "Converting and saving...")

    def finish_success(self, media_format, history_title, output_path):
        self.is_busy = False
        self.conversion_completed = True
        self.cancel_event.clear()
        self.set_conversion_controls_enabled(True)
        self.progress_var.set(1)
        self.progress.configure(progress_color=(SUCCESS, SUCCESS_DARK))
        self.status_label.configure(text_color=("#15803D", "#4ADE80"), font=app_font(15, "bold"))
        self.status_var.set("✓ Conversion complete — file saved. Change an option to convert again.")
        self.add_history(media_format, history_title, output_path)

    def finish_error(self, error):
        self.is_busy = False
        self.conversion_completed = False
        self.cancel_event.clear()
        self.restore_status_style()
        self.set_conversion_controls_enabled(True)
        self.status_var.set("Conversion failed — see the message for details.")
        messagebox.showerror("Conversion failed", error)

    def finish_cancelled(self):
        self.is_busy = False
        self.conversion_completed = False
        self.cancel_event.clear()
        self.restore_status_style()
        self.set_conversion_controls_enabled(True)
        self.progress_var.set(0)
        self.status_var.set("Conversion cancelled.")

    def conversion_detail_changed(self, *_args):
        self.clear_completed_conversion()

    def clear_completed_conversion(self):
        if not self.conversion_completed or self.is_busy:
            return
        self.conversion_completed = False
        self.progress_var.set(0)
        self.restore_status_style()
        self.status_var.set("Ready to convert.")
        self.update_control_states()

    def restore_status_style(self):
        self.status_label.configure(text_color=("gray30", "gray70"), font=app_font(15))

    @staticmethod
    def _looks_like_youtube_url(url):
        return is_youtube_video_url(url)


def ask_conversion_confirmation(parent, media_format, file_name, target_folder):
    width = parent.winfo_width()
    height = parent.winfo_height()
    dialog_width, dialog_height = 560, 400
    accent = (BLUE, BLUE_DARK) if media_format == "mp3" else (RED, RED_DARK)
    hover = (BLUE_HOVER, BLUE_HOVER_DARK) if media_format == "mp3" else (RED_HOVER, RED_HOVER_DARK)
    dark_mode = ctk.get_appearance_mode() == "Dark"
    surface_color = "#1F1F1F" if dark_mode else "#F5F5F5"
    tint_color = "#101010" if dark_mode else "#F2F2F2"
    accent_color = accent[1] if dark_mode else accent[0]
    fallback_overlay_color = "#2E2E2E" if dark_mode else "#BFBFBF"
    overlay = None
    card_is_in_backdrop = False
    try:
        screenshot = ImageGrab.grab(
            bbox=(parent.winfo_rootx(), parent.winfo_rooty(), parent.winfo_rootx() + width, parent.winfo_rooty() + height)
        ).filter(ImageFilter.GaussianBlur(9))
        tint = Image.new("RGB", screenshot.size, tint_color)
        screenshot = Image.blend(screenshot.convert("RGB"), tint, 0.22)

        # Tk widgets always occupy rectangular windows. Its "transparent"
        # corners therefore reveal the root colour instead of the blurred
        # sibling behind the dialog. Render the rounded surface into the
        # backdrop itself, with 4x antialiasing for a clean outline.
        scale_x = screenshot.width / max(width, 1)
        scale_y = screenshot.height / max(height, 1)
        scale = min(scale_x, scale_y)
        card_width = max(1, round(dialog_width * scale_x))
        card_height = max(1, round(dialog_height * scale_y))
        supersample = 4
        card_layer = Image.new(
            "RGBA",
            (card_width * supersample, card_height * supersample),
            (0, 0, 0, 0),
        )
        ImageDraw.Draw(card_layer).rounded_rectangle(
            (0, 0, card_layer.width - 1, card_layer.height - 1),
            radius=max(1, round(18 * scale * supersample)),
            fill=surface_color,
            outline=accent_color,
            width=max(1, round(2 * scale * supersample)),
        )
        card_layer = card_layer.resize(
            (card_width, card_height),
            Image.Resampling.LANCZOS,
        )
        card_left = round((screenshot.width - card_width) / 2)
        card_top = round((screenshot.height - card_height) / 2)
        screenshot.paste(card_layer, (card_left, card_top), card_layer)
        card_is_in_backdrop = True

        overlay_image = ctk.CTkImage(light_image=screenshot, dark_image=screenshot, size=(width, height))
        overlay = ctk.CTkLabel(parent, text="", image=overlay_image, corner_radius=0)
        overlay.image = overlay_image
    except Exception:
        overlay = ctk.CTkFrame(parent, fg_color=fallback_overlay_color, corner_radius=0)
    overlay.place(x=0, y=0, relwidth=1, relheight=1)
    overlay.lift()

    result = {"confirmed": False}
    finished = ctk.BooleanVar(value=False)
    if card_is_in_backdrop:
        # This rectangular widget is inset far enough that all four of its
        # corners remain inside the visible rounded surface.
        dialog = ctk.CTkFrame(
            parent,
            width=dialog_width - 16,
            height=dialog_height - 16,
            bg_color=surface_color,
            fg_color=surface_color,
            corner_radius=0,
        )
    else:
        dialog = ctk.CTkFrame(
            parent,
            width=dialog_width,
            height=dialog_height,
            bg_color=fallback_overlay_color,
            fg_color=surface_color,
            corner_radius=18,
            border_width=2,
            border_color=accent,
        )
    dialog.place(relx=0.5, rely=0.5, anchor="center")
    dialog.grid_propagate(False)
    dialog.lift()
    dialog.grid_columnconfigure(0, weight=1)
    dialog.grid_rowconfigure(2, weight=1)

    heading = ctk.CTkFrame(dialog, fg_color="transparent")
    heading.grid(row=0, column=0, sticky="ew", padx=30, pady=(28, 4))
    ctk.CTkLabel(
        heading,
        text=media_format.upper(),
        width=54,
        height=30,
        corner_radius=8,
        fg_color=accent,
        text_color=("white", "white"),
        font=app_font(13, "bold"),
    ).pack(side="left", padx=(0, 12))
    ctk.CTkLabel(heading, text="Ready to convert?", font=app_font(25, "bold")).pack(side="left")
    ctk.CTkLabel(
        dialog,
        text="Check the file details below before starting.",
        text_color=("gray35", "gray70"),
    ).grid(row=1, column=0, sticky="w", padx=30, pady=(0, 18))

    details = ctk.CTkFrame(dialog, fg_color=("gray90", "gray18"), corner_radius=12)
    details.grid(row=2, column=0, sticky="nsew", padx=30)
    ctk.CTkLabel(details, text="FILE NAME", font=app_font(12, "bold"), text_color=("gray40", "gray60")).pack(
        anchor="w", padx=16, pady=(13, 2)
    )
    ctk.CTkLabel(details, text=file_name, anchor="w", justify="left", wraplength=460).pack(
        anchor="w", padx=16
    )
    ctk.CTkLabel(details, text="SAVE FOLDER", font=app_font(12, "bold"), text_color=("gray40", "gray60")).pack(
        anchor="w", padx=16, pady=(11, 2)
    )
    ctk.CTkLabel(details, text=target_folder, anchor="w", justify="left", wraplength=460).pack(
        anchor="w", padx=16, pady=(0, 13)
    )

    actions = ctk.CTkFrame(dialog, fg_color="transparent", height=48)
    actions.grid(row=3, column=0, sticky="ew", padx=30, pady=(24, 28))
    actions.grid_columnconfigure((0, 1), weight=1, uniform="dialog_action")

    def close_dialog(confirmed=False):
        result["confirmed"] = confirmed
        dialog.grab_release()
        finished.set(True)

    ctk.CTkButton(
        actions,
        text="Cancel",
        height=46,
        fg_color=("gray72", "gray28"),
        hover_color=("gray62", "gray35"),
        command=close_dialog,
    ).grid(row=0, column=0, sticky="ew", padx=(0, 7))
    confirm_button = ctk.CTkButton(
        actions,
        text=f"Convert to {media_format.upper()}",
        height=46,
        fg_color=accent,
        hover_color=hover,
        text_color=("white", "white"),
        command=lambda: close_dialog(True),
    )
    confirm_button.grid(row=0, column=1, sticky="ew", padx=(7, 0))
    dialog.bind("<Escape>", lambda _event: close_dialog())
    parent.update_idletasks()
    dialog.grab_set()
    confirm_button.focus_set()
    parent.wait_variable(finished)
    dialog.destroy()
    overlay.destroy()
    parent.update_idletasks()
    parent.focus_force()
    return result["confirmed"]


class EntryUndoManager:
    def __init__(self, entry):
        self.entry = entry
        self.variable = entry.cget("textvariable")
        self.undo_stack = []
        self.redo_stack = []
        entry.bind("<KeyPress>", self.on_key_press)
        entry.bind("<Control-v>", self.checkpoint)
        entry.bind("<Control-V>", self.checkpoint)
        entry.bind("<Control-x>", self.checkpoint)
        entry.bind("<Control-X>", self.checkpoint)
        entry.bind("<Control-BackSpace>", self.delete_previous_word)
        entry.bind("<Control-z>", self.undo)
        entry.bind("<Control-Z>", self.undo)
        entry.bind("<Control-y>", self.redo)
        entry.bind("<Control-Y>", self.redo)
        entry.bind("<Control-Shift-z>", self.redo)
        entry.bind("<Control-Shift-Z>", self.redo)

    def on_key_press(self, event):
        if not event.state & 0x4 and (event.char or event.keysym in ("BackSpace", "Delete")):
            self.checkpoint()

    def checkpoint(self, _event=None):
        current = self.entry.get()
        if not self.undo_stack or self.undo_stack[-1] != current:
            self.undo_stack.append(current)
            self.undo_stack = self.undo_stack[-100:]
        self.redo_stack.clear()

    def replace_text(self, value):
        self.variable.set(value)
        try:
            self.entry.icursor("end")
        except Exception:
            pass

    def undo(self, _event=None):
        if not self.undo_stack:
            return "break"
        current = self.entry.get()
        previous = self.undo_stack.pop()
        if previous == current and self.undo_stack:
            previous = self.undo_stack.pop()
        self.redo_stack.append(current)
        self.replace_text(previous)
        return "break"

    def redo(self, _event=None):
        if not self.redo_stack:
            return "break"
        current = self.entry.get()
        next_value = self.redo_stack.pop()
        self.undo_stack.append(current)
        self.replace_text(next_value)
        return "break"

    def delete_previous_word(self, _event=None):
        self.checkpoint()
        inner_entry = self.entry._entry
        try:
            if inner_entry.selection_present():
                inner_entry.delete("sel.first", "sel.last")
                return "break"
        except Exception:
            pass
        cursor = inner_entry.index("insert")
        before_cursor = inner_entry.get()[:cursor]
        match = re.search(r"\s*(?:\w+|[^\w\s]+)\s*$", before_cursor)
        if match:
            inner_entry.delete(match.start(), cursor)
        return "break"


def fetch_oembed_preview(url):
    endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"})
    request = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def app_font(size, weight="normal"):
    return ctk.CTkFont(family="Arial", size=size, weight=weight)


def format_accent_palette(media_format):
    if str(media_format).lower() == "mp3":
        return BLUE, BLUE_DARK, BLUE_HOVER, BLUE_HOVER_DARK
    return RED, RED_DARK, RED_HOVER, RED_HOVER_DARK


def interpolate_hex_color(start, end, progress):
    progress = max(0.0, min(1.0, float(progress)))
    start_rgb = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
    end_rgb = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
    channels = (
        round(start_channel + (end_channel - start_channel) * progress)
        for start_channel, end_channel in zip(start_rgb, end_rgb)
    )
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def interpolate_accent_palette(start_palette, end_palette, progress):
    return tuple(
        interpolate_hex_color(start, end, progress)
        for start, end in zip(start_palette, end_palette)
    )


def red_button(parent, **kwargs):
    return ctk.CTkButton(
        parent,
        fg_color=(RED, RED_DARK),
        hover_color=(RED_HOVER, RED_HOVER_DARK),
        text_color=("white", "white"),
        **kwargs,
    )


def red_option_menu(parent, **kwargs):
    # Dynamic resizing lets the selected label change the widget's requested
    # width, which shifts neighbouring controls when MP3 and MP4 use different
    # quality text. The grid already provides all required width.
    kwargs.setdefault("dynamic_resizing", False)
    return ctk.CTkOptionMenu(
        parent,
        fg_color=(RED, RED_DARK),
        button_color=(RED_DARK, RED_DARK),
        button_hover_color=(RED_HOVER, RED_HOVER_DARK),
        dropdown_hover_color=(RED_HOVER, RED_HOVER_DARK),
        text_color=("white", "white"),
        **kwargs,
    )


def prune_history_entries(entries, now=None):
    if not isinstance(entries, list):
        return []

    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=30)
    kept = []
    for original_entry in entries:
        if not isinstance(original_entry, dict):
            continue

        entry = dict(original_entry)
        created = None
        created_at = entry.get("created_at")
        if created_at:
            try:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=now.tzinfo)
                else:
                    created = created.astimezone(now.tzinfo)
            except (TypeError, ValueError):
                created = None

        if created is None:
            try:
                created = datetime.strptime(entry.get("time", ""), "%d %b, %H:%M").replace(
                    year=now.year,
                    tzinfo=now.tzinfo,
                )
                if created > now + timedelta(days=1):
                    created = created.replace(year=now.year - 1)
            except (TypeError, ValueError):
                created = now
            entry["created_at"] = created.isoformat(timespec="seconds")

        if created >= cutoff:
            kept.append(entry)
    return kept


def load_settings():
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(settings, dict):
        return {}

    try:
        settings_version = int(settings.get("settings_version", 1))
    except (TypeError, ValueError):
        settings_version = 1
    if settings_version < 2:
        # Older builds could force browser cookies for every request. The
        # repaired engine now works anonymously first, so authentication is
        # reset to an explicit fallback during this one-time migration.
        settings["youtube_browser"] = "Off"
        settings["settings_version"] = 2

    if settings.pop("use_brave_cookies", False):
        settings.setdefault("youtube_browser", "Off")
    if settings.get("youtube_browser") not in BROWSER_COOKIE_OPTIONS:
        settings["youtube_browser"] = "Off"

    old_folder = settings.pop("download_folder", None)
    if old_folder and Path(old_folder).expanduser().exists():
        settings.setdefault("mp3_download_folder", old_folder)
        settings.setdefault("mp4_download_folder", old_folder)

    for key in ("mp3_download_folder", "mp4_download_folder"):
        folder = settings.get(key)
        if not folder or not Path(folder).expanduser().exists():
            settings.pop(key, None)
    for key in ("mp3_history", "mp4_history"):
        history = settings.get(key)
        if not isinstance(history, list):
            settings[key] = []
        else:
            settings[key] = prune_history_entries(history)
    return settings


def save_settings(settings):
    temporary_path = SETTINGS_PATH.with_suffix(".json.tmp")
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("w", encoding="utf-8") as settings_file:
            json.dump(settings, settings_file, indent=2)
            settings_file.flush()
            os.fsync(settings_file.fileno())
        temporary_path.replace(SETTINGS_PATH)
    except OSError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def parse_percent(value):
    match = re.search(r"([\d.]+)%", value.replace("\x1b", ""))
    if not match:
        return 0
    try:
        return max(0, min(100, float(match.group(1))))
    except ValueError:
        return 0


def is_age_restriction_error(value):
    message = value.casefold()
    return any(phrase in message for phrase in (
        "sign in to confirm your age",
        "age-restricted",
        "age restricted",
        "confirm your age",
    ))


def protected_video_guidance(browser_name, *, cookie_unavailable=False):
    if cookie_unavailable:
        return (
            f"Could not read the {browser_name} login. Fully close {browser_name}, "
            f"including background windows, then choose {browser_name} again."
        )
    if browser_name == "Off":
        return (
            "Age verification is required. Sign in to YouTube in a browser, then choose "
            "that browser under Login fallback."
        )
    return (
        f"YouTube did not accept the {browser_name} session for age verification. "
        f"Make sure this video plays while signed in to {browser_name}, fully close the "
        f"browser, then choose {browser_name} again."
    )


def sanitize_filename(value):
    name = value.strip()
    name = re.sub(r"\.(mp3|mp4)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")[:180].rstrip(" .")
    if name.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        name += "_"
    return name


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def best_resolution(info):
    heights = [fmt.get("height") for fmt in info.get("formats", []) if fmt.get("height")]
    if not heights:
        return ""
    return f"Up to {max(heights)}p"


def fetch_thumbnail(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = response.read()
    image = Image.open(BytesIO(data)).convert("RGB")
    image.thumbnail((360, 204), Image.Resampling.LANCZOS)
    return image


def safe_fetch_thumbnail(url):
    try:
        return fetch_thumbnail(url)
    except Exception:
        return None


def reveal_file(file_path):
    path = Path(file_path)
    if not path.is_file():
        messagebox.showwarning("File not found", "This converted file has been moved or deleted.")
        return
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        item_id_list = ctypes.c_void_p()
        shell32.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        result = shell32.SHParseDisplayName(str(path.resolve()), None, ctypes.byref(item_id_list), 0, None)
        if result != 0:
            raise OSError("Windows could not resolve the file path")
        try:
            result = shell32.SHOpenFolderAndSelectItems(item_id_list, 0, None, 0)
            if result != 0:
                raise OSError("Windows could not select the file")
        finally:
            ole32.CoTaskMemFree(item_id_list)
    except Exception:
        try:
            subprocess.Popen(f'explorer.exe /select,"{path.resolve()}"')
        except OSError:
            messagebox.showwarning("Could not open folder", "Windows File Explorer could not be opened.")


def open_folder(folder):
    try:
        path = Path(folder).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)
    except OSError:
        pass


def run_packaged_self_test(output_path):
    """Write machine-readable engine health without opening the GUI."""
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        health = inspect_engine()
        result = {
            "ready": health.ready,
            "summary": health.summary(),
            "details": health.details(),
            "problems": list(health.problems),
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": str(Path(sys.executable).resolve()),
        }
    except Exception as exc:
        result = {
            "ready": False,
            "summary": "Self-test crashed",
            "details": f"{type(exc).__name__}: {exc}",
            "problems": [str(exc)],
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": str(Path(sys.executable).resolve()),
        }
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(run_packaged_self_test(sys.argv[2]))
    YouTubeConverterApp().mainloop()

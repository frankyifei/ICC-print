"""Toga GUI for the ICC spooler.

Single entry point for the program: edit the color/settings config, start and
stop the background spool service, watch live status and the log, and keep
running in the system tray (pystray) so it can autostart minimized.

Run with:  python -m icc_spooler gui [--config config.json] [--minimized]
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import toga
from toga.dialogs import (
    ErrorDialog, InfoDialog, OpenFileDialog, QuestionDialog, SaveFileDialog,
    SelectFolderDialog,
)
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

from . import autostart, imageops, printer_admin
from .config import (
    AppConfig, ColorSettings, INTENT_NAMES, INTENT_VALUES, default_config_path,
)
from .controller import ServiceController
from .i18n import available_languages, set_language, tr
from .service import list_printers
from .tray import TrayIcon

# Display labels are English source strings (translated via tr() when the UI is
# built, so the language must be set before the widgets are created).
TAC_MODES = (("scale_cmy", "Scale CMY only (keep K)"), ("scale_all", "Scale whole CMYK"))
CMM_MODES = (("lcms", "Built-in littleCMS (Pillow)"), ("wcs", "Windows WCS (mscms)"))
INTENT_LABELS = ("Perceptual", "Relative Colorimetric", "Saturation", "Absolute Colorimetric")
MAX_LOG_LINES = 800


def _row(label: str, widget, label_width: int = 180) -> toga.Box:
    return toga.Box(
        style=Pack(direction=ROW, margin=(3, 0, 3, 0)),
        children=[
            toga.Label(label, style=Pack(width=label_width, margin=(6, 6, 6, 6))),
            widget,
        ],
    )


def _field(value: str = "") -> toga.TextInput:
    return toga.TextInput(value=value, style=Pack(flex=1, margin=(6, 6, 6, 6)))


def _num(value, minimum=None, maximum=None, step=1) -> toga.NumberInput:
    return toga.NumberInput(value=value, min=minimum, max=maximum, step=step,
                            style=Pack(flex=1, margin=(6, 6, 6, 6)))


def _switch(text: str, value: bool = False) -> toga.Switch:
    return toga.Switch(text, value=value, style=Pack(margin=(4, 6, 4, 6)))


class IccSpoolerApp(toga.App):
    PV_WHEEL = "Color wheel / swatches"
    PV_IMAGE = "Custom image"

    # Widgets (by attribute name) that feed _collect(); changing any one of them
    # auto-saves config.json after a short idle delay.
    AUTOSAVE_ATTRS = (
        "printer", "spool_dir", "archive_dir", "spool_pattern", "poll_seconds",
        "sw_dryrun", "vp_name", "vp_port",
        "sw_auto_service", "sw_autostart", "sw_startmin", "sw_minimize",
        "enabled", "k_reduction", "k_curve_enabled", "k_curve_min",
        "k_curve_max", "k_curve_power", "preserve_black", "black_threshold",
        "cmy_c", "cmy_m", "cmy_y", "c_gamma", "m_gamma", "y_gamma", "k_gamma",
        "gcr", "ucr", "rich_black", "rich_black_threshold", "tac_limit",
        "tac_mode", "rgb_to_cmyk", "gray_to_k", "neutral_to_k",
        "neutral_threshold", "neutral_min", "neutral_vector_only",
        "text_solid_black", "text_k_gain", "process_images",
        "target_icc", "input_rgb_icc", "intent", "cmm", "lang",
    )
    AUTOSAVE_DELAY = 0.8

    def __init__(self, config_path=None, start_minimized: bool = False):
        super().__init__(formal_name="ICC Spooler", app_name="icc_spooler",
                         app_id="com.iccspooler.app", author="ICC Spooler",
                         version="0.1")
        self._config_path = Path(config_path) if config_path else default_config_path()
        self._start_minimized = bool(start_minimized)
        self._loading = False
        self._quitting = False

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._cmd_queue: "queue.Queue[str]" = queue.Queue()
        self._log_buffer: list[str] = []
        self._tray: TrayIcon | None = None
        self._tray_ok = False
        self._controller: ServiceController | None = None
        self._last_running: bool | None = None
        self._pump_task = None
        self._single_instance = None
        self._autosave_at = None
        self._param_groups: list = []

    # -------------------------------------------------------------- lifecycle
    def startup(self):
        self._cfg = self._load_config()
        set_language(self._cfg.lang)
        self._controller = ServiceController(self._log_queue, self._cfg.log_path())
        self._build_ui()
        self._apply_config_to_ui(self._cfg)
        self.main_window = toga.MainWindow(title=tr('ICC Spooler Settings'), size=(860, 700),
                                           on_close=self._on_close)
        self.main_window.content = self._root

    async def on_running(self):
        self._tray = TrayIcon(self._cmd_queue)
        self._tray_ok = self._tray.start()
        if self._tray_ok:
            self._log(tr('[gui] Tray icon enabled'))
        else:
            self._log(tr('[gui] Tray not enabled (pystray unavailable)'))

        if self._start_minimized and self._tray_ok:
            self.main_window.hide()
        else:
            self.main_window.show()

        if self._cfg.auto_start_service:
            self._start_service()
        else:
            self._log(tr('[gui] Service not auto-started (auto_start_service=false)'))

        self._pump_task = asyncio.create_task(self._pump())

    async def on_exit(self):
        self._shutdown()

    def _shutdown(self):
        if self._quitting:
            return
        self._quitting = True
        try:
            if self._controller is not None and self._controller.is_running():
                self._controller.stop()
            if self._tray is not None:
                self._tray.stop()
            self._save_config(quiet=True)
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self, window, **kwargs):
        if self._cfg.minimize_to_tray and self._tray_ok:
            window.hide()
            self._set_message(tr('Minimized to tray; the service keeps running in the background.'))
            return False
        self._shutdown()
        return True

    # --------------------------------------------------------------- background
    async def _pump(self):
        while True:
            self._drain_commands()
            self._drain_logs()
            self._refresh_status()
            self._check_activation()
            self._pv_autorefresh()
            self._maybe_autosave()
            await asyncio.sleep(0.4)

    def _check_activation(self):
        if self._single_instance is None:
            return
        try:
            activated = self._single_instance.consume_signal()
        except Exception:  # noqa: BLE001
            return
        if activated:
            self._ensure_tray()
            self.main_window.show()
            self._set_message(tr('The program is already running; showing the existing window.'))

    def _ensure_tray(self):
        """(Re)create the tray icon if it is missing or its thread died."""
        if self._tray is not None and self._tray.is_alive():
            return
        self._tray = TrayIcon(self._cmd_queue)
        self._tray_ok = self._tray.start()
        self._last_running = None  # force icon refresh next pump tick
        self._log(tr('[gui] Tray icon restored') if self._tray_ok else tr('[gui] Tray restore failed'))

    def _drain_commands(self):
        while True:
            try:
                cmd = self._cmd_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == "show":
                self.main_window.show()
            elif cmd == "start":
                self._start_service()
            elif cmd == "stop":
                self._stop_service()
            elif cmd == "open-log":
                self._open_log()
            elif cmd == "quit":
                self._shutdown()
                self.exit()
                return

    def _drain_logs(self):
        lines: list[str] = []
        while True:
            try:
                lines.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self._log_buffer.extend(lines)
        if len(self._log_buffer) > MAX_LOG_LINES:
            self._log_buffer = self._log_buffer[-MAX_LOG_LINES:]
        self.log_view.value = "\n".join(self._log_buffer)

    def _refresh_status(self):
        if self._controller is None:
            return
        running = self._controller.is_running()
        if running != self._last_running:
            self._last_running = running
            self.status_label.text = tr('● Service: running') if running else tr('○ Service: stopped')
            self.btn_start.enabled = not running
            self.btn_stop.enabled = running
            if self._tray is not None:
                self._tray.set_running(running)
        st = self._controller.stats()
        self.stats_label.text = tr('Jobs {jobs} · Errors {errors} · Last: {last}').format(
            jobs=st['jobs'], errors=st['errors'], last=st['last'] or '—')

    def _log(self, msg: str) -> None:
        if self._controller is not None:
            self._controller.log(msg)

    def _set_message(self, text: str, error: bool = False) -> None:
        self.message_label.text = ("⚠ " if error else "") + text

    # --------------------------------------------------------------- service
    def _start_service(self):
        if self._controller is None or self._controller.is_running():
            return
        try:
            cfg = self._collect()
        except Exception as exc:  # noqa: BLE001
            self._set_message(tr('Failed to read config: {exc}').format(exc=exc), error=True)
            return
        self._controller.start(cfg)
        self._last_running = None
        self._set_message(tr('Service started.'))

    def _stop_service(self):
        if self._controller is None:
            return
        self._controller.stop()
        self._last_running = None
        self._set_message(tr('Service stopped.'))

    def _open_log(self):
        path = self._controller.log_file if self._controller else None
        if path is None or not Path(path).exists():
            self._set_message(tr('Log file does not exist yet.'), error=True)
            return
        try:
            os.startfile(str(path))  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            self._set_message(tr('Cannot open log: {exc}').format(exc=exc), error=True)

    # --------------------------------------------------------------- UI build
    def _build_ui(self):
        self._build_service_tab()
        self._build_basic_tab()
        self._build_printer_tab()
        self._build_color_tab()
        self._build_preview_tab()

        tabs = toga.OptionContainer(
            style=Pack(flex=1),
            content=[
                toga.OptionItem(tr('Service'), self._tab_service),
                toga.OptionItem(tr('General'), self._tab_basic),
                toga.OptionItem(tr('Virtual Printer'), self._tab_printer),
                toga.OptionItem(tr('Color Conversion'), self._tab_color),
                toga.OptionItem(tr('Preview'), self._tab_preview),
            ],
        )
        self._tabs = tabs
        self.status_label = toga.Label(tr('○ Service: stopped'), style=Pack(margin=(8, 8, 8, 8)))
        self.message_label = toga.Label("", style=Pack(margin=(4, 8, 4, 8)))

        header = toga.Box(
            style=Pack(direction=ROW, margin=(4, 6, 4, 6)),
            children=[
                toga.Label(tr('ICC Spooler · Print Color Processing Service'),
                           style=Pack(flex=1, margin=(4, 4, 4, 4))),
                self.status_label,
            ],
        )

        button_bar = toga.Box(
            style=Pack(direction=ROW, margin=(4, 6, 6, 6)),
            children=[
                toga.Button(tr('Open Config…'), on_press=self._on_open_config, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Save'), on_press=self._on_save, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Save As…'), on_press=self._on_save_as, style=Pack(margin=(0, 6, 0, 0))),
                toga.Box(style=Pack(flex=1)),
                toga.Button(tr('Validate'), on_press=self._on_validate, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Restore defaults'), on_press=self._on_reset, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Hide to Tray'), on_press=self._on_hide, style=Pack(margin=(0, 6, 0, 0))),
            ],
        )

        self._root = toga.Box(
            style=Pack(direction=COLUMN, margin=6),
            children=[header, tabs, self.message_label, button_bar],
        )
        self._wire_autosave()

    def _wire_autosave(self):
        for name in self.AUTOSAVE_ATTRS:
            widget = getattr(self, name, None)
            if widget is None or not hasattr(widget, "on_change"):
                continue
            widget.on_change = self._make_change_handler(
                getattr(widget, "on_change", None))

    def _make_change_handler(self, previous):
        # `previous` (if any) is already a Toga-wrapped handler that prepends the
        # widget, so it must be called with no arguments.
        def handler(widget=None, **kwargs):
            if previous is not None:
                previous()
            self._schedule_autosave()
        return handler

    def _schedule_autosave(self):
        if self._loading:
            return
        self._autosave_at = time.monotonic()

    def _maybe_autosave(self):
        if self._autosave_at is None or self._loading:
            return
        if time.monotonic() - self._autosave_at < self.AUTOSAVE_DELAY:
            return
        self._autosave_at = None
        self._save_config(quiet=True)
        self._set_message(tr('Config auto-saved.'))

    def _build_service_tab(self):
        self.btn_start = toga.Button(tr('Start Service'), on_press=self._on_start, style=Pack(margin=(0, 6, 0, 0)))
        self.btn_stop = toga.Button(tr('Stop Service'), on_press=self._on_stop, enabled=False,
                                    style=Pack(margin=(0, 6, 0, 0)))
        self.stats_label = toga.Label(tr('Jobs 0 · Errors 0 · Last: —'), style=Pack(flex=1, margin=(6, 6, 6, 6)))

        self.sw_auto_service = _switch(tr('Start service when the GUI opens'), True)
        self.sw_autostart = _switch(tr('Start on login (run this program at sign-in)'), False)
        self.sw_startmin = _switch(tr('Minimize to tray on start'), False)
        self.sw_minimize = _switch(tr('Minimize to tray on window close (service keeps running)'), True)
        self.sw_dryrun = _switch(tr('Dry run: do not send to the real printer, only produce intermediate files'), False)
        self.sw_autostart.on_change = self._on_toggle_autostart
        self.sw_startmin.on_change = self._on_toggle_startmin
        self.sw_minimize.on_change = self._on_toggle_minimize
        self.sw_auto_service.on_change = self._on_toggle_auto_service

        self.btn_open_log = toga.Button(tr('Open Log File'), on_press=self._on_open_log,
                                        style=Pack(margin=(0, 6, 0, 0)))
        self.btn_open_archive = toga.Button(tr('Open Archive Folder'), on_press=self._on_open_archive,
                                            style=Pack(margin=(0, 6, 0, 0)))
        self.btn_clear_log = toga.Button(tr('Clear Display'), on_press=self._on_clear_log,
                                         style=Pack(margin=(0, 6, 0, 0)))
        self.log_view = toga.MultilineTextInput(readonly=True, style=Pack(flex=1, margin=(6, 6, 6, 6)))

        box = toga.Box(
            style=Pack(direction=COLUMN, margin=6),
            children=[
                _row(tr('Service Control'), toga.Box(style=Pack(direction=ROW, flex=1),
                                          children=[self.btn_start, self.btn_stop])),
                _row(tr('Processing Stats'), self.stats_label),
                toga.Box(style=Pack(height=1, margin=(6, 0, 6, 0))),
                toga.Label(tr('Startup & Residency'), style=Pack(margin=(4, 6, 4, 6))),
                _row("", self.sw_auto_service),
                _row("", self.sw_autostart),
                _row("", self.sw_startmin),
                _row("", self.sw_minimize),
                _row("", self.sw_dryrun),
                toga.Box(style=Pack(height=1, margin=(6, 0, 6, 0))),
                toga.Box(style=Pack(direction=ROW, margin=(4, 6, 4, 6)),
                         children=[toga.Label(tr('Run Log'), style=Pack(flex=1)),
                                   self.btn_open_archive, self.btn_open_log, self.btn_clear_log]),
                self.log_view,
            ],
        )
        self._tab_service = box

    def _build_basic_tab(self):
        self.enabled = _switch(tr('Enable color processing'), True)
        self.printer = toga.Selection(items=[], style=Pack(flex=1, margin=(6, 6, 6, 6)))
        self.spool_dir = _field()
        self.archive_dir = _field()
        self.spool_pattern = _field()
        self.poll_seconds = _num(1.0, 0.1, 60.0, 0.5)
        self._lang_items = available_languages()
        self._lang_name_to_code = {name: code for code, name in self._lang_items.items()}
        self.lang = toga.Selection(items=list(self._lang_name_to_code),
                                   style=Pack(flex=1, margin=(6, 6, 6, 6)))
        self.lang.on_change = self._on_lang_change
        body = toga.Box(
            style=Pack(direction=COLUMN, margin=6),
            children=[
                _row("", self.enabled),
                _row(tr('Target printer queue'), self._printer_row()),
                _row(tr('Watch folder (spool)'), self._path_row(self.spool_dir, directory=True)),
                _row(tr('Archive folder (processed)'), self._path_row(self.archive_dir, directory=True)),
                _row(tr('Watch filename pattern'), self.spool_pattern),
                _row(tr('Poll interval (seconds)'), self.poll_seconds),
                _row(tr('Language'), self.lang),
            ],
        )
        self._tab_basic = toga.ScrollContainer(content=body, style=Pack(flex=1))

    def _build_printer_tab(self):
        self.vp_name = _field()
        self.vp_driver = toga.Selection(items=[], style=Pack(flex=1, margin=(6, 6, 6, 6)))
        self.vp_port = _field()
        self.vp_status = toga.Label(tr('Not checked yet.'), style=Pack(margin=(6, 6, 6, 6)))
        self.vp_sync_spool = _switch(tr('After creating, sync the service watch folder to the folder of the port file'), True)

        driver_row = toga.Box(
            style=Pack(direction=ROW, flex=1),
            children=[self.vp_driver,
                      toga.Button(tr('Refresh drivers'), on_press=self._on_vp_refresh,
                                  style=Pack(width=96, margin=(0, 6, 0, 0)))],
        )
        buttons = toga.Box(
            style=Pack(direction=ROW, margin=(4, 6, 4, 6)),
            children=[
                toga.Button(tr('Check status'), on_press=self._on_vp_check, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Create / Update'), on_press=self._on_vp_create, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Delete'), on_press=self._on_vp_delete, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Set as default'), on_press=self._on_vp_default, style=Pack(margin=(0, 6, 0, 0))),
            ],
        )
        body = toga.Box(
            style=Pack(direction=COLUMN, margin=6),
            children=[
                toga.Label(
                    tr('Virtual printer: the port points to a file (local file port). Printing to this queue writes a PostScript file; the service then does color processing and forwards it to the real queue.'),
                    style=Pack(margin=(4, 6, 4, 6))),
                _row(tr('Queue name'), self.vp_name),
                _row(tr('Driver (PostScript)'), driver_row),
                _row(tr('Port file'), self.vp_port),
                _row("", self.vp_sync_spool),
                _row(tr('Status'), self.vp_status),
                buttons,
                toga.Label(
                    tr('Note: create/delete/set-default need administrator rights and will show a UAC prompt. The file port overwrites the same file for each job, so print sequentially (do not run jobs concurrently).'),
                    style=Pack(margin=(8, 6, 4, 6))),
            ],
        )
        self._tab_printer = toga.ScrollContainer(content=body, style=Pack(flex=1))

    def _build_k_section(self) -> toga.Box:
        self.k_reduction = _num(0.0, 0.0, 99.9, 1.0)
        self.k_curve_enabled = _switch(tr('Enable dynamic K curve (grade reduction by K value)'))
        self.k_curve_min = _num(0.0, 0.0, 100.0, 1.0)
        self.k_curve_max = _num(100.0, 0.0, 100.0, 1.0)
        self.k_curve_power = _num(1.0, 0.1, 5.0, 0.1)
        self.preserve_black = _switch(tr('Preserve pure black (do not change K above the threshold)'))
        self.black_threshold = _num(95.0, 0.0, 100.0, 1.0)
        self.k_toggle = _switch(tr('Show K-reduction settings'), False)
        self.k_toggle.on_change = self._on_toggle_k
        self._k_anchor = _row("", self.k_toggle)
        self._k_body = toga.Box(
            style=Pack(direction=COLUMN),
            children=[
                _row(tr('K-reduction strength (%)'), self.k_reduction),
                _row("", self.k_curve_enabled),
                _row(tr('Curve start K (%)'), self.k_curve_min),
                _row(tr('Curve end K (%)'), self.k_curve_max),
                _row(tr('Curve power'), self.k_curve_power),
                _row("", self.preserve_black),
                _row(tr('Pure-black threshold K (%)'), self.black_threshold),
            ],
        )
        self._k_section = toga.Box(
            style=Pack(direction=COLUMN, margin=(0, 0, 0, 0)),
            children=[
                toga.Label(tr('K Reduction'), style=Pack(margin=(4, 6, 4, 6))),
                self._k_anchor,
            ],
        )
        return self._k_section

    def _build_color_tab(self):
        self.target_icc = _field()
        self.input_rgb_icc = _field()
        self._intent_labels = {tr(lbl): name
                               for lbl, name in zip(INTENT_LABELS, INTENT_NAMES)}
        self._intent_name_to_label = {name: tr(lbl)
                                      for lbl, name in zip(INTENT_LABELS, INTENT_NAMES)}
        self._cmm_labels = {tr(lbl): code for code, lbl in CMM_MODES}
        self._cmm_code_to_label = {code: tr(lbl) for code, lbl in CMM_MODES}
        self.intent = toga.Selection(items=list(self._intent_labels), style=Pack(flex=1, margin=(6, 6, 6, 6)))
        self.cmm = toga.Selection(items=list(self._cmm_labels), style=Pack(flex=1, margin=(6, 6, 6, 6)))
        self.rgb_to_cmyk = _switch(tr('Force RGB to CMYK (automatic when there is a CMYK adjustment)'))
        self.gray_to_k = _switch(tr('Force gray to K (automatic when there is a CMYK adjustment)'))
        self.neutral_to_k = _switch(tr('Route neutral gray to K (near-neutral RGB to pure K, removes gray cast)'))
        self.neutral_vector_only = _switch(tr('Apply to vector content only (do not route images to K)'), True)
        self.neutral_threshold = _num(0.04, 0.0, 1.0, 0.01)
        self.neutral_min = _num(0.05, 0.0, 1.0, 0.01)
        self.text_solid_black = _switch(tr('Print all neutral vector text as single-channel K (matches Acrobat; no 4-color rich black on glyph strokes)'))
        self.text_k_gain = _num(1.20, 0.0, 3.0, 0.05)
        self.process_images = _switch(tr('Process image pixels (automatic when there is a CMYK adjustment)'), True)

        k_section = self._build_k_section()
        sep_section = self._build_sep_section()

        body = toga.Box(style=Pack(direction=COLUMN, margin=6))
        self._color_body = body
        # Subordinate parameters live in a section that is hidden while the
        # switch is off (one shared rule, see `_attach_params`).
        self._neutral_anchor, self._neutral_body = self._attach_params(
            body, self.neutral_to_k,
            [
                _row("", self.neutral_vector_only),
                _row(tr('Neutral-gray threshold (0..1, e.g. 0.04)'), self.neutral_threshold),
                _row(tr('Neutral-gray floor (keep ICC below this lightness to avoid near-black turning gray)'), self.neutral_min),
            ],
        )
        self._text_anchor, self._text_body = self._attach_params(
            body, self.text_solid_black,
            [
                _row(tr('Text K gain (0..3; 1.2 reproduces Acrobat\'s tints, e.g. src 0.29 -> K 85%)'), self.text_k_gain),
            ],
        )

        body.add(
            toga.Label(tr('Color Conversion'), style=Pack(margin=(4, 6, 4, 6))),
            toga.Label(
                tr('Auto mode: whenever any CMYK adjustment is set (K reduction / CMY scaling / separation, etc.), RGB and gray content is first converted to CMYK and then adjusted; image pixels and the preview are handled too, with no manual checking needed. The three switches above only force conversion when there is no CMYK adjustment.'),
                style=Pack(margin=(4, 6, 8, 6))),
            _row(tr('Target ICC (CMYK)'), self._path_row(self.target_icc, directory=False)),
            _row(tr('Input RGB ICC (optional)'), self._path_row(self.input_rgb_icc, directory=False)),
            _row(tr('Rendering intent'), self.intent),
            _row(tr('Color engine (CMM)'), self.cmm),
            _row("", self.rgb_to_cmyk),
            _row("", self.gray_to_k),
            self._neutral_anchor,
            self._text_anchor,
            _row("", self.process_images),
            toga.Box(style=Pack(height=1, margin=(8, 0, 8, 0))),
            k_section,
            toga.Box(style=Pack(height=1, margin=(8, 0, 8, 0))),
            sep_section,
        )
        self._tab_color = toga.ScrollContainer(content=body, style=Pack(flex=1))

    def _build_sep_section(self) -> toga.Box:
        self.cmy_c = _num(1.0, 0.0, 3.0, 0.01)
        self.cmy_m = _num(1.0, 0.0, 3.0, 0.01)
        self.cmy_y = _num(1.0, 0.0, 3.0, 0.01)
        self.c_gamma = _num(1.0, 0.1, 5.0, 0.05)
        self.m_gamma = _num(1.0, 0.1, 5.0, 0.05)
        self.y_gamma = _num(1.0, 0.1, 5.0, 0.05)
        self.k_gamma = _num(1.0, 0.1, 5.0, 0.05)
        self.gcr = _num(0.0, 0.0, 1.0, 0.05)
        self.ucr = _num(0.0, 0.0, 1.0, 0.05)
        self.rich_black = _num(0.0, 0.0, 1.0, 0.05)
        self.rich_black_threshold = _num(0.9, 0.0, 1.0, 0.05)
        self.tac_limit = _num(0.0, 0.0, 4.0, 0.1)
        self._tac_labels = {tr(lbl): code for code, lbl in TAC_MODES}
        self._tac_code_to_label = {code: tr(lbl) for code, lbl in TAC_MODES}
        self.tac_mode = toga.Selection(items=list(self._tac_labels), style=Pack(flex=1, margin=(6, 6, 6, 6)))

        self.sep_toggle = _switch(tr('Show separation & total-ink settings'), False)
        self.sep_toggle.on_change = self._on_toggle_sep
        self._sep_anchor = _row("", self.sep_toggle)
        self._sep_body = toga.Box(
            style=Pack(direction=COLUMN),
            children=[
                toga.Label(tr('CMY channel scaling'), style=Pack(margin=(4, 6, 4, 6))),
                _row("C", self.cmy_c), _row("M", self.cmy_m), _row("Y", self.cmy_y),
                toga.Label(tr('Channel Gamma'), style=Pack(margin=(8, 6, 4, 6))),
                _row("C", self.c_gamma), _row("M", self.m_gamma),
                _row("Y", self.y_gamma), _row("K", self.k_gamma),
                toga.Label(tr('Black generation / total ink'), style=Pack(margin=(8, 6, 4, 6))),
                _row(tr('GCR (gray component replacement)'), self.gcr),
                _row(tr('UCR (under color removal)'), self.ucr),
                _row(tr('Rich black'), self.rich_black),
                _row(tr('Rich-black threshold K'), self.rich_black_threshold),
                _row(tr('Total ink limit TAC (0 = off)'), self.tac_limit),
                _row(tr('TAC mode'), self.tac_mode),
            ],
        )
        self._sep_section = toga.Box(
            style=Pack(direction=COLUMN),
            children=[
                toga.Label(tr('Separation & Total Ink'), style=Pack(margin=(4, 6, 4, 6))),
                self._sep_anchor,
            ],
        )
        return self._sep_section

    def _collapse(self, parent, section, anchor, visible):
        """Show/hide a section by adding/removing it, so siblings reflow.

        Toga's `visibility = HIDDEN` only hides the native widget and does not
        reflow the layout, so a removed-then-reinserted child is used instead.
        """
        present = section in parent.children
        if visible and not present:
            parent.insert(parent.index(anchor) + 1, section)
        elif not visible and present:
            parent.remove(section)

    def _attach_params(self, parent, switch, rows):
        """General rule: a switch's subordinate parameters hide while it is off.

        Builds the anchor row for `switch` plus a parameters box from `rows`,
        registers the pair so the box follows the switch on every toggle and can
        be restored from config via `_sync_param_groups`, and returns
        `(anchor, body)`. Put the anchor where the switch should appear; do not
        add the body to the layout yourself.
        """
        anchor = _row("", switch)
        body = toga.Box(style=Pack(direction=COLUMN), children=list(rows))

        def _on_change(widget, **kwargs):
            self._collapse(parent, body, anchor, bool(widget.value))

        switch.on_change = _on_change
        self._param_groups.append((parent, anchor, body, switch))
        return anchor, body

    def _sync_param_groups(self):
        for parent, anchor, body, switch in self._param_groups:
            self._collapse(parent, body, anchor, bool(switch.value))

    def _on_toggle_k(self, widget, **kwargs):
        self._collapse(self._k_section, self._k_body, self._k_anchor, widget.value)

    def _on_toggle_sep(self, widget, **kwargs):
        self._collapse(self._sep_section, self._sep_body, self._sep_anchor, widget.value)

    def _build_preview_tab(self):
        self._pv_source = "wheel"
        self._pv_custom_pil = None
        self._pv_chart_cache = None
        self._pv_toga = None
        self._pv_w = 0
        self._pv_h = 0
        self._pv_cw = 0
        self._pv_ch = 0
        self._pv_scale = 1.0
        self._pv_tx = 0.0
        self._pv_ty = 0.0
        self._pv_refit = True
        self._pv_sig = None
        self._pv_rendering = False
        self._pv_drag = None

        self._pv_wheel_label = tr(self.PV_WHEEL)
        self._pv_image_label = tr(self.PV_IMAGE)
        self.pv_source = toga.Selection(
            items=[self._pv_wheel_label, self._pv_image_label],
            style=Pack(width=160, margin=(6, 6, 6, 6)))
        self.pv_source.value = self._pv_wheel_label
        self.pv_source.on_change = self._on_pv_source_change
        self.pv_zoom_label = toga.Label("100%", style=Pack(width=60, margin=(6, 6, 6, 6)))

        controls = toga.Box(
            style=Pack(direction=ROW, margin=(4, 6, 4, 6)),
            children=[
                toga.Label(tr('Preview source'), style=Pack(margin=(8, 6, 8, 6))),
                self.pv_source,
                toga.Button(tr('Choose Image…'), on_press=self._on_pv_pick, style=Pack(margin=(0, 6, 0, 0))),
                toga.Box(style=Pack(flex=1)),
                toga.Button(tr('Refresh Preview'), on_press=self._on_pv_refresh, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Zoom Out −'), on_press=self._on_pv_zoom_out, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Zoom In +'), on_press=self._on_pv_zoom_in, style=Pack(margin=(0, 6, 0, 0))),
                toga.Button(tr('Reset'), on_press=self._on_pv_reset, style=Pack(margin=(0, 6, 0, 0))),
                self.pv_zoom_label,
            ],
        )
        self.pv_canvas = toga.Canvas(
            style=Pack(flex=1, margin=(6, 6, 6, 6)),
            on_resize=self._on_pv_resize,
            on_press=self._on_pv_press,
            on_drag=self._on_pv_drag,
            on_release=self._on_pv_release,
        )
        hint = toga.Label(
            tr('Left = original, right = converted with the current color settings (target ICC soft proof). Whenever any CMYK adjustment is set, the preview shows the effect (RGB/gray auto-converted to CMYK then adjusted). Drag to pan, buttons to zoom.'),
            style=Pack(margin=(2, 6, 2, 6)))
        self._tab_preview = toga.Box(
            style=Pack(direction=COLUMN, margin=6),
            children=[controls, hint, self.pv_canvas],
        )

    # ------------------------------------------------------------------ preview
    @staticmethod
    def _pv_font(size: int = 14):
        from PIL import ImageFont
        for path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
                     r"C:\Windows\Fonts\simhei.ttf"):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except Exception:  # noqa: BLE001
                    pass
        return ImageFont.load_default()

    def _pv_chart(self):
        if self._pv_chart_cache is not None:
            return self._pv_chart_cache
        import colorsys
        import math

        from PIL import Image, ImageDraw

        W, H = 420, 300
        img = Image.new("RGB", (W, H), (255, 255, 255))
        px = img.load()
        cx, cy, R = 130, 128, 118
        for yy in range(cy - R, cy + R):
            for xx in range(cx - R, cx + R):
                dx, dy = xx - cx, yy - cy
                r = math.hypot(dx, dy)
                if r <= R:
                    hue = (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0
                    rr, gg, bb = colorsys.hsv_to_rgb(hue / 360.0, r / R, 1.0)
                    px[xx, yy] = (int(rr * 255), int(gg * 255), int(bb * 255))

        draw = ImageDraw.Draw(img)
        patches = [
            (255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255),
            (255, 0, 255), (255, 255, 0), (0, 0, 0), (255, 255, 255),
            (128, 128, 128), (200, 180, 150), (90, 120, 80), (40, 80, 160),
            (230, 120, 60), (120, 60, 90), (60, 160, 120), (180, 140, 90),
        ]
        x0, y0, pw, ph, cols = 275, 16, 66, 40, 2
        for i, col in enumerate(patches):
            c, r = i % cols, i // cols
            x = x0 + c * (pw + 6)
            y = y0 + r * (ph + 6)
            if y + ph > H - 44:
                break
            draw.rectangle([x, y, x + pw - 1, y + ph - 1], fill=col, outline=(120, 120, 120))

        ramp = Image.new("L", (256, 1))
        ramp.putdata(list(range(256)))
        ramp = ramp.convert("RGB").resize((W, 36))
        img.paste(ramp, (0, H - 36))
        self._pv_chart_cache = img
        return img

    def _pv_source_pil(self):
        if self._pv_source == "image" and self._pv_custom_pil is not None:
            return self._pv_custom_pil
        return self._pv_chart()

    @staticmethod
    def _cmyk_to_rgb(cmyk_img, target_icc: str, intent_name: str):
        from PIL import Image, ImageCms
        if target_icc and Path(target_icc).is_file():
            try:
                src = ImageCms.getOpenProfile(str(Path(target_icc).resolve()))
                dst = ImageCms.createProfile("sRGB")
                intent = INTENT_VALUES.get(intent_name, 1)
                transform = ImageCms.buildTransform(src, dst, "CMYK", "RGB", intent, 0x2400)
                return ImageCms.applyTransform(cmyk_img, transform).convert("RGB")
            except Exception:  # noqa: BLE001
                pass
        return cmyk_img.convert("RGB")

    def _pv_convert(self, color: ColorSettings):
        from PIL import Image

        from .colorops import ColorEngine

        orig = self._pv_source_pil()
        engine = ColorEngine(color)
        if not engine.wants_images():
            return orig.copy()
        if not engine.has_active_color():
            return orig.copy()
        w, h = orig.size
        cmyk = imageops.convert_rgb(engine, w, h, orig.tobytes())
        cmyk_img = Image.frombytes("CMYK", (w, h), cmyk)
        return self._cmyk_to_rgb(cmyk_img, color.target_icc, color.intent)

    def _pv_compose(self, orig, conv):
        from PIL import Image, ImageDraw

        gap = 10
        top = 26
        W = orig.width + gap + conv.width
        H = max(orig.height, conv.height) + top
        out = Image.new("RGB", (W, H), (245, 245, 245))
        out.paste(orig, (0, top))
        out.paste(conv, (orig.width + gap, top))
        draw = ImageDraw.Draw(out)
        font = self._pv_font(14)
        draw.text((6, 6), tr('Original'), fill=(0, 0, 0), font=font)
        draw.text((orig.width + gap + 6, 6), tr('Converted'), fill=(0, 0, 0), font=font)
        draw.line([(orig.width + gap // 2, 0), (orig.width + gap // 2, H)], fill=(170, 170, 170))
        return out

    def _pv_build(self, color: ColorSettings):
        orig = self._pv_source_pil()
        conv = self._pv_convert(color)
        return self._pv_compose(orig, conv)

    async def _pv_render_async(self, color: ColorSettings):
        if self._pv_rendering:
            return
        self._pv_rendering = True
        try:
            pil = await asyncio.to_thread(self._pv_build, color)
        except Exception as exc:  # noqa: BLE001
            self._set_message(tr('Preview generation failed: {exc}').format(exc=exc), error=True)
            return
        finally:
            self._pv_rendering = False
        self._pv_set_image(pil)
        if self._pv_refit and self._pv_fit_scale():
            self._pv_refit = False
        self._pv_paint()

    def _pv_set_image(self, pil):
        import io

        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        self._pv_w, self._pv_h = pil.size
        self._pv_toga = toga.Image(buf.getvalue())

    def _pv_fit_scale(self) -> bool:
        cw, ch = self._pv_cw, self._pv_ch
        if cw <= 1 or ch <= 1 or self._pv_w <= 0 or self._pv_h <= 0:
            return False
        scale = min(cw / self._pv_w, ch / self._pv_h, 1.0) * 0.97
        self._pv_scale = max(0.02, scale)
        self._pv_tx = (cw - self._pv_w * self._pv_scale) / 2.0
        self._pv_ty = (ch - self._pv_h * self._pv_scale) / 2.0
        self.pv_zoom_label.text = f"{int(self._pv_scale * 100)}%"
        return True

    def _pv_paint(self):
        c = self.pv_canvas
        c.root_state.drawing_actions.clear()
        cw, ch = max(self._pv_cw, 1.0), max(self._pv_ch, 1.0)
        c.fill_style = "#f0f0f0"
        c.rect(0, 0, cw, ch)
        c.fill()
        if self._pv_toga is not None:
            c.save()
            c.translate(self._pv_tx, self._pv_ty)
            c.scale(self._pv_scale, self._pv_scale)
            c.draw_image(self._pv_toga, 0, 0, self._pv_w, self._pv_h)
            c.restore()
        c.redraw()

    def _pv_zoom(self, factor: float):
        self._pv_refit = False
        cw, ch = self._pv_cw, self._pv_ch
        scale = max(0.05, min(8.0, self._pv_scale * factor))
        if self._pv_scale:
            f = scale / self._pv_scale
            self._pv_tx = cw / 2.0 - (cw / 2.0 - self._pv_tx) * f
            self._pv_ty = ch / 2.0 - (ch / 2.0 - self._pv_ty) * f
        self._pv_scale = scale
        self.pv_zoom_label.text = f"{int(self._pv_scale * 100)}%"
        self._pv_paint()

    def _pv_autorefresh(self):
        if getattr(self, "pv_canvas", None) is None or self._pv_rendering:
            return
        try:
            color = self._collect().color
        except Exception:  # noqa: BLE001
            return
        sig = json.dumps(asdict(color), sort_keys=True)
        if sig == self._pv_sig:
            return
        self._pv_sig = sig
        asyncio.create_task(self._pv_render_async(color))

    # --------------------------------------------------------- preview events
    def _on_pv_resize(self, widget, width, height, **kwargs):
        self._pv_cw = float(width or 0)
        self._pv_ch = float(height or 0)
        if self._pv_toga is not None:
            if self._pv_refit and self._pv_fit_scale():
                self._pv_refit = False
            self._pv_paint()

    def _on_pv_press(self, widget, x, y, **kwargs):
        self._pv_drag = (x, y)

    def _on_pv_drag(self, widget, x, y, **kwargs):
        if self._pv_drag is None:
            return
        self._pv_refit = False
        self._pv_tx += x - self._pv_drag[0]
        self._pv_ty += y - self._pv_drag[1]
        self._pv_drag = (x, y)
        self._pv_paint()

    def _on_pv_release(self, widget, x, y, **kwargs):
        self._pv_drag = None

    def _on_pv_zoom_in(self, widget, **kwargs):
        self._pv_zoom(1.25)

    def _on_pv_zoom_out(self, widget, **kwargs):
        self._pv_zoom(0.8)

    def _on_pv_reset(self, widget, **kwargs):
        self._pv_refit = True
        if self._pv_fit_scale():
            self._pv_refit = False
        self._pv_paint()

    def _on_pv_refresh(self, widget, **kwargs):
        self._pv_sig = None
        self._set_message(tr('Refreshing preview…'))

    def _on_pv_source_change(self, widget, **kwargs):
        self._pv_source = "image" if widget.value == self._pv_image_label else "wheel"
        self._pv_refit = True
        self._pv_sig = None

    async def _on_pv_pick(self, widget, **kwargs):
        result = await self.main_window.dialog(
            OpenFileDialog(title=tr('Choose preview image'),
                           file_types=["*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif",
                                       "*.tiff", "*.gif", "*.webp"]))
        if not result:
            return
        path = Path(result[0] if isinstance(result, list) else result)
        try:
            from PIL import Image
            im = Image.open(str(path))
            im.load()
            im = im.convert("RGB")
            im.thumbnail((900, 900))
        except Exception as exc:  # noqa: BLE001
            self._set_message(tr('Cannot open image: {exc}').format(exc=exc), error=True)
            return
        self._pv_custom_pil = im
        self._pv_source = "image"
        self.pv_source.value = self._pv_image_label
        self._pv_refit = True
        self._pv_sig = None
        self._set_message(tr('Preview image selected: {name}').format(name=path.name))

    def _printer_row(self):
        btn = toga.Button(tr('Refresh'), on_press=self._on_refresh_printers,
                          style=Pack(width=84, margin=(0, 6, 0, 0)))
        return toga.Box(style=Pack(direction=ROW, flex=1),
                        children=[self.printer, btn])

    @staticmethod
    def _printer_items(current: str) -> list[str]:
        names = list_printers()
        if current and current not in names:
            names = [current] + names
        return names

    def _on_refresh_printers(self, widget, **kwargs):
        current = self.printer.value
        items = self._printer_items(str(current) if current else "")
        self.printer.items = items
        if items:
            self.printer.value = current if current in items else items[0]
        self._set_message(tr('Printer list refreshed ({count}).').format(count=len(items)))

    def _path_row(self, edit, directory: bool):
        btn = toga.Button(tr('Browse…'), on_press=self._make_pick(edit, directory),
                          style=Pack(width=84, margin=(0, 6, 0, 0)))
        return toga.Box(style=Pack(direction=ROW, flex=1),
                        children=[edit, btn])

    def _make_pick(self, edit, directory: bool):
        async def handler(widget, **kwargs):
            if directory:
                result = await self.main_window.dialog(
                    SelectFolderDialog(title=tr('Choose folder'), initial_directory=edit.value or None))
                if result:
                    edit.value = str(result)
            else:
                result = await self.main_window.dialog(
                    OpenFileDialog(title=tr('Choose ICC file'), file_types=["*.icc", "*.icm"]))
                if result:
                    edit.value = str(result[0] if isinstance(result, list) else result)
        return handler

    # --------------------------------------------------------------- toggles
    def _on_toggle_autostart(self, widget, **kwargs):
        if self._loading:
            return
        try:
            if widget.value:
                autostart.enable(self._config_path)
            else:
                autostart.disable()
            self._set_message(tr('Start-on-login setting updated.'))
        except Exception as exc:  # noqa: BLE001
            self._loading = True
            widget.value = autostart.is_enabled()
            self._loading = False
            self._set_message(tr('Failed to set autostart: {exc}').format(exc=exc), error=True)

    def _on_lang_change(self, widget, **kwargs):
        if self._loading:
            return
        self._set_message(tr('Language changed. Restart the program to apply.'))

    def _on_toggle_startmin(self, widget, **kwargs):
        if self._loading:
            return
        self._set_message(tr('Updated (takes effect after saving).'))

    def _on_toggle_minimize(self, widget, **kwargs):
        if self._loading:
            return
        self._cfg.minimize_to_tray = widget.value
        self._set_message(tr('Window-close behavior updated.'))

    def _on_toggle_auto_service(self, widget, **kwargs):
        if self._loading:
            return
        self._cfg.auto_start_service = widget.value

    # --------------------------------------------------------------- buttons
    def _on_start(self, widget, **kwargs):
        self._start_service()

    def _on_stop(self, widget, **kwargs):
        self._stop_service()

    def _on_open_log(self, widget, **kwargs):
        self._open_log()

    def _on_open_archive(self, widget, **kwargs):
        path = Path(self.archive_dir.value.strip() or "")
        if not path.exists():
            self._set_message(tr('Archive folder does not exist yet (no jobs processed yet).'), error=True)
            return
        try:
            os.startfile(str(path))  # noqa: S606
        except Exception as exc:  # noqa: BLE001
            self._set_message(tr('Cannot open archive folder: {exc}').format(exc=exc), error=True)

    def _on_clear_log(self, widget, **kwargs):
        self._log_buffer = []
        self.log_view.value = ""
        self._set_message(tr('Log display cleared (file log is kept).'))

    def _on_hide(self, widget, **kwargs):
        if self._tray_ok:
            self.main_window.hide()
            self._set_message(tr('Hidden to tray.'))
        else:
            self._set_message(tr('Tray unavailable; cannot hide.'), error=True)

    # --------------------------------------------------------- virtual printer
    def _vp_driver_items(self, current: str) -> list[str]:
        names = printer_admin.installed_drivers()
        if current and current not in names:
            names = [current] + names
        return names

    def _on_vp_refresh(self, widget, **kwargs):
        current = self.vp_driver.value
        items = self._vp_driver_items(str(current) if current else "")
        self.vp_driver.items = items
        if items:
            self.vp_driver.value = current if current in items else items[0]
        self._set_message(tr('Driver list refreshed ({count}).').format(count=len(items)))

    def _on_vp_check(self, widget, **kwargs):
        name = self.vp_name.value.strip()
        port = self.vp_port.value.strip()
        res = printer_admin.check(name, port)
        if res.get("exists"):
            extra = ""
            if port and res.get("port") and res.get("port") != port:
                extra = tr('(port differs from config: {port})').format(port=res.get('port'))
            self.vp_status.text = tr('✔ Exists: driver={driver} port={port}{extra}').format(
                driver=res.get('driver'), port=res.get('port'), extra=extra)
        else:
            self.vp_status.text = tr('✘ Printer not found “{name}”.').format(name=name)
        self._set_message(res.get("message", ""))

    def _show_admin_result(self, res: dict) -> None:
        ok = bool(res.get("ok"))
        msg = res.get("message", "")
        self._set_message(msg, error=not ok)
        self._log(f"[printer] {'OK' if ok else 'FAIL'} {msg}")

    def _vp_params(self) -> dict:
        return {
            "name": self.vp_name.value.strip(),
            "driver": str(self.vp_driver.value or "").strip(),
            "port": self.vp_port.value.strip(),
        }

    async def _on_vp_create(self, widget, **kwargs):
        p = self._vp_params()
        if not (p["name"] and p["driver"] and p["port"]):
            self._set_message(tr('Queue name / driver / port file must not be empty.'), error=True)
            return
        ok = await self.main_window.dialog(QuestionDialog(
            tr('Create virtual printer'),
            tr('Will create or update the printer:\n\n  Queue: {name}\n  Driver: {driver}\n'
               '  Port: {port}\n\nAdministrator rights are required; a UAC prompt will '
               'appear. Continue?').format(name=p['name'], driver=p['driver'], port=p['port'])))
        if not ok:
            return
        self._set_message(tr('Requesting administrator rights…'))
        res = await asyncio.to_thread(printer_admin.run_action, "create", p)
        self._show_admin_result(res)
        if res.get("ok") and self.vp_sync_spool.value:
            self.spool_dir.value = str(Path(p["port"]).parent)
            self._set_message(res.get("message", "")
                              + tr(' Watch folder synced (remember to save the config).'))
        self._on_vp_check(None)

    async def _on_vp_delete(self, widget, **kwargs):
        p = self._vp_params()
        if not p["name"]:
            self._set_message(tr('Queue name must not be empty.'), error=True)
            return
        ok = await self.main_window.dialog(QuestionDialog(
            tr('Delete virtual printer'),
            tr('Delete printer “{name}” and its file port?\nAdministrator rights are '
               'required.').format(name=p['name'])))
        if not ok:
            return
        self._set_message(tr('Requesting administrator rights…'))
        res = await asyncio.to_thread(printer_admin.run_action, "delete", p)
        self._show_admin_result(res)
        self._on_vp_check(None)

    async def _on_vp_default(self, widget, **kwargs):
        p = self._vp_params()
        if not p["name"]:
            self._set_message(tr('Queue name must not be empty.'), error=True)
            return
        res = await asyncio.to_thread(printer_admin.run_action, "set-default", p)
        self._show_admin_result(res)

    async def _on_open_config(self, widget, **kwargs):
        result = await self.main_window.dialog(
            OpenFileDialog(title=tr('Open config'), file_types=["*.json"]))
        if not result:
            return
        path = Path(result[0] if isinstance(result, list) else result)
        if not path.is_file():
            return
        try:
            self._cfg = AppConfig.load(path)
        except Exception as exc:  # noqa: BLE001
            await self.main_window.dialog(ErrorDialog(
                tr('Read failed'), tr('Cannot read {path}:\n{exc}').format(path=path, exc=exc)))
            return
        self._config_path = path
        self._controller.log_file = self._cfg.log_path()
        self._apply_config_to_ui(self._cfg)
        self._set_message(tr('Loaded: {path}').format(path=path))

    def _on_save(self, widget, **kwargs):
        self._save_config(quiet=False)

    async def _on_save_as(self, widget, **kwargs):
        result = await self.main_window.dialog(
            SaveFileDialog(title=tr('Save as'), suggested_filename=str(self._config_path),
                           file_types=["*.json"]))
        if not result:
            return
        self._config_path = Path(result)
        self._save_config(quiet=False)

    async def _on_validate(self, widget, **kwargs):
        try:
            problems = self._collect().validate()
        except Exception as exc:  # noqa: BLE001
            self._set_message(tr('Failed to read config: {exc}').format(exc=exc), error=True)
            return
        if problems:
            await self.main_window.dialog(ErrorDialog(tr('Validation failed'), "\n".join(f"- {p}" for p in problems)))
            self._set_message(tr('Validation failed.'), error=True)
        else:
            self._set_message(tr('Validation passed.'))

    async def _on_reset(self, widget, **kwargs):
        result = await self.main_window.dialog(
            QuestionDialog(tr('Restore defaults'), tr('Overwrite all current settings with defaults?')))
        if result:
            self._apply_config_to_ui(AppConfig())
            if not self._loading:
                self.sw_autostart.value = autostart.is_enabled()
            self._set_message(tr('Defaults restored (not saved yet).'))

    # -------------------------------------------------------------- config io
    def _load_config(self) -> AppConfig:
        if self._config_path.is_file():
            try:
                cfg = AppConfig.load(self._config_path)
            except Exception as exc:  # noqa: BLE001
                print(tr('[config] read failed, using defaults: {exc}').format(exc=exc),
                      file=sys.stderr)
                cfg = AppConfig()
        else:
            cfg = AppConfig()
        set_language(cfg.lang)
        for problem in cfg.validate():
            print(f"[config] {problem}", file=sys.stderr)
        return cfg

    def _apply_config_to_ui(self, cfg: AppConfig) -> None:
        self._loading = True
        try:
            c = cfg.color
            self.enabled.value = bool(c.enabled)
            items = self._printer_items(cfg.printer)
            self.printer.items = items
            if items:
                self.printer.value = cfg.printer if cfg.printer in items else items[0]
            self.spool_dir.value = cfg.spool_dir
            self.archive_dir.value = cfg.archive_dir
            self.spool_pattern.value = cfg.spool_pattern
            self.poll_seconds.value = float(cfg.poll_seconds)

            if cfg.lang in self._lang_items:
                self.lang.value = self._lang_items[cfg.lang]

            self.vp_name.value = cfg.virtual_printer
            self.vp_port.value = cfg.virtual_port
            drv_items = self._vp_driver_items(cfg.printer)
            self.vp_driver.items = drv_items
            if drv_items:
                self.vp_driver.value = cfg.printer if cfg.printer in drv_items else drv_items[0]

            self.sw_auto_service.value = bool(cfg.auto_start_service)
            self.sw_startmin.value = bool(cfg.start_minimized)
            self.sw_minimize.value = bool(cfg.minimize_to_tray)
            self.sw_dryrun.value = bool(cfg.dry_run)
            self.sw_autostart.value = autostart.is_enabled() or bool(cfg.autostart)

            self.k_reduction.value = float(c.k_reduction)
            self.k_curve_enabled.value = bool(c.k_curve_enabled)
            self.k_curve_min.value = float(c.k_curve_min)
            self.k_curve_max.value = float(c.k_curve_max)
            self.k_curve_power.value = float(c.k_curve_power)
            self.preserve_black.value = bool(c.preserve_black)
            self.black_threshold.value = float(c.black_threshold)

            self.target_icc.value = c.target_icc
            self.input_rgb_icc.value = c.input_rgb_icc
            self.intent.value = self._intent_name_to_label.get(c.intent, list(self._intent_labels)[1])
            self.cmm.value = self._cmm_code_to_label.get(c.cmm, list(self._cmm_labels)[0])
            self.rgb_to_cmyk.value = bool(c.rgb_to_cmyk)
            self.gray_to_k.value = bool(c.gray_to_k)
            self.neutral_to_k.value = bool(c.neutral_to_k)
            self.neutral_vector_only.value = bool(c.neutral_vector_only)
            self.neutral_threshold.value = float(c.neutral_threshold)
            self.neutral_min.value = float(c.neutral_min)
            self.text_solid_black.value = bool(c.text_solid_black)
            self.text_k_gain.value = float(c.text_k_gain)
            self._sync_param_groups()
            self.process_images.value = bool(c.process_images)

            scale = list(c.cmy_scale) + [1.0, 1.0, 1.0]
            self.cmy_c.value = float(scale[0])
            self.cmy_m.value = float(scale[1])
            self.cmy_y.value = float(scale[2])
            self.c_gamma.value = float(c.c_gamma)
            self.m_gamma.value = float(c.m_gamma)
            self.y_gamma.value = float(c.y_gamma)
            self.k_gamma.value = float(c.k_gamma)
            self.gcr.value = float(c.gcr)
            self.ucr.value = float(c.ucr)
            self.rich_black.value = float(c.rich_black)
            self.rich_black_threshold.value = float(c.rich_black_threshold)
            self.tac_limit.value = float(c.tac_limit)
            self.tac_mode.value = self._tac_code_to_label.get(c.tac_mode, list(self._tac_labels)[0])

            sep_active = (
                scale[0] != 1.0 or scale[1] != 1.0 or scale[2] != 1.0
                or c.c_gamma != 1.0 or c.m_gamma != 1.0
                or c.y_gamma != 1.0 or c.k_gamma != 1.0
                or bool(c.gcr) or bool(c.ucr) or bool(c.rich_black) or bool(c.tac_limit)
            )
            self.sep_toggle.value = bool(sep_active)
            self._collapse(self._sep_section, self._sep_body, self._sep_anchor, sep_active)

            k_active = (
                c.k_reduction != 0.0 or bool(c.k_curve_enabled)
                or not c.preserve_black or c.black_threshold != 95.0
            )
            self.k_toggle.value = bool(k_active)
            self._collapse(self._k_section, self._k_body, self._k_anchor, k_active)
        finally:
            self._loading = False

    def _collect(self) -> AppConfig:
        cfg = AppConfig()
        cfg.printer = str(self.printer.value or "").strip()
        cfg.spool_dir = self.spool_dir.value.strip()
        cfg.archive_dir = self.archive_dir.value.strip()
        cfg.spool_pattern = self.spool_pattern.value.strip() or "*.prn"
        cfg.poll_seconds = float(self.poll_seconds.value or 1.0)
        cfg.dry_run = bool(self.sw_dryrun.value)
        cfg.virtual_printer = self.vp_name.value.strip() or "Brother ICC Virtual"
        cfg.virtual_port = self.vp_port.value.strip()

        cfg.auto_start_service = bool(self.sw_auto_service.value)
        cfg.autostart = bool(self.sw_autostart.value)
        cfg.start_minimized = bool(self.sw_startmin.value)
        cfg.minimize_to_tray = bool(self.sw_minimize.value)
        cfg.log_file = self._cfg.log_file
        cfg.lang = self._lang_name_to_code.get(self.lang.value, "en")

        cfg.color = ColorSettings(
            enabled=bool(self.enabled.value),
            k_reduction=float(self.k_reduction.value or 0.0),
            k_curve_enabled=bool(self.k_curve_enabled.value),
            k_curve_min=float(self.k_curve_min.value or 0.0),
            k_curve_max=float(self.k_curve_max.value or 0.0),
            k_curve_power=float(self.k_curve_power.value or 1.0),
            preserve_black=bool(self.preserve_black.value),
            black_threshold=float(self.black_threshold.value or 0.0),
            cmy_scale=[float(self.cmy_c.value or 0.0), float(self.cmy_m.value or 0.0),
                       float(self.cmy_y.value or 0.0)],
            c_gamma=float(self.c_gamma.value or 1.0),
            m_gamma=float(self.m_gamma.value or 1.0),
            y_gamma=float(self.y_gamma.value or 1.0),
            k_gamma=float(self.k_gamma.value or 1.0),
            gcr=float(self.gcr.value or 0.0),
            ucr=float(self.ucr.value or 0.0),
            rich_black=float(self.rich_black.value or 0.0),
            rich_black_threshold=float(self.rich_black_threshold.value or 0.0),
            tac_limit=float(self.tac_limit.value or 0.0),
            tac_mode=self._tac_labels.get(self.tac_mode.value, "scale_cmy"),
            rgb_to_cmyk=bool(self.rgb_to_cmyk.value),
            gray_to_k=bool(self.gray_to_k.value),
            neutral_to_k=bool(self.neutral_to_k.value),
            neutral_threshold=float(self.neutral_threshold.value or 0.0),
            neutral_min=float(self.neutral_min.value or 0.0),
            neutral_vector_only=bool(self.neutral_vector_only.value),
            text_solid_black=bool(self.text_solid_black.value),
            text_k_gain=float(self.text_k_gain.value or 0.0),
            process_images=bool(self.process_images.value),
            target_icc=self.target_icc.value.strip(),
            input_rgb_icc=self.input_rgb_icc.value.strip(),
            intent=self._intent_labels.get(self.intent.value, INTENT_NAMES[1]),
            cmm=self._cmm_labels.get(self.cmm.value, "lcms"),
        )
        return cfg

    def _save_config(self, quiet: bool = False) -> None:
        try:
            cfg = self._collect()
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                self._set_message(tr('Failed to read config: {exc}').format(exc=exc), error=True)
            return
        problems = cfg.validate()
        if problems and not quiet:
            self._set_message(tr('Config has problems: ') + tr('; ').join(problems) + tr(' (still saved)'), error=True)
        try:
            cfg.save(self._config_path)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                self._set_message(tr('Save failed: {exc}').format(exc=exc), error=True)
            return
        previous = self._cfg
        changed = previous is None or asdict(cfg) != asdict(previous)
        self._cfg = cfg
        self._controller.log_file = cfg.log_path()
        if problems:
            return
        if not quiet:
            self._set_message(tr('Saved: {path}').format(path=self._config_path))
        if changed and self._controller.is_running():
            # Apply immediately: the watch loop copies the engine at start, so a
            # change would otherwise silently keep using the old settings (a
            # frequent source of "the setting does nothing" reports).
            self._controller.restart(cfg)
            self._last_running = None
            if not quiet:
                self._set_message(tr('Saved and applied: {path}').format(path=self._config_path))


def main(config_path=None, start_minimized: bool = False) -> int:
    from .single_instance import SingleInstance

    instance = SingleInstance()
    if not instance.acquire():
        instance.signal_primary()
        print(tr('[gui] Another instance is running; asked it to show its window.'), file=sys.stderr)
        return 0
    try:
        app = IccSpoolerApp(config_path=config_path, start_minimized=start_minimized)
        app._single_instance = instance
        app.main_loop()
    finally:
        instance.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

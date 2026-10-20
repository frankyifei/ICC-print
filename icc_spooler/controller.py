"""Background service controller.

Runs the spool watch loop in a daemon thread so the GUI stays responsive, and
exposes running state, counters and a log stream to the front end.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime
from pathlib import Path

from .colorops import ColorEngine
from .config import AppConfig, ColorSettings
from .i18n import tr
from .service import watch_loop


def _describe_color(c: ColorSettings) -> str:
    """One-line summary of the active color pipeline, for the log."""
    if not c.enabled:
        return tr('disabled')
    parts = []
    if c.rgb_to_cmyk:
        parts.append('RGB>CMYK')
    if c.gray_to_k:
        parts.append('gray>K')
    if c.neutral_to_k:
        parts.append(f'neutral>K({c.neutral_threshold:g})')
    if c.text_solid_black:
        parts.append(f'text>K(gain {c.text_k_gain:g}, dpi {c.text_dpi:g})')
    if c.k_reduction:
        parts.append(f'K-{c.k_reduction:g}')
    if c.gcr:
        parts.append(f'GCR {c.gcr:g}')
    if c.ucr:
        parts.append(f'UCR {c.ucr:g}')
    if c.rich_black:
        parts.append(f'rich {c.rich_black:g}')
    if c.tac_limit:
        parts.append(f'TAC {c.tac_limit:g}')
    return ', '.join(parts) if parts else tr('nothing active')


class ServiceController:
    def __init__(self, log_queue: "queue.Queue[str]", log_file: Path | None = None):
        self.log_queue = log_queue
        self.log_file = Path(log_file) if log_file else None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = {"jobs": 0, "errors": 0, "last": ""}

    # ------------------------------------------------------------- status
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    # -------------------------------------------------------------- logging
    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {msg}"
        try:
            self.log_queue.put_nowait(line)
        except queue.Full:
            pass
        if self.log_file is None:
            return
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def _on_log(self, msg: str) -> None:
        with self._lock:
            if msg.startswith("[sent]") or msg.startswith("[dry-run]"):
                self._stats["jobs"] += 1
                self._stats["last"] = msg
            elif msg.startswith("[error]"):
                self._stats["errors"] += 1
        self.log(msg)

    # --------------------------------------------------------------- control
    def _launch(self, cfg: AppConfig, previous=None) -> None:
        engine = ColorEngine(cfg.color)
        stop = threading.Event()
        self._stop = stop
        with self._lock:
            self._stats = {"jobs": 0, "errors": 0, "last": ""}
        self.log(tr('[service] started  spool={spool}  printer={printer}').format(
            spool=cfg.spool_dir, printer=cfg.printer))
        self.log(tr('[service] color: {summary}').format(summary=_describe_color(cfg.color)))

        def run():
            if previous is not None:
                prev_thread, prev_stop = previous
                if prev_thread.is_alive():
                    prev_stop.set()
                    prev_thread.join()
            try:
                watch_loop(cfg.spool_dir, cfg.archive_dir, cfg.printer, engine,
                           pattern=cfg.spool_pattern, poll_seconds=cfg.poll_seconds,
                           dry_run=cfg.dry_run, logger=self._on_log, stop_event=stop)
            except Exception as exc:  # noqa: BLE001
                self.log(tr('[service] exited unexpectedly: {exc}').format(exc=exc))
            finally:
                self.log(tr('[service] stopped'))

        self._thread = threading.Thread(target=run, name="icc-spooler-watch", daemon=True)
        self._thread.start()

    def start(self, cfg: AppConfig) -> bool:
        if self.is_running():
            return False
        self._launch(cfg)
        return True

    def restart(self, cfg: AppConfig) -> None:
        """Swap in new settings without dropping a job that is being sent."""
        previous = (self._thread, self._stop) if self._thread is not None else None
        self._launch(cfg, previous=previous)

    def stop(self, timeout: float = 5.0) -> None:
        if not self.is_running():
            return
        self.log(tr('[service] stopping…'))
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

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
from .config import AppConfig
from .i18n import tr
from .service import watch_loop


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
    def start(self, cfg: AppConfig) -> bool:
        if self.is_running():
            return False
        self._stop.clear()
        with self._lock:
            self._stats = {"jobs": 0, "errors": 0, "last": ""}
        engine = ColorEngine(cfg.color)
        self.log(tr('[service] started  spool={spool}  printer={printer}').format(
            spool=cfg.spool_dir, printer=cfg.printer))

        def run():
            try:
                watch_loop(cfg.spool_dir, cfg.archive_dir, cfg.printer, engine,
                           pattern=cfg.spool_pattern, poll_seconds=cfg.poll_seconds,
                           dry_run=cfg.dry_run, logger=self._on_log, stop_event=self._stop)
            except Exception as exc:  # noqa: BLE001
                self.log(tr('[service] exited unexpectedly: {exc}').format(exc=exc))
            finally:
                self.log(tr('[service] stopped'))

        self._thread = threading.Thread(target=run, name="icc-spooler-watch", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        if not self.is_running():
            return
        self.log(tr('[service] stopping…'))
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

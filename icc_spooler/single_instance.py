"""Single-instance guard and "wake the running instance" signaling.

Only one ICC Spooler process may own the tray icon and the spool service.
A named mutex enforces this; a second launch writes a tiny signal file and
exits, while the primary instance notices it (from its UI loop) and shows its
window / restores its tray icon.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import tempfile
import time
from pathlib import Path

MUTEX_NAME = "Local\\IccSpooler_SingleInstance"
ERROR_ALREADY_EXISTS = 183


def _signal_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    folder = Path(base) / "IccSpooler"
    return folder / "activate.signal"


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME):
        self._name = name
        self._handle = None
        self._signal = _signal_path()
        self.is_primary = False

    def acquire(self) -> bool:
        """Create/own the mutex. Returns True for the first (primary) instance."""
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception:
            self.is_primary = True  # not Windows: do not block startup
            return True
        create = kernel32.CreateMutexW
        create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        self._handle = create(None, False, self._name)
        self.is_primary = ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        if self.is_primary:
            try:
                self._signal.unlink()
            except (FileNotFoundError, OSError):
                pass
        return self.is_primary

    def signal_primary(self) -> None:
        """Ask the running (primary) instance to show itself."""
        try:
            self._signal.parent.mkdir(parents=True, exist_ok=True)
            self._signal.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass

    def consume_signal(self) -> bool:
        """True if a second launch has requested activation since last check."""
        try:
            if self._signal.exists():
                self._signal.unlink()
                return True
        except OSError:
            return True
        return False

    def release(self) -> None:
        if self._handle:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

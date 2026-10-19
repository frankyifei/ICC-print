"""Per-user autostart via the HKCU Run registry key (Windows only)."""
from __future__ import annotations

import sys
from pathlib import Path

from .i18n import tr

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE = "IccSpooler"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_supported() -> bool:
    return sys.platform.startswith("win")


def _pythonw() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.is_file() else exe)


def launcher() -> Path:
    return PROJECT_ROOT / "IccSpooler.pyw"


def command(config_path: str | Path | None = None, minimized: bool = True) -> str:
    parts = [f'"{_pythonw()}"', f'"{launcher()}"', "gui"]
    if config_path:
        parts += ["--config", f'"{Path(config_path)}"']
    if minimized:
        parts.append("--minimized")
    return " ".join(parts)


def _open_run_key(write: bool = False):
    import winreg
    access = winreg.KEY_SET_VALUE if write else winreg.KEY_READ
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def is_enabled() -> bool:
    if not is_supported():
        return False
    import winreg
    try:
        with _open_run_key() as key:  # type: ignore[union-attr]
            value, _ = winreg.QueryValueEx(key, APP_VALUE)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable(config_path: str | Path | None = None) -> None:
    if not is_supported():
        raise RuntimeError(tr('Autostart is only supported on Windows.'))
    import winreg
    with _open_run_key(write=True) as key:  # type: ignore[union-attr]
        winreg.SetValueEx(key, APP_VALUE, 0, winreg.REG_SZ, command(config_path))


def disable() -> None:
    if not is_supported():
        return
    import winreg
    try:
        with _open_run_key(write=True) as key:  # type: ignore[union-attr]
            winreg.DeleteValue(key, APP_VALUE)
    except FileNotFoundError:
        pass
    except OSError:
        pass

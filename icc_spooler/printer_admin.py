"""Manage the virtual print queue via native Win32 (no PowerShell).

The virtual queue's port is a local *file* port: the port name is a file path
and the classic print-to-file spooler monitor writes each job to that file.
This is exactly what `Add-PrinterPort -Name <file>` does under the hood.

Printer/port changes need Administrator rights, so the GUI calls
`run_action`, which relaunches this module's `printer-admin` CLI elevated
through the UAC (`runas`) verb and reads a JSON result back.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import winreg

try:
    import win32print
    _HAVE_WIN32PRINT = True
except Exception:  # pragma: no cover - non-Windows / missing pywin32
    _HAVE_WIN32PRINT = False

from .i18n import current_language, set_language, tr

PORTS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Ports"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
ERROR_CANCELLED = 1223
SW_HIDE = 0
SERVER_ACCESS_ADMINISTER = 0x00000001


class _PrinterDefaults(ctypes.Structure):
    _fields_ = [
        ("pDatatype", wintypes.LPWSTR),
        ("pDevMode", ctypes.c_void_p),
        ("DesiredAccess", wintypes.DWORD),
    ]


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


# --------------------------------------------------------------------- helpers
def is_admin() -> bool:
    """True if the current process is elevated (Administrator)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def list_printers() -> list[str]:
    """Local + connected printer queue names."""
    try:
        from .service import list_printers as _lp
        return _lp()
    except Exception:
        return []


def installed_drivers() -> list[str]:
    """Installed printer drivers, PostScript ones first."""
    if not _HAVE_WIN32PRINT:
        return []
    names: list[str] = []
    try:
        for item in win32print.EnumPrinterDrivers(None, None, 2):
            if isinstance(item, dict):
                name = item.get("Name") or item.get("pName")
            else:
                name = item[0]
            if name:
                names.append(str(name))
    except Exception:
        pass
    names.sort(key=lambda n: (0 if "postscript" in n.lower() else 1, n.lower()))
    return names


def get_printer(name: str):
    """Return PRINTER_INFO_2 for `name`, or None if it does not exist."""
    if not _HAVE_WIN32PRINT:
        return None
    handle = None
    try:
        handle = win32print.OpenPrinter(name)
    except Exception:
        return None
    try:
        return win32print.GetPrinter(handle, 2)
    except Exception:
        return None
    finally:
        try:
            win32print.ClosePrinter(handle)
        except Exception:
            pass


def _open_printer(name: str, access: int | None = None):
    if access is None:
        return win32print.OpenPrinter(name)
    return win32print.OpenPrinter(name, {"DesiredAccess": access})


def _existing_ports() -> set:
    out: set = set()
    if not _HAVE_WIN32PRINT:
        return out
    try:
        for item in win32print.EnumPorts(None, 1):
            name = item.get("Name") if isinstance(item, dict) else item[0]
            if name:
                out.add(str(name))
    except Exception:
        pass
    return out


def _resolve_driver(driver: str | None) -> str | None:
    if not driver:
        return None
    for name in installed_drivers():
        if name.lower() == driver.lower():
            return name
    return None


def _template_devmode(driver: str | None):
    """Borrow a valid DEVMODE from an existing queue (AddPrinter needs one)."""
    if not _HAVE_WIN32PRINT:
        return None
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        for item in win32print.EnumPrinters(flags, None, 2):
            name = item.get("pPrinterName") if isinstance(item, dict) else item[2]
            if not name:
                continue
            info = get_printer(name)
            if not info:
                continue
            if driver is None or info.get("pDriverName") == driver:
                devmode = info.get("pDevMode")
                if devmode is not None:
                    return devmode
        info = get_printer(win32print.GetDefaultPrinter())
        if info:
            return info.get("pDevMode")
    except Exception:
        pass
    return None


def _xcv_local_port(command: str, port_name: str) -> int:
    """Drive the Local Port monitor via XcvData ("AddPort"/"DeletePort").

    Returns the monitor status code (0 = success), or -1 if the handle could
    not be opened. A file port's name is the target file path.
    """
    try:
        winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
    except Exception:
        return -1
    open_printer = winspool.OpenPrinterW
    open_printer.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE),
                             ctypes.POINTER(_PrinterDefaults)]
    open_printer.restype = wintypes.BOOL
    xcv_data = winspool.XcvDataW
    xcv_data.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    xcv_data.restype = wintypes.BOOL
    close_printer = winspool.ClosePrinter
    close_printer.argtypes = [wintypes.HANDLE]

    defaults = _PrinterDefaults(None, None, SERVER_ACCESS_ADMINISTER)
    handle = wintypes.HANDLE()
    if not open_printer(",XcvMonitor Local Port", ctypes.byref(handle),
                        ctypes.byref(defaults)):
        return -1
    try:
        buf = ctypes.create_unicode_buffer(port_name)
        size = (len(port_name) + 1) * 2
        needed = wintypes.DWORD(0)
        status = wintypes.DWORD(0)
        xcv_data(handle, command, ctypes.cast(buf, ctypes.c_void_p), size,
                 None, 0, ctypes.byref(needed), ctypes.byref(status))
        return int(status.value)
    finally:
        close_printer(handle)


def _write_registry_port(port_name: str) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, PORTS_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, port_name, 0, winreg.REG_SZ, "")


def ensure_local_port(port_name: str) -> bool:
    """Create the file port if missing. Returns True if it was created."""
    if not port_name:
        raise ValueError(tr('Port name must not be empty.'))
    if port_name in _existing_ports():
        return False
    status = _xcv_local_port("AddPort", port_name)
    if status not in (0, 183):  # 0 = ok, 183 = already exists
        try:
            _write_registry_port(port_name)
        except Exception:
            pass
        if status == -1:
            raise RuntimeError(tr('Cannot open the “Local Port” port monitor (administrator rights required).'))
        raise RuntimeError(tr('Failed to create file port “{name}” (status {status}).').format(
            name=port_name, status=status))
    return True


def remove_local_port(port_name: str) -> bool:
    """Delete the file port. Returns True if it no longer exists."""
    _xcv_local_port("DeletePort", port_name)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PORTS_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, port_name)
    except FileNotFoundError:
        return True
    except Exception:
        return False
    return True


# ------------------------------------------------------------------- actions
def create_virtual_printer(name: str, driver: str, port_file: str) -> dict:
    if not _HAVE_WIN32PRINT:
        raise RuntimeError(tr('pywin32 (win32print) is required to manage printers.'))
    if not is_admin():
        raise PermissionError(tr('Administrator rights required.'))
    if not name:
        raise ValueError(tr('Queue name must not be empty.'))
    resolved = _resolve_driver(driver)
    if resolved is None:
        raise ValueError(tr('Installed driver not found: {driver}').format(
            driver=driver or tr('(empty)')))
    ensure_local_port(port_file)

    existing = get_printer(name)
    if existing is not None:
        info = dict(existing)
        info["pDriverName"] = resolved
        info["pPortName"] = port_file
        info["pPrintProcessor"] = info.get("pPrintProcessor") or "winprint"
        info["pDatatype"] = info.get("pDatatype") or "RAW"
        handle = _open_printer(name, win32print.PRINTER_ALL_ACCESS)
        try:
            win32print.SetPrinter(handle, 2, info, 0)
        finally:
            win32print.ClosePrinter(handle)
        return {"ok": True, "action": "updated",
                "message": tr('Updated printer “{name}” driver/port.').format(name=name)}

    info = {
        "pServerName": None,
        "pPrinterName": name,
        "pShareName": None,
        "pPortName": port_file,
        "pDriverName": resolved,
        "pComment": tr('ICC Spooler virtual printer (outputs to file; the service does color processing)'),
        "pLocation": None,
        "pDevMode": _template_devmode(resolved),
        "pSepFile": None,
        "pPrintProcessor": "winprint",
        "pDatatype": "RAW",
        "pParameters": None,
        "pSecurityDescriptor": None,
        "Attributes": 0,
        "Priority": 1,
        "DefaultPriority": 0,
        "StartTime": 0,
        "UntilTime": 0,
        "Status": 0,
        "cJobs": 0,
        "AveragePPM": 0,
    }
    win32print.AddPrinter(None, 2, info)
    return {"ok": True, "action": "created",
            "message": tr('Created virtual printer “{name}” → {port}').format(
                name=name, port=port_file)}


def delete_virtual_printer(name: str, port_file: str | None = None) -> dict:
    if not _HAVE_WIN32PRINT:
        raise RuntimeError(tr('pywin32 (win32print) is required to manage printers.'))
    if not is_admin():
        raise PermissionError(tr('Administrator rights required.'))
    if get_printer(name) is None:
        return {"ok": True, "action": "absent",
                "message": tr('Printer “{name}” does not exist; nothing to delete.').format(name=name)}
    handle = _open_printer(name, win32print.PRINTER_ALL_ACCESS)
    try:
        win32print.DeletePrinter(handle)
    finally:
        try:
            win32print.ClosePrinter(handle)
        except Exception:
            pass
    note = ""
    if port_file:
        try:
            if remove_local_port(port_file):
                note = tr(', and deleted file port {port}').format(port=port_file)
        except Exception:
            note = tr('(port deletion failed, can be ignored)')
    return {"ok": True, "action": "deleted",
            "message": tr('Deleted printer “{name}”{note}.').format(name=name, note=note)}


def set_default_printer(name: str) -> dict:
    if not _HAVE_WIN32PRINT:
        raise RuntimeError(tr('pywin32 (win32print) is required to manage printers.'))
    if get_printer(name) is None:
        raise ValueError(tr('Printer “{name}” does not exist.').format(name=name))
    win32print.SetDefaultPrinter(name)
    return {"ok": True, "action": "default",
            "message": tr('Set “{name}” as the default printer.').format(name=name)}


def check(name: str, port_file: str | None = None) -> dict:
    """Inspect the virtual queue (no admin needed)."""
    info = get_printer(name)
    result = {
        "ok": True,
        "action": "check",
        "exists": info is not None,
        "name": name,
        "driver": (info or {}).get("pDriverName"),
        "port": (info or {}).get("pPortName"),
        "port_registered": (port_file in _existing_ports()) if port_file else None,
    }
    if info is None:
        result["message"] = tr('Printer not found “{name}”.').format(name=name)
    else:
        result["message"] = tr('Printer “{name}” exists: driver={driver}, port={port}').format(
            name=name, driver=result['driver'], port=result['port'])
    return result


def perform(action: str, name: str, driver: str | None = None,
            port: str | None = None) -> dict:
    """Run one action, returning a JSON-friendly result (never raises)."""
    try:
        if action == "create":
            return create_virtual_printer(name, driver or "", port or "")
        if action == "delete":
            return delete_virtual_printer(name, port)
        if action == "set-default":
            return set_default_printer(name)
        if action == "check":
            return check(name, port)
        if action == "remove-port":
            if not name:
                raise ValueError(tr('Port name must not be empty.'))
            if not is_admin():
                raise PermissionError(tr('Administrator rights required.'))
            removed = remove_local_port(name)
            note = tr('Deleted') if removed else tr('Deleted (registry entry did not exist)')
            return {"ok": True, "action": "remove-port",
                    "message": tr('{note}port “{name}”.').format(note=note, name=name)}
        return {"ok": False, "action": action,
                "message": tr('Unknown action: {action}').format(action=action)}
    except PermissionError as exc:
        return {"ok": False, "action": action, "elevation_required": True, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": action, "message": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ elevation
def _quote_args(argv: list[str]) -> str:
    out = []
    for a in argv:
        a = str(a)
        if a == "" or any(c in a for c in ' \t"'):
            out.append('"' + a.replace('"', r'\"') + '"')
        else:
            out.append(a)
    return " ".join(out)


def _shell_runas(executable: str, params: str, timeout: float) -> int:
    """Run `executable params` elevated (UAC). Returns the exit code."""
    sei = _ShellExecuteInfoW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    sei.lpVerb = "runas"
    sei.lpFile = executable
    sei.lpParameters = params
    sei.lpDirectory = str(PROJECT_ROOT)
    sei.nShow = SW_HIDE
    shell32 = ctypes.windll.shell32
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        # 1223 = user declined the UAC prompt
        return err or 1
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000))
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)


def run_action(action: str, params: dict, timeout: float = 300.0) -> dict:
    """Perform a printer action, elevating via UAC when needed.

    Runs in-process when already elevated, otherwise relaunches this program's
    `printer-admin` subcommand elevated and reads back a JSON result file.
    """
    if not _HAVE_WIN32PRINT:
        return {"ok": False, "message": tr('pywin32 (win32print) is required to manage printers.')}
    if action == "check":
        return perform("check", params.get("name", ""), port=params.get("port"))

    if is_admin():
        return perform(action, params.get("name", ""), params.get("driver"),
                       params.get("port"))

    result_path = Path(tempfile.gettempdir()) / (
        f"icc_printer_{os.getpid()}_{int(time.time() * 1000)}.json")
    argv = ["-m", "icc_spooler", "printer-admin", "--action", action,
            "--result", str(result_path), "--name", params.get("name", ""),
            "--lang", current_language()]
    if params.get("driver"):
        argv += ["--driver", str(params["driver"])]
    if params.get("port"):
        argv += ["--port", str(params["port"])]

    try:
        code = _shell_runas(sys.executable, _quote_args(argv), timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": tr('Elevation failed: {exc}').format(exc=exc)}

    data: dict = {}
    if result_path.is_file():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        try:
            result_path.unlink()
        except OSError:
            pass
    if data:
        return data
    if code == ERROR_CANCELLED:
        return {"ok": False, "elevation_required": True,
                "message": tr('Administrator authorization (UAC) cancelled; operation not performed.')}
    return {"ok": False, "message": tr('Operation failed (exit code {code}).').format(code=code)}


# ----------------------------------------------------------------------- CLI
def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="printer-admin",
                                     description=tr('Manage virtual printer (internal / for elevation)'))
    parser.add_argument("--action", required=True,
                        choices=["create", "delete", "set-default", "check", "remove-port"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--driver", default=None)
    parser.add_argument("--port", default=None)
    parser.add_argument("--result", default=None, help=tr('Write the JSON result to this file'))
    parser.add_argument("--lang", default=None, help="UI language code")
    args = parser.parse_args(argv)
    set_language(args.lang)

    result = perform(args.action, args.name, args.driver, args.port)
    if args.result:
        try:
            Path(args.result).write_text(json.dumps(result, ensure_ascii=False),
                                         encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(cli())

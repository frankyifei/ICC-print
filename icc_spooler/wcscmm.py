"""Windows ICM/WCS RGB->CMYK backend (mscms.dll) via ctypes.

This is the same imaging CMM the Windows/GDI print path uses, offered as an
alternative to littleCMS (Pillow/ImageCms). The classic Microsoft CMM works at
8 bits per channel, so this does not add precision over littleCMS; it produces
the Windows-native color numbers.

No third-party dependency: only ``ctypes`` and the system ``mscms.dll``.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from .i18n import tr

_IS_WINDOWS = sys.platform == "win32"

WORD = wintypes.WORD
DWORD = wintypes.DWORD
HANDLE = wintypes.HANDLE
PVOID = ctypes.c_void_p
BOOL = wintypes.BOOL

# icm.h enum values
COLOR_RGB = 2
COLOR_CMYK = 7
# BMFORMAT values from icm.h. On the stock Windows CMM the byte order that
# matches a packed interleaved RGB byte buffer is BM_BGRTRIPLETS, and the CMYK
# output order is BM_KYMCQUADS (verified against TranslateColors per-pixel
# results); the plain BM_RGBTRIPLETS/BM_CMYKQUADS pair comes out swapped.
BM_BGRTRIPLETS = 0x0004
BM_KYMCQUADS = 0x0305

PROFILE_FILENAME = 1
PROFILE_READ = 1
FILE_SHARE_READ = 1
OPEN_EXISTING = 3
INDEX_DONT_CARE = 0


class _PROFILE(ctypes.Structure):
    _fields_ = [("dwType", DWORD), ("pProfileData", PVOID), ("cbDataSize", DWORD)]


class _RGBCOLOR(ctypes.Structure):
    _fields_ = [("red", WORD), ("green", WORD), ("blue", WORD)]


class _CMYKCOLOR(ctypes.Structure):
    _fields_ = [("cyan", WORD), ("magenta", WORD), ("yellow", WORD), ("black", WORD)]


class _COLOR(ctypes.Union):
    # Mirrors the icm.h COLOR union: 4 WORDs for CMYK plus an alignment pad to
    # the union's natural size (16 bytes on x64).
    _fields_ = [("rgb", _RGBCOLOR), ("cmyk", _CMYKCOLOR), ("_pad", PVOID * 2)]


_LIB = None


def _lib():
    global _LIB
    if _LIB is None:
        lib = ctypes.WinDLL("mscms")
        lib.OpenColorProfileW.argtypes = [ctypes.POINTER(_PROFILE), DWORD, DWORD, DWORD]
        lib.OpenColorProfileW.restype = HANDLE
        lib.CloseColorProfile.argtypes = [HANDLE]
        lib.CloseColorProfile.restype = BOOL
        lib.CreateMultiProfileTransform.argtypes = [
            ctypes.POINTER(HANDLE), DWORD, ctypes.POINTER(DWORD), DWORD, DWORD, DWORD]
        lib.CreateMultiProfileTransform.restype = HANDLE
        lib.DeleteColorTransform.argtypes = [HANDLE]
        lib.DeleteColorTransform.restype = BOOL
        lib.TranslateColors.argtypes = [HANDLE, PVOID, DWORD, ctypes.c_int, PVOID, ctypes.c_int]
        lib.TranslateColors.restype = BOOL
        lib.TranslateBitmapBits.argtypes = [
            HANDLE, PVOID, ctypes.c_int, DWORD, DWORD, DWORD,
            PVOID, ctypes.c_int, DWORD, PVOID, ctypes.c_void_p]
        lib.TranslateBitmapBits.restype = BOOL
        _LIB = lib
    return _LIB


def available() -> bool:
    """True when the Windows color-management CMM can be used."""
    if not _IS_WINDOWS:
        return False
    try:
        _lib()
        return True
    except OSError:
        return False


def srgb_profile_path(src_icc: str = "") -> str:
    """Resolve an sRGB source profile: the user's override or the system one."""
    if src_icc and Path(src_icc).is_file():
        return str(Path(src_icc).resolve())
    root = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "spool" / "drivers" / "color"
    for name in ("sRGB Color Space Profile.icm", "sRGB Color Space Profile.ICC", "sRGB.icm"):
        candidate = root / name
        if candidate.is_file():
            return str(candidate)
    return ""


class WcsRgbToCmyk:
    """RGB(0..1) -> CMYK(0..1) through the Windows CMM."""

    def __init__(self, dst_icc: str, intent: int = 1, src_icc: str = ""):
        if not _IS_WINDOWS:
            raise RuntimeError(tr('Windows WCS is only available on Windows.'))
        if not dst_icc or not Path(dst_icc).is_file():
            raise RuntimeError(tr('RGB→CMYK requires a valid target ICC file.'))
        src = srgb_profile_path(src_icc)
        if not src:
            raise RuntimeError(tr('Windows sRGB profile not found (you can specify an input RGB ICC).'))
        self._lib = _lib()
        self._filenames: list = []
        self._handles: list = []
        self._xform = None
        try:
            h_rgb = self._open(src)
            h_cmyk = self._open(str(Path(dst_icc).resolve()))
            arr = (HANDLE * 2)(h_rgb, h_cmyk)
            intents = (DWORD * 1)(int(intent))
            self._xform = self._lib.CreateMultiProfileTransform(
                arr, 2, intents, 1, 0, INDEX_DONT_CARE)
            if not self._xform:
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.close()
            raise
        self._in = (_COLOR * 1)()
        self._out = (_COLOR * 1)()

    def _open(self, path: str) -> HANDLE:
        buf = ctypes.create_unicode_buffer(path)
        self._filenames.append(buf)
        prof = _PROFILE(PROFILE_FILENAME, ctypes.cast(buf, PVOID), (len(path) + 1) * 2)
        handle = self._lib.OpenColorProfileW(
            ctypes.byref(prof), PROFILE_READ, FILE_SHARE_READ, OPEN_EXISTING)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handles.append(handle)
        return handle

    def convert_pixel(self, r: float, g: float, b: float):
        """16-bit input/output request; the CMM quantizes to 8-bit internally."""
        self._in[0].rgb.red = int(round(min(max(r, 0.0), 1.0) * 65535))
        self._in[0].rgb.green = int(round(min(max(g, 0.0), 1.0) * 65535))
        self._in[0].rgb.blue = int(round(min(max(b, 0.0), 1.0) * 65535))
        ok = self._lib.TranslateColors(
            self._xform, ctypes.cast(self._in, PVOID), 1, COLOR_RGB,
            ctypes.cast(self._out, PVOID), COLOR_CMYK)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        q = self._out[0].cmyk
        return (q.cyan / 65535.0, q.magenta / 65535.0, q.yellow / 65535.0, q.black / 65535.0)

    def convert_buffer(self, rgb: bytes, width: int, height: int) -> bytes:
        """Convert a packed RGB byte buffer to packed CMYK bytes (8-bit)."""
        npix = int(width) * int(height)
        if len(rgb) != npix * 3:
            raise ValueError("RGB buffer size mismatch")
        src = ctypes.create_string_buffer(bytes(rgb), len(rgb))
        dst = ctypes.create_string_buffer(npix * 4)
        ok = self._lib.TranslateBitmapBits(
            self._xform, ctypes.cast(src, PVOID), BM_BGRTRIPLETS, int(width), int(height),
            int(width) * 3, ctypes.cast(dst, PVOID), BM_KYMCQUADS, int(width) * 4, None, None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return dst.raw[:npix * 4]

    def close(self) -> None:
        if self._xform:
            self._lib.DeleteColorTransform(self._xform)
            self._xform = None
        for handle in self._handles:
            self._lib.CloseColorProfile(handle)
        self._handles = []
        self._filenames = []

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

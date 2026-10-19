"""Image-level color processing for PostScript Level-1 `colorimage` data.

PostScript producers wrap page pixels in Level-1 `colorimage` calls, e.g.

    1743 2400 8[1743 0 0 -2400 0 2400]currentfile/ASCII85Decode filter
    /DCTDecode filter false 3 colorimage
    <ascii85 data>~>

Those pixels carry the page color, so operator-level rewriting cannot change
them. This module decodes such images (any combination of ASCII85Decode,
ASCIIHexDecode, FlateDecode and DCTDecode, with 1/3/4 components), converts
them to CMYK (ICC or naive for RGB, direct for CMYK), applies the same numeric
pipeline as `ColorEngine.transform_cmyk`, and emits them back as a
`/DeviceCMYK` Level-2 `image`.
"""
from __future__ import annotations

import base64
import io
import re
import zlib

_FILTER = rb'(?:ASCII85Decode|ASCIIHexDecode|FlateDecode|DCTDecode|RunLengthDecode|LZWDecode)'
_INVOKE = re.compile(
    rb'(\d+)\s+(\d+)\s+(\d+)\s*(\[[^\]]*\])\s*currentfile\s*'
    rb'((?:/\s*' + _FILTER + rb'\s+filter\s*)+)'
    rb'(true|false)\s+(\d)\s+colorimage'
)
_FILTERNAME = re.compile(rb'/' + _FILTER)
BAND = 512


def _apply_pipeline(engine, buf: bytes, npix: int) -> bytes:
    """Run the CMYK numeric pipeline (same math as ColorEngine.transform_cmyk).

    `buf` is packed 4-byte CMYK pixels. When no CMYK-side stage is active we
    return the bytes unchanged, so pure-Python conversion stays fast.
    """
    if len(buf) != npix * 4:
        raise ValueError("bad buffer size")
    if not engine.has_cmyk_adjust():
        return bytes(buf)
    tc = engine.transform_cmyk
    out = bytearray(len(buf))
    i = 0
    for _ in range(npix):
        c, m, y, k = tc(buf[i] / 255.0, buf[i + 1] / 255.0, buf[i + 2] / 255.0, buf[i + 3] / 255.0)
        out[i] = round(c * 255.0)
        out[i + 1] = round(m * 255.0)
        out[i + 2] = round(y * 255.0)
        out[i + 3] = round(k * 255.0)
        i += 4
    return bytes(out)


def _rgb_to_cmyk_bytes(chunk: bytes) -> bytearray:
    """Naive RGB->CMY (no ICC): C=1-R, M=1-G, Y=1-B, K=0."""
    buf = bytearray(len(chunk) // 3 * 4)
    j = 0
    for i in range(0, len(chunk), 3):
        buf[j] = 255 - chunk[i]
        buf[j + 1] = 255 - chunk[i + 1]
        buf[j + 2] = 255 - chunk[i + 2]
        j += 4
    return buf


def convert_rgb(engine, w: int, h: int, raw: bytes) -> bytes:
    backend = engine.rgb_backend()
    neutral = engine.wants_neutral(for_image=True)
    out = bytearray()
    for y0 in range(0, h, BAND):
        rows = min(BAND, h - y0)
        chunk = bytes(raw[y0 * w * 3:(y0 + rows) * w * 3])
        if backend is None:
            buf = _rgb_to_cmyk_bytes(chunk)
        else:
            try:
                buf = backend.buffer(chunk, w, rows)
            except Exception:
                buf = _rgb_to_cmyk_bytes(chunk)
        if neutral:
            buf = bytearray(buf)
            _neutral_to_k(engine, chunk, buf)
        out += _apply_pipeline(engine, buf, rows * w)
    return bytes(out)


def _neutral_to_k(engine, chunk: bytes, buf) -> None:
    """Overwrite near-neutral pixels in a packed CMYK buffer with K-only.

    `chunk` is the original RGB source; `buf` is the CMYK output for the same
    pixels (a bytearray, modified in place).
    """
    th = float(engine.s.neutral_threshold) * 255.0 + 0.5
    lo = float(engine.s.neutral_min) * 255.0
    n = len(chunk) // 3
    for i in range(n):
        r = chunk[i * 3]
        g = chunk[i * 3 + 1]
        b = chunk[i * 3 + 2]
        if max(r, g, b) - min(r, g, b) <= th and (r + g + b) / 3.0 >= lo:
            j = i * 4
            buf[j] = 0
            buf[j + 1] = 0
            buf[j + 2] = 0
            buf[j + 3] = 255 - (r + g + b) // 3


def convert_gray(engine, w: int, h: int, raw: bytes) -> bytes:
    # Dedicated loop so K uses exactly `1 - g/255` (same as gray_to_cmyk),
    # avoiding a 1-ulp difference from the byte-buffer pipeline.
    adjust = engine.has_cmyk_adjust()
    tc = engine.transform_cmyk if adjust else None
    out = bytearray()
    for y0 in range(0, h, BAND):
        rows = min(BAND, h - y0)
        chunk = raw[y0 * w:(y0 + rows) * w]
        buf = bytearray(len(chunk) * 4)
        j = 0
        if adjust:
            for g in chunk:
                c, m, y, k = tc(0.0, 0.0, 0.0, 1.0 - g / 255.0)
                buf[j] = round(c * 255.0)
                buf[j + 1] = round(m * 255.0)
                buf[j + 2] = round(y * 255.0)
                buf[j + 3] = round(k * 255.0)
                j += 4
        else:
            for g in chunk:
                buf[j + 3] = 255 - g
                j += 4
        out += buf
    return bytes(out)


def convert_cmyk(engine, w: int, h: int, raw: bytes) -> bytes:
    out = bytearray()
    for y0 in range(0, h, BAND):
        rows = min(BAND, h - y0)
        chunk = bytes(raw[y0 * w * 4:(y0 + rows) * w * 4])
        out += _apply_pipeline(engine, chunk, rows * w)
    return bytes(out)


def _read_binary(data: bytes, pos: int, filt: str):
    """Extract a bare binary stream (no ASCII terminator) from `currentfile`."""
    if filt == "DCTDecode":
        end = data.find(b"\xff\xd9", pos)
        if end < 0:
            return None, pos
        return data[pos:end + 2], end + 2
    if filt == "FlateDecode":
        obj = zlib.decompressobj()
        try:
            body = obj.decompress(data[pos:])
        except Exception:
            return None, pos
        if not obj.eof:
            return None, pos
        consumed = len(data) - pos - len(obj.unused_data)
        return body, pos + consumed
    return None, pos


def _decode(data: bytes, pos: int, filters):
    """Apply a PostScript filter chain to the bytes following `colorimage`.

    Returns (kind, buffer, new_pos) where kind is "raw" (fully decoded) or
    "jpeg" (a DCTDecode stream left for Pillow), or (None, None, pos) on failure.
    """
    first = filters[0]
    if first == "ASCII85Decode":
        end = data.find(b"~>", pos)
        if end < 0:
            return None, None, pos
        buf = base64.a85decode(data[pos:end], adobe=False)
        pos = end + 2
    elif first == "ASCIIHexDecode":
        end = data.find(b">", pos)
        if end < 0:
            return None, None, pos
        buf = bytes.fromhex(re.sub(rb"\s+", b"", data[pos:end]).decode("ascii"))
        pos = end + 1
    elif first in ("DCTDecode", "FlateDecode"):
        buf, pos = _read_binary(data, pos, first)
        if buf is None:
            return None, None, pos
    else:
        return None, None, pos

    for f in filters[1:]:
        if f == "FlateDecode":
            buf = zlib.decompress(buf)
        elif f == "ASCII85Decode":
            buf = base64.a85decode(buf, adobe=False)
        elif f == "ASCIIHexDecode":
            buf = bytes.fromhex(re.sub(rb"\s+", b"", buf).decode("ascii"))
        elif f == "DCTDecode":
            return "jpeg", buf, pos
        else:
            return None, None, pos
    return "raw", buf, pos


def _blank_stats(stats: dict, key: str) -> None:
    stats[key] = stats.get(key, 0) + 1


def rewrite_colorimages(data: bytes, engine, stats: dict) -> bytes:
    if not engine.wants_images():
        return data
    if not engine.has_active_color():
        return data

    out = bytearray()
    pos = 0
    while True:
        m = _INVOKE.search(data, pos)
        if not m:
            break

        w = int(m.group(1))
        h = int(m.group(2))
        bits = int(m.group(3))
        matrix = m.group(4)
        filters = [f.lstrip(b"/").decode("ascii") for f in _FILTERNAME.findall(m.group(5))]
        multipass = m.group(6) == b"true"
        n = int(m.group(7))

        if multipass or bits != 8 or w <= 0 or h <= 0 or n not in (1, 3, 4) or not filters:
            _blank_stats(stats, "images_skipped")
            out += data[pos:m.end()]
            pos = m.end()
            continue

        j = m.end()
        while j < len(data) and data[j] in b" \t\r\n":
            j += 1

        try:
            kind, buf, newpos = _decode(data, j, filters)
        except Exception:
            kind, buf, newpos = None, None, m.end()
        if kind is None:
            _blank_stats(stats, "images_skipped")
            out += data[pos:m.end()]
            pos = m.end()
            continue

        try:
            if kind == "jpeg":
                from PIL import Image

                img = Image.open(io.BytesIO(buf))
                img.load()
                w, h = img.size
                if img.mode == "CMYK":
                    n, raw = 4, img.tobytes()
                elif img.mode in ("L", "1"):
                    n, raw = 1, img.convert("L").tobytes()
                else:
                    n, raw = 3, img.convert("RGB").tobytes()
            else:
                raw = buf
                if len(raw) != w * h * n:
                    raise ValueError("length mismatch")

            if n == 1:
                cmyk = convert_gray(engine, w, h, raw)
            elif n == 3:
                cmyk = convert_rgb(engine, w, h, raw)
            else:
                cmyk = convert_cmyk(engine, w, h, raw)
            enc = base64.a85encode(zlib.compress(cmyk, 6), adobe=False)
        except Exception:
            _blank_stats(stats, "images_skipped")
            out += data[pos:m.end()]
            pos = m.end()
            continue

        out += data[pos:m.start()]
        out += (
            b"[/DeviceCMYK] setcolorspace << /ImageType 1 /Width %d /Height %d "
            b"/BitsPerComponent 8 /Decode [0 1 0 1 0 1 0 1] /ImageMatrix %s "
            b"/DataSource currentfile/ASCII85Decode filter /FlateDecode filter >> image\n"
            % (w, h, matrix)
        )
        out += enc + b"~>"
        pos = newpos
        stats["images"] = stats.get("images", 0) + 1

    out += data[pos:]
    return bytes(out)


# --- PScript5 (Windows PS driver) procedural image path -----------------------
#
# The driver does not use `colorimage`. It defines helper procs and invokes them:
#
#     : 1588 500 24 0 4759 1498 1020 0 F F 3 [ 0 0 0 ] F
#     X
#     doNimage
#     <ascii85>~> Z
#
# `X` selects `imageprocarray` index 3 = setup2asciiproc =
# `currentfile/ASCII85Decode filter/RunLengthDecode filter`, so the pixels are
# RunLength-encoded raw RGB (bpc 24) or gray (bpc 8). `doNimage` picks the
# colorspace/Decode at run time (`bpc 24 eq{currentcolorspace}{colspA}?`).
#
# We decode the pixels, run the CMYK pipeline, and re-emit them as ASCII85 +
# FlateDecode DeviceCMYK. That requires patching the shared procs so the driver's
# own decode chain and colorspace declaration match what we emit.

_DON_INVOKE = re.compile(
    rb":\s*([0-9][0-9\.\s]*?)\s+F\s+F\s+(\d+)\s*\[[^\]]*\]\s*F\s*\r?\n"
    rb"X\s*\r?\ndoNimage\s*\r?\n"
)

IDX_ASCII = 3   # imageprocarray[3] = setup2asciiproc  : ASCII85 + RunLength
IDX_BINARY = 2  # imageprocarray[2] = setup2binaryproc : RunLength (binary)

_SETUP2_ASCII_OLD = re.compile(
    rb"/setup2asciiproc\{currentfile/ASCII85Decode filter/RunLengthDecode\s+filter\}b")
_SETUP2_ASCII_NEW = b"/setup2asciiproc{currentfile/ASCII85Decode filter/FlateDecode filter}b"
_SETUP2_BIN_OLD = re.compile(rb"/setup2binaryproc\{currentfile/RunLengthDecode\s+filter\}b")
_SETUP2_BIN_NEW = b"/setup2binaryproc{currentfile/FlateDecode filter}b"

_CSP_OLD = b"bpc 24 eq{currentcolorspace}{colspA}?"
_DECODE_OLD = b"bpc 24 eq{[0 1 0 1 0 1]}{[0 1]}?"
_BPC_OLD = b"bpc 24 eq{8}{bpc}?"
_RGBGRAY_OLD = b"bpc 24 eq{/DeviceRGB}{/DeviceGray}?"


def _rle_decode(src: bytes, pos: int = 0):
    """PostScript RunLengthDecode. Returns (output, next_position)."""
    out = bytearray()
    i, n = pos, len(src)
    while i < n:
        c = src[i]
        i += 1
        if c == 128:
            break
        if c < 128:
            out += src[i:i + c + 1]
            i += c + 1
        else:
            if i >= n:
                break
            out += bytes([src[i]]) * (257 - c)
            i += 1
    return bytes(out), i


def _to_cmyk(engine, comps: int, raw: bytes) -> bytes:
    if comps == 3:
        return convert_rgb(engine, len(raw) // 3, 1, raw)
    return convert_gray(engine, len(raw), 1, raw)


def rewrite_pscript_images(data: bytes, engine, stats: dict) -> bytes:
    if not engine.wants_images():
        return data
    if not engine.has_active_color():
        return data
    if not (_SETUP2_ASCII_OLD.search(data) or _SETUP2_BIN_OLD.search(data)):
        return data

    invokes = list(_DON_INVOKE.finditer(data))
    if not invokes:
        return data

    replacements = []
    used = set()
    for m in invokes:
        idx = int(m.group(2))
        nums = re.findall(rb"[0-9]+(?:\.[0-9]+)?", m.group(1))
        if b"24" in nums:
            comps = 3
        elif b"8" in nums:
            comps = 1
        else:
            return data
        start = m.end()
        if idx == IDX_ASCII:
            end = data.find(b"~>", start)
            if end < 0:
                return data
            try:
                payload = re.sub(rb"\s+", b"", data[start:end])
                raw, _ = _rle_decode(base64.a85decode(payload, adobe=False))
            except Exception:
                return data
            if not raw or len(raw) % comps:
                return data
            enc = base64.a85encode(zlib.compress(_to_cmyk(engine, comps, raw), 6),
                                   adobe=False) + b"~>"
            replacements.append((start, end + 2, enc))
        elif idx == IDX_BINARY:
            raw, used_end = _rle_decode(data, start)
            if not raw or used_end <= start or len(raw) % comps:
                return data
            enc = zlib.compress(_to_cmyk(engine, comps, raw), 6)
            replacements.append((start, used_end, enc))
        else:
            return data
        used.add(idx)

    out = bytearray()
    last = 0
    for s, e, enc in replacements:
        out += data[last:s]
        out += enc
        last = e
    out += data[last:]
    out = bytes(out)

    if IDX_ASCII in used:
        out, n1 = _SETUP2_ASCII_OLD.subn(_SETUP2_ASCII_NEW, out, count=1)
        if not n1:
            return data
    if IDX_BINARY in used:
        out, n2 = _SETUP2_BIN_OLD.subn(_SETUP2_BIN_NEW, out, count=1)
        if not n2:
            return data
    out = out.replace(_CSP_OLD, b"/DeviceCMYK")
    out = out.replace(_DECODE_OLD, b"[0 1 0 1 0 1 0 1]")
    out = out.replace(_BPC_OLD, b"8")
    out = out.replace(_RGBGRAY_OLD, b"/DeviceCMYK")

    stats["pscript_images"] = stats.get("pscript_images", 0) + len(replacements)
    return out

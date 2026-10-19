"""Rewrite color operators in a PostScript stream.

Handles PostScript color operators (not PDF ones): setgray / setrgbcolor /
setcmykcolor / setcolor and the Level-2 sc/scn/SC/SCN forms under the current
setcolorspace. Unknown constructs are passed through unchanged; any failure
falls back to the original bytes (fail-open).
"""
from __future__ import annotations

import re

from .colorops import ColorEngine, fmt_num
from .imageops import rewrite_colorimages, rewrite_pscript_images
from .pstokens import Tok, tokenize, find_operands, find_colorspace

COLOR_CMYK = {b"setcmykcolor"}
COLOR_RGB = {b"setrgbcolor"}
COLOR_GRAY = {b"setgray"}
COLOR_CURRENT = {b"setcolor", b"sc", b"scn", b"SC", b"SCN"}
SPACE_ARITY = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}

# PScript5 (and other producers) define short aliases for the color operators,
# e.g. `/rg/setrgbcolor load def`, `/g/setgray load def`, `/k/setcmykcolor load def`.
# Real jobs then call the aliases, so the full-name dispatch above would miss them.
_ALIAS_DEFS = (
    re.compile(rb"/([^\s/()<>\[\]{}%]+)/\s*"
               rb"(setrgbcolor|setcmykcolor|setgray|setcolor|setcolorspace)\b\s+load\b"),
    re.compile(rb"/([^\s/()<>\[\]{}%]+)\s*\{\s*"
               rb"(setrgbcolor|setcmykcolor|setgray|setcolor|setcolorspace)\s*\}"
               rb"\s*(?:bind\s+)?def"),
)


def _alias_map(data: bytes) -> dict:
    """Map producer-defined alias names to the color operator they stand for."""
    aliases: dict = {}
    for rx in _ALIAS_DEFS:
        for name, op in rx.findall(data):
            if name != op:
                aliases[name] = op
    return aliases


def _append_operands(out: list[Tok], values) -> None:
    for idx, v in enumerate(values):
        if idx:
            out.append(Tok("ws", b" "))
        out.append(Tok("token", fmt_num(v)))


def _changed(old, new) -> bool:
    return any(abs(float(a) - float(b)) > 1e-9 for a, b in zip(old, new))


def _ensure_sep(out: list[Tok]) -> None:
    if out and out[-1].kind not in ("ws", "comment"):
        if out[-1].data[-1:] not in b"\x00\t\n\x0c\r ":
            out.append(Tok("ws", b" "))


def process_ps(data: bytes, engine: ColorEngine) -> bytes:
    try:
        toks = tokenize(data)
    except Exception:
        return data

    s = engine.s
    active = engine.has_active_color()
    aliases = _alias_map(data) if active else {}
    out: list[Tok] = []
    pos: dict[int, int] = {}
    current_space = None

    def replace_operands(start: int, new_values) -> None:
        cut = pos.get(start)
        if cut is None:
            return
        del out[cut:]
        _ensure_sep(out)
        _append_operands(out, new_values)
        out.append(Tok("ws", b" "))

    i = 0
    while i < len(toks):
        t = toks[i]
        handled = False

        if t.kind == "token" and active:
            d = aliases.get(t.data, t.data)

            if d == b"setcolorspace":
                space = find_colorspace(toks, i)
                if space:
                    current_space = space

            elif d in COLOR_CMYK:
                info = find_operands(toks, i, 4)
                if info:
                    start, values = info
                    new_values = engine.transform_cmyk(*values)
                    if _changed(values, new_values):
                        replace_operands(start, new_values)
                        out.append(t)
                        handled = True
                    engine._stats["cmyk"] += 1
                    current_space = "DeviceCMYK"

            elif d in COLOR_RGB:
                if engine.wants_rgb():
                    info = find_operands(toks, i, 3)
                    if info:
                        start, values = info
                        cmyk = engine.rgb_to_cmyk(*values)
                        replace_operands(start, engine.transform_cmyk(*cmyk))
                        out.append(Tok("token", b"setcmykcolor"))
                        engine._stats["rgb"] += 1
                        handled = True
                        current_space = "DeviceCMYK"
                else:
                    current_space = "DeviceRGB"

            elif d in COLOR_GRAY:
                if engine.wants_gray():
                    info = find_operands(toks, i, 1)
                    if info:
                        start, values = info
                        cmyk = engine.gray_to_cmyk(values[0])
                        replace_operands(start, engine.transform_cmyk(*cmyk))
                        out.append(Tok("token", b"setcmykcolor"))
                        engine._stats["gray"] += 1
                        handled = True
                        current_space = "DeviceCMYK"
                else:
                    current_space = "DeviceGray"

            elif d in COLOR_CURRENT:
                if current_space in SPACE_ARITY:
                    info = find_operands(toks, i, SPACE_ARITY[current_space])
                    if info:
                        start, values = info
                        if current_space == "DeviceCMYK":
                            new_values = engine.transform_cmyk(*values)
                            if _changed(values, new_values):
                                replace_operands(start, new_values)
                                out.append(t)
                                handled = True
                            engine._stats["cmyk"] += 1
                        elif current_space == "DeviceRGB" and engine.wants_rgb():
                            cmyk = engine.rgb_to_cmyk(*values)
                            replace_operands(start, engine.transform_cmyk(*cmyk))
                            out.append(Tok("token", b"setcmykcolor"))
                            engine._stats["rgb"] += 1
                            handled = True
                            current_space = "DeviceCMYK"
                        elif current_space == "DeviceGray" and engine.wants_gray():
                            cmyk = engine.gray_to_cmyk(values[0])
                            replace_operands(start, engine.transform_cmyk(*cmyk))
                            out.append(Tok("token", b"setcmykcolor"))
                            engine._stats["gray"] += 1
                            handled = True
                            current_space = "DeviceCMYK"

        if handled:
            pos[i] = len(out) - 1 if out else 0
        else:
            pos[i] = len(out)
            out.append(t)

        i += 1

    result = b"".join(x.data for x in out)
    try:
        result = rewrite_colorimages(result, engine, engine._stats)
    except Exception:
        pass
    try:
        result = rewrite_pscript_images(result, engine, engine._stats)
    except Exception:
        pass
    return result

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
# Text-showing operators. Their fill color is the current color, so protection
# is applied just before (and restored just after) each of these.
TEXT_OPS = {b"show", b"ashow", b"widthshow", b"awidthshow", b"kshow",
            b"xshow", b"yshow", b"xyshow", b"glyphshow", b"cshow"}
SPACE_ARITY = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}

# PScript5 (and other producers) define short aliases for the color operators,
# e.g. `/rg/setrgbcolor load def`, `/g/setgray load def`, `/k/setcmykcolor load def`.
# Real jobs then call the aliases, so the full-name dispatch above would miss them.
# Text operators can be aliased the same way (`/S/show load def`).
_OP_ALT = (rb"(setrgbcolor|setcmykcolor|setgray|setcolor|setcolorspace|"
           rb"show|ashow|widthshow|awidthshow|kshow|xshow|yshow|xyshow|"
           rb"glyphshow|cshow)")
_ALIAS_DEFS = (
    re.compile(rb"/([^\s/()<>\[\]{}%]+)/\s*" + _OP_ALT + rb"\b\s+load\b"),
    re.compile(rb"/([^\s/()<>\[\]{}%]+)\s*\{\s*" + _OP_ALT + rb"\s*\}"
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


def _emit_setcmyk(out: list[Tok], values) -> None:
    """Append `<c> <m> <y> <k> setcmykcolor` to the output."""
    _ensure_sep(out)
    _append_operands(out, values)
    out.append(Tok("ws", b" "))
    out.append(Tok("token", b"setcmykcolor"))
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
    cur_blackish = False
    blackish_stack: list[bool] = []

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

            if d in TEXT_OPS:
                if cur_blackish and engine.wants_text_solid_black():
                    _ensure_sep(out)
                    out.append(Tok("token", b"gsave"))
                    out.append(Tok("ws", b" "))
                    _emit_setcmyk(out, (0.0, 0.0, 0.0, 1.0))
                    out.append(t)
                    out.append(Tok("ws", b" "))
                    out.append(Tok("token", b"grestore"))
                    out.append(Tok("ws", b" "))
                    handled = True

            elif t.data == b"gsave":
                blackish_stack.append(cur_blackish)

            elif t.data == b"grestore":
                cur_blackish = blackish_stack.pop() if blackish_stack else False

            elif t.data == b"grestoreall":
                cur_blackish = False
                blackish_stack.clear()

            elif d == b"setcolorspace":
                space = find_colorspace(toks, i)
                if space:
                    current_space = space
                cur_blackish = False

            elif d in COLOR_CMYK:
                info = find_operands(toks, i, 4)
                if info:
                    start, values = info
                    cur_blackish = engine.is_blackish("DeviceCMYK", values)
                    new_values = engine.transform_cmyk(*values)
                    if _changed(values, new_values):
                        replace_operands(start, new_values)
                        out.append(t)
                        handled = True
                    engine._stats["cmyk"] += 1
                    current_space = "DeviceCMYK"

            elif d in COLOR_RGB:
                info = find_operands(toks, i, 3)
                if info:
                    start, values = info
                    cur_blackish = engine.is_blackish("DeviceRGB", values)
                if engine.wants_rgb():
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
                info = find_operands(toks, i, 1)
                if info:
                    cur_blackish = engine.is_blackish("DeviceGray", info[1])
                if engine.wants_gray():
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
                        cur_blackish = engine.is_blackish(current_space, values)
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

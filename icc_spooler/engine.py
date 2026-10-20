"""Rewrite color operators in a PostScript stream.

Handles PostScript color operators (not PDF ones): setgray / setrgbcolor /
setcmykcolor / setcolor and the Level-2 sc/scn/SC/SCN forms under the current
setcolorspace. Unknown constructs are passed through unchanged; any failure
falls back to the original bytes (fail-open).
"""
from __future__ import annotations

import re

from .colorops import ColorEngine, fmt_num, clamp01
from .imageops import image_data_spans, rewrite_colorimages, rewrite_pscript_images
from .pstokens import Tok, tokenize, find_operands, find_colorspace, is_number

COLOR_CMYK = {b"setcmykcolor"}
COLOR_RGB = {b"setrgbcolor"}
COLOR_GRAY = {b"setgray"}
COLOR_CURRENT = {b"setcolor", b"sc", b"scn", b"SC", b"SCN"}
# Text-showing operators. Their fill color is the current color, so protection
# is applied just before (and restored just after) each of these.
TEXT_OPS = {b"show", b"ashow", b"widthshow", b"awidthshow", b"kshow",
            b"xshow", b"yshow", b"xyshow", b"glyphshow", b"cshow"}
SPACE_ARITY = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}
# Matrix operators that change the CTM scale (used to estimate text size).
CONCAT_OPS = {b"concat", b"cm"}

# PScript5 (and other producers) define short aliases for the color operators,
# e.g. `/rg/setrgbcolor load def`, `/g/setgray load def`, `/k/setcmykcolor load def`.
# Real jobs then call the aliases, so the full-name dispatch above would miss them.
# Text (`/Tj/show`) and graphics-state (`/q/gsave`) operators are aliased the same way.
_OP_ALT = (rb"(setrgbcolor|setcmykcolor|setgray|setcolor|setcolorspace|"
           rb"show|ashow|widthshow|awidthshow|kshow|xshow|yshow|xyshow|"
           rb"glyphshow|cshow|gsave|grestore|grestoreall|concat|setmatrix|"
           rb"initmatrix|scale|rotate|translate)")
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


def _emit_sep_black(out: list[Tok], tint: float) -> None:
    """Set a device-black separation tint, i.e. pure K with no CMY.

    Acrobat's PScript5 driver draws neutral map text this way:
    ``true sop 1 /0 /CSD get_res sepcs <tint> sep``. A plain
    ``setcmykcolor`` would be run through the printer's CMYK profile (rich
    black, four screens); a separation reaches the K plate directly, so the
    glyph strokes stay crisp and match Acrobat. Black text also overprints, so
    glyphs sitting on the map image do not knock a white hole in it.
    """
    _ensure_sep(out)
    out.append(Tok("token", b"[/Separation/Black/DeviceCMYK{0 0 0 4 -1 roll}]setcolorspace"))
    out.append(Tok("ws", b" "))
    out.append(Tok("token", fmt_num(tint)))
    out.append(Tok("ws", b" "))
    out.append(Tok("token", b"setcolor"))
    out.append(Tok("ws", b" "))
    out.append(Tok("token", b"/setoverprint where{pop true setoverprint}if"))
    out.append(Tok("ws", b" "))


def find_matrix(toks: list[Tok], op_index: int):
    """Return the 6 numbers of the matrix operand for cm/concat/setmatrix.

    Accepts both the array form (`[a b c d e f] cm`) and the inline form
    (`a b c d e f cm`). Returns None when the operand is a name/procedure
    (e.g. a page-setup matrix defined elsewhere), which is simply skipped.
    """
    j = op_index - 1
    while j >= 0 and toks[j].kind in ("ws", "comment"):
        j -= 1
    if j < 0:
        return None
    if toks[j].kind == "punct" and toks[j].data == b"]":
        depth = 0
        nums: list[float] = []
        k = j
        while k >= 0:
            t = toks[k]
            if t.kind == "punct" and t.data == b"]":
                depth += 1
            elif t.kind == "punct" and t.data == b"[":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and t.kind == "token" and is_number(t.data):
                nums.append(float(t.data))
            elif depth == 1 and t.kind not in ("ws", "comment"):
                return None
            k -= 1
        if k < 0 or depth != 0:
            return None
        nums.reverse()
        return nums if len(nums) == 6 else None
    info = find_operands(toks, op_index, 6)
    if info:
        return info[1]
    return None


def _already_solid_black(space: str, values) -> bool:
    if space == "DeviceGray":
        return clamp01(values[0]) <= 1e-9
    if space == "DeviceCMYK":
        c, m, y, k = (clamp01(v) for v in values[:4])
        return max(c, m, y) <= 1e-9 and k >= 1.0 - 1e-9
    return False


def process_ps(data: bytes, engine: ColorEngine) -> bytes:
    try:
        toks = tokenize(data, image_data_spans(data))
    except Exception:
        return data

    s = engine.s
    active = engine.has_active_color()
    aliases = _alias_map(data) if active else {}
    out: list[Tok] = []
    pos: dict[int, int] = {}
    current_space = None
    # Current fill color as the document specifies it: (space, values) or None.
    cur_color = None
    # Approximate CTM scale (starts at 1; named page-setup matrices are skipped),
    # used only to tell small text from large text.
    cur_scale = 1.0
    gstack: list = []
    k_gain = max(0.0, float(s.text_k_gain))

    def push_state() -> None:
        gstack.append((cur_scale, cur_color))

    def pop_state() -> None:
        nonlocal cur_scale, cur_color
        if gstack:
            cur_scale, cur_color = gstack.pop()
        else:
            cur_scale, cur_color = 1.0, None

    def scale_by(factor: float) -> None:
        nonlocal cur_scale
        if factor > 1e-9:
            cur_scale *= factor

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
                target = None
                if engine.wants_text_solid_black() and cur_color is not None:
                    space, vals = cur_color
                    neutral, tone = engine.text_neutral_tone(space, vals)
                    if neutral and not _already_solid_black(space, vals):
                        k = min(1.0, tone * k_gain)
                        if k > 1e-9:
                            target = k
                if target is not None:
                    _ensure_sep(out)
                    out.append(Tok("token", b"gsave"))
                    out.append(Tok("ws", b" "))
                    _emit_sep_black(out, target)
                    out.append(t)
                    out.append(Tok("ws", b" "))
                    out.append(Tok("token", b"grestore"))
                    out.append(Tok("ws", b" "))
                    handled = True
                    engine._stats["text"] += 1

            elif d == b"gsave":
                push_state()

            elif d == b"grestore":
                pop_state()

            elif d == b"grestoreall":
                if gstack:
                    cur_scale, cur_color = gstack[0]
                    gstack.clear()
                else:
                    cur_scale, cur_color = 1.0, None

            elif d in CONCAT_OPS:
                m = find_matrix(toks, i)
                if m:
                    scale_by(abs(m[0] * m[3] - m[1] * m[2]) ** 0.5)

            elif d == b"scale":
                info = find_operands(toks, i, 2)
                if info:
                    sx, sy = info[1]
                    scale_by(abs(sx * sy) ** 0.5)

            elif d == b"setmatrix":
                m = find_matrix(toks, i)
                if m:
                    cur_scale = abs(m[0] * m[3] - m[1] * m[2]) ** 0.5

            elif d == b"initmatrix":
                cur_scale = 1.0

            elif d == b"setcolorspace":
                space = find_colorspace(toks, i)
                if space:
                    current_space = space
                cur_color = None

            elif d in COLOR_CMYK:
                info = find_operands(toks, i, 4)
                if info:
                    start, values = info
                    cur_color = ("DeviceCMYK", values)
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
                    cur_color = ("DeviceRGB", values)
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
                    cur_color = ("DeviceGray", info[1])
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
                        cur_color = (current_space, values)
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

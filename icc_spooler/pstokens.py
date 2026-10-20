"""A small PostScript lexer used to locate and rewrite color operators.

It splits a PostScript byte stream into lossless tokens so the byte stream can
be reassembled unchanged except where a token is intentionally replaced.
Strings, hex strings, dictionaries, comments, whitespace and binary regions are
kept as opaque tokens; numbers, names, brackets and operators are addressable.
"""
from __future__ import annotations

from dataclasses import dataclass

WS = b"\x00\t\n\x0c\r "
DELIMS = b"()<>[]{}/%"


@dataclass
class Tok:
    kind: str
    data: bytes


def _scan_string(data: bytes, start: int) -> int:
    n = len(data)
    j = start + 1
    depth = 1
    esc = False
    while j < n:
        c = data[j]
        if esc:
            esc = False
        elif c == 0x5C:
            esc = True
        elif c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return min(j + 1, n)
        j += 1
    return n


def _scan_hexstring(data: bytes, start: int) -> int:
    n = len(data)
    j = start + 1
    while j < n and data[j] != 0x3E:
        j += 1
    return min(j + 1, n)


def _scan_dict(data: bytes, start: int) -> int:
    n = len(data)
    j = start
    depth = 0
    while j < n:
        if data[j:j + 2] == b"<<":
            depth += 1
            j += 2
            continue
        if data[j:j + 2] == b">>":
            depth -= 1
            j += 2
            if depth <= 0:
                return j
            continue
        if data[j] == 0x28:
            j = _scan_string(data, j)
            continue
        j += 1
    return n


def _scan_binary(data: bytes, start: int) -> int:
    n = len(data)
    j = start + 2
    while j + 1 < n:
        if data[j] in WS and data[j + 1:j + 3] == b"EI" and (j + 3 >= n or data[j + 3] in WS + DELIMS):
            return j + 3
        j += 1
    return n


def tokenize(data: bytes, skip=None) -> list[Tok]:
    """Split PostScript into tokens.

    `skip` is a list of (start, end) byte ranges that must be kept verbatim as
    a single opaque token. Image data after `colorimage`/`image` is binary and
    may contain parentheses, percent signs or stray whitespace; without this
    the scanner would mis-read it and lose every operator that follows.
    """
    spans = sorted(skip) if skip else []
    si = 0
    toks: list[Tok] = []
    i = 0
    n = len(data)
    while i < n:
        while si < len(spans) and spans[si][1] <= i:
            si += 1
        if si < len(spans) and spans[si][0] <= i < spans[si][1]:
            end = spans[si][1]
            toks.append(Tok("opaque", data[i:end]))
            i = end
            si += 1
            continue
        ch = data[i]
        if ch in WS:
            j = i
            while j < n and data[j] in WS:
                j += 1
            toks.append(Tok("ws", data[i:j]))
            i = j
            continue
        if ch == 0x25:  # %
            j = i
            while j < n and data[j] not in b"\r\n":
                j += 1
            toks.append(Tok("comment", data[i:j]))
            i = j
            continue
        if ch == 0x28:  # (
            j = _scan_string(data, i)
            toks.append(Tok("string", data[i:j]))
            i = j
            continue
        if ch == 0x3C:  # <
            if data[i + 1:i + 2] == b"<":
                j = _scan_dict(data, i)
                toks.append(Tok("dict", data[i:j]))
                i = j
                continue
            j = _scan_hexstring(data, i)
            toks.append(Tok("hexstring", data[i:j]))
            i = j
            continue
        if ch == 0x3E and data[i + 1:i + 2] == b">":
            toks.append(Tok("punct", data[i:i + 2]))
            i += 2
            continue
        if ch in b"[]{}":
            toks.append(Tok("punct", data[i:i + 1]))
            i += 1
            continue
        if ch == 0x2F:  # /name
            j = i + 1
            while j < n and data[j] not in WS and data[j] not in DELIMS:
                j += 1
            toks.append(Tok("name", data[i:j]))
            i = j
            continue
        j = i
        while j < n and data[j] not in WS and data[j] not in DELIMS:
            j += 1
        if j == i:
            toks.append(Tok("punct", data[i:i + 1]))
            i += 1
            continue
        token = data[i:j]
        if token == b"ID" and j < n and data[j] not in b"\r\n":
            end = _scan_binary(data, i)
            toks.append(Tok("opaque", data[i:end]))
            i = end
            continue
        toks.append(Tok("token", token))
        i = j
    return toks


def is_number(token: bytes) -> bool:
    if not token:
        return False
    if b"#" in token:
        return False
    try:
        float(token)
        return True
    except ValueError:
        return False


def decode_name(token: bytes) -> str:
    if token.startswith(b"/"):
        token = token[1:]
    return token.decode("latin-1")


def find_operands(toks: list[Tok], op_index: int, count: int):
    values: list[float] = []
    indices: list[int] = []
    j = op_index - 1
    while j >= 0 and len(values) < count:
        t = toks[j]
        if t.kind in ("ws", "comment"):
            j -= 1
            continue
        if t.kind == "token" and is_number(t.data):
            values.append(float(t.data))
            indices.append(j)
            j -= 1
            continue
        return None
    if len(values) < count:
        return None
    values.reverse()
    return indices[-1], values


def find_colorspace(toks: list[Tok], op_index: int):
    j = op_index - 1
    while j >= 0 and toks[j].kind in ("ws", "comment"):
        j -= 1
    if j < 0:
        return None
    if toks[j].kind == "punct" and toks[j].data == b"]":
        depth = 0
        k = j
        while k >= 0:
            if toks[k].kind == "punct" and toks[k].data == b"]":
                depth += 1
            elif toks[k].kind == "punct" and toks[k].data == b"[":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        if k < 0:
            return None
        m = k + 1
        while m < j:
            if toks[m].kind == "name":
                return decode_name(toks[m].data)
            m += 1
        return None
    if toks[j].kind == "name":
        return decode_name(toks[j].data)
    return None

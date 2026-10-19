"""Lightweight JSON-based translations for the ICC spooler.

Source strings in the code are **English** and are used directly as the
translation keys. The English locale is therefore built in (identity) and does
not need a file. Other locales live next to this module in ``lang/<code>.json``
and map each English source string to its translation, e.g.::

    { "Start Service": "启动服务", "__name__": "中文" }

The special ``__name__`` entry is the language's display name (shown in the GUI
language picker); it is skipped when looking up strings.

Usage::

    from .i18n import tr, set_language, available_languages
    set_language("zh")          # once at start-up, from config
    label = tr("Start Service")
    msg = tr("Jobs {jobs} · Errors {errors}").format(jobs=3, errors=0)

If a string is missing from the active catalog, the English source is returned,
so an incomplete translation degrades gracefully.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_LANGUAGE = "en"
_NAME_KEY = "__name__"

_LANG_DIR = Path(__file__).with_name("lang")
_code = DEFAULT_LANGUAGE
_catalog: dict[str, str] = {}


def _load(code: str) -> dict[str, str]:
    if not code or code == DEFAULT_LANGUAGE:
        return {}
    path = _LANG_DIR / f"{code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/broken file -> fall back to English
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, str) and k != _NAME_KEY}


def set_language(code: str | None) -> str:
    """Activate a language by code; returns the code actually used."""
    global _code, _catalog
    _code = code or DEFAULT_LANGUAGE
    _catalog = _load(_code)
    return _code


def current_language() -> str:
    return _code


def available_languages() -> dict[str, str]:
    """Map language code -> display name, always including built-in English."""
    langs = {DEFAULT_LANGUAGE: "English"}
    if _LANG_DIR.is_dir():
        for path in sorted(_LANG_DIR.glob("*.json")):
            code = path.stem
            name = code
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                name = str(data.get(_NAME_KEY, code))
            except Exception:  # noqa: BLE001
                pass
            langs[code] = name
    return langs


def tr(text: str, **fmt) -> str:
    """Translate ``text`` and optionally ``str.format`` it with ``fmt``."""
    s = _catalog.get(text, text)
    if fmt:
        try:
            s = s.format(**fmt)
        except Exception:  # noqa: BLE001 - never break the UI over a bad template
            pass
    return s

"""Interface language: Ukrainian by default, English as the second language.

Source strings are English; `_()` looks them up in the Ukrainian catalog
(zir/locale_uk.py). Key bindings are class attributes that Textual caches
when the class is created, so classes with translatable key labels are
registered with `@localized` and re-labelled on every language change.
"""

from __future__ import annotations

import os
from dataclasses import replace

from .locale_uk import UK, UK_PLURAL

LANGUAGES = {"uk": "Українська", "en": "English"}
DEFAULT_LANGUAGE = "uk"

_current = DEFAULT_LANGUAGE
_registry: list[type] = []


def normalize(value: object) -> str | None:
    """'uk', 'UK', 'ua', 'uk_UA.UTF-8', 'en_US' -> 'uk' / 'en'; None if unknown."""
    if not isinstance(value, str) or not value.strip():
        return None
    code = value.strip().lower().replace("-", "_").split(".")[0].split("_")[0]
    code = {"ua": "uk"}.get(code, code)
    return code if code in LANGUAGES else None


def get_language() -> str:
    return _current


def set_language(language: str | None) -> str:
    global _current
    _current = normalize(language) or DEFAULT_LANGUAGE
    for cls in _registry:
        _apply_bindings(cls)
    return _current


def env_language() -> str | None:
    return normalize(os.environ.get("ZIR_LANG") or os.environ.get("SDECK_LANG"))


def _(text: str, **values: object) -> str:
    if _current == "uk":
        text = UK.get(text, text)
    return text.format(**values) if values else text


def _uk_form(n: int) -> int:
    if n % 10 == 1 and n % 100 != 11:
        return 0
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 1
    return 2


def _n(singular: str, plural: str, n: int, **values: object) -> str:
    """Plural-aware `_()`; `{n}` in the text is the count."""
    if _current == "uk" and singular in UK_PLURAL:
        text = UK_PLURAL[singular][_uk_form(abs(n))]
    else:
        text = singular if n == 1 else plural
    return text.format(n=n, **values)


# --------------------------------------------------------------------------
# Key bindings
# --------------------------------------------------------------------------


def _apply_bindings(cls: type) -> None:
    # Imported here: report mode (--report) uses `_()` but never loads Textual.
    from textual.binding import Binding

    cls.BINDINGS = [
        replace(binding, description=_(binding.description))
        if isinstance(binding, Binding) and binding.description
        else binding
        for binding in cls._BINDINGS_SOURCE
    ]
    # Textual merges BINDINGS once, in __init_subclass__; redo it for the new labels.
    cls._merged_bindings = cls._merge_bindings()


def localized(cls: type) -> type:
    """Class decorator: translate the descriptions of the class's BINDINGS."""
    cls._BINDINGS_SOURCE = list(cls.__dict__.get("BINDINGS", []))
    _registry.append(cls)
    _apply_bindings(cls)
    return cls

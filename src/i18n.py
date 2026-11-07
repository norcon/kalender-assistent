"""Mehrsprachigkeit: Deutsch ist die Quellsprache, Übersetzungen in src/locales/<lang>.json.

Texte werden mit _("Deutscher Text {platzhalter}", platzhalter=wert) markiert. Die
Sprache gilt pro Anfrage (Cookie „lang“, sonst Einstellung bzw. Browsersprache).
"""

import contextvars
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGUAGES = {"de": "Deutsch", "en": "English"}
DEFAULT = "de"
_LOCALES = Path(__file__).resolve().parent / "locales"
_current: contextvars.ContextVar[str] = contextvars.ContextVar("language", default=DEFAULT)


@lru_cache(maxsize=None)
def catalog(language: str) -> dict[str, str]:
    if language == DEFAULT or language not in LANGUAGES:
        return {}
    path = _LOCALES / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def current() -> str:
    return _current.get()


def activate(language: str) -> contextvars.Token:
    return _current.set(language if language in LANGUAGES else DEFAULT)


def reset(token: contextvars.Token) -> None:
    _current.reset(token)


_service = "iCloud"  # Name des Kalenderdienstes; ersetzt „iCloud“ in allen Texten


def set_service_name(name: str) -> None:
    global _service
    _service = (name or "").strip() or "iCloud"


def service_name() -> str:
    return _service


def brand(text: str) -> str:
    return text if _service == "iCloud" else text.replace("iCloud", _service)


def js_catalog(language: str) -> dict[str, str]:
    """Übersetzungen für den Browser, mit eingesetztem Dienstnamen."""
    base = catalog(language) if language != DEFAULT else {}
    result = {key: brand(value) for key, value in base.items()}
    if _service != "iCloud" and language == DEFAULT:
        result.update({key: brand(key) for key in catalog("en") if "iCloud" in key})
    return result


def gettext(text: str, /, **values: Any) -> str:
    translated = brand(catalog(current()).get(text, text))
    return translated.format(**values) if values else translated


def ngettext(singular: str, plural: str, count: int, /, **values: Any) -> str:
    return gettext(singular if count == 1 else plural, count=count, **values)


_ = gettext


def negotiate(cookie: str | None, setting: str, accept_language: str | None) -> str:
    """Cookie vor Einstellung; „auto“ nimmt die bevorzugte Browsersprache."""
    if cookie in LANGUAGES:
        return cookie
    if setting in LANGUAGES:
        return setting
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGUAGES:
            return code
    return DEFAULT

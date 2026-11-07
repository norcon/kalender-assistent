"""Verbindungsdaten (Postfächer, KI, Kalender-Server, Benachrichtigungen) verschlüsselt in der DB.

- Der Schlüssel liegt in einer eigenen Datei neben der Datenbank (`secret.key`,
  nur für den App-Benutzer lesbar) oder in APP_SECRET_KEY. Datenbank-
  Sicherungen enthalten nur den verschlüsselten Block – ohne Schlüssel wertlos.
- `.env` dient nur noch als „Eingang“: Werte dort werden beim Start
  übernommen und danach (über den Updater) aus `.env` entfernt. Ein
  unveränderter alter Wert in `.env` überschreibt nie eine Änderung aus der
  Oberfläche – maßgeblich ist, ob er sich seit der letzten Übernahme geändert hat.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import CONNECTION_KEYS

logger = logging.getLogger(__name__)
STATE_KEY = "connections_v1"
ENV_HASHES = "_env"  # interne Ablage: Prüfsumme je übernommenem .env-Wert

SECRET_KEYS = frozenset(
    key for key in CONNECTION_KEYS
    if key.endswith(("_PASSWORD", "_TOKEN", "_API_KEY"))
)


class StoreError(Exception):
    """Gespeicherte Verbindungen lassen sich nicht lesen (z. B. Schlüssel fehlt)."""


def value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def key_path(database_path: str) -> Path:
    return Path(database_path).parent / "secret.key"


def _fernet(database_path: str, *, create: bool) -> Fernet:
    configured = os.getenv("APP_SECRET_KEY", "").strip()
    if configured:
        return Fernet(configured.encode("ascii"))
    path = key_path(database_path)
    if not path.exists():
        if not create:
            raise StoreError("Schlüsseldatei fehlt")
        key = Fernet.generate_key()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        logger.info("Schlüssel für gespeicherte Verbindungen angelegt (%s).", path.name)
    return Fernet(path.read_bytes().strip())


def load(database_path: str) -> dict[str, Any]:
    """Entschlüsselte Ablage (inkl. interner Prüfsummen); leer, wenn noch nichts gespeichert."""
    raw = db.get_service_state(database_path).get(STATE_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(_fernet(database_path, create=False).decrypt(raw.encode("ascii")))
    except (InvalidToken, ValueError, OSError) as error:
        raise StoreError(type(error).__name__) from error
    return data if isinstance(data, dict) else {}


def save(database_path: str, data: dict[str, Any]) -> None:
    token = _fernet(database_path, create=True).encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    db.set_service_state(database_path, **{STATE_KEY: token.decode("ascii")})


def values(data: Mapping[str, Any]) -> dict[str, str]:
    """Nur die Verbindungswerte (ohne interne Einträge)."""
    return {key: str(value) for key, value in data.items() if key in CONNECTION_KEYS and value not in (None, "")}


def absorb_env(database_path: str, environ: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    """Neue oder geänderte Werte aus .env übernehmen.

    Rückgabe: (übernommene Schlüssel, {Schlüssel: Prüfsumme} aller .env-Werte,
    die jetzt sicher in der DB stehen und aus .env entfernt werden dürfen).
    """
    data = load(database_path)
    hashes: dict[str, str] = dict(data.get(ENV_HASHES) or {})
    absorbed = []
    for key in CONNECTION_KEYS:
        value = _env_value(environ, key)
        if not value:
            continue
        digest = value_hash(value)
        if hashes.get(key) != digest:
            data[key] = value
            hashes[key] = digest
            absorbed.append(key)
    if absorbed:
        data[ENV_HASHES] = hashes
        save(database_path, data)
        if values(load(database_path)) != values(data):  # zurücklesen, bevor .env aufgeräumt wird
            raise StoreError("Kontrolle nach dem Speichern fehlgeschlagen")
        logger.info("Verbindungsdaten aus .env übernommen: %s.", ", ".join(absorbed))
    removable = {
        key: hashes[key] for key in CONNECTION_KEYS
        if _env_value(environ, key) and hashes.get(key) == value_hash(_env_value(environ, key))
    }
    return absorbed, removable


def _env_value(environ: Mapping[str, str], key: str) -> str:
    value = (environ.get(key) or "").strip("\r\n")
    return value if key in SECRET_KEYS else value.strip()


def update(database_path: str, changes: Mapping[str, str | None]) -> None:
    """Werte aus der Oberfläche setzen (None oder "" = entfernen)."""
    data = load(database_path)
    for key, value in changes.items():
        if key not in CONNECTION_KEYS:
            raise ValueError(key)
        if value in (None, ""):
            data.pop(key, None)
        else:
            data[key] = str(value)
    save(database_path, data)

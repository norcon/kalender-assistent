"""Zugangsschutz der Oberfläche – gespeichert in der Datenbank.

- Das Passwort liegt nur als scrypt-Hash in `service_state` (nie im Klartext).
- Ein Passwort ist optional; mit Passwort fragt die Oberfläche beim Öffnen danach.
- APP_PASSWORD in `.env` (ältere Versionen) wird beim Start übernommen und danach
  vom Updater entfernt – ebenso das frühere CONNECTIONS_EDIT_WITHOUT_PASSWORD,
  das keine Bedeutung mehr hat.
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from typing import Mapping

from . import db

PASSWORD_KEY = "app_password_hash"
ENV_SEEN_KEY = "access_env_seen"  # was zuletzt aus .env übernommen wurde
MIN_LENGTH = 8
KEEP_STATE = (PASSWORD_KEY,)  # überdauern eine Wiederherstellung
_TRUE = {"1", "true", "yes", "ja"}
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), dklen=32,
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual.hex(), digest)


def load(database_path: str) -> str:
    """Passwort-Hash oder "" (kein Passwort)."""
    return db.get_service_state(database_path).get(PASSWORD_KEY, "")


def set_password(database_path: str, password: str | None) -> None:
    """Neues Passwort setzen; None oder "" schaltet den Zugangsschutz ab."""
    if password and len(password) < MIN_LENGTH:
        raise ValueError(f"mindestens {MIN_LENGTH} Zeichen")
    if password:
        db.set_service_state(database_path, **{PASSWORD_KEY: hash_password(password)})
    else:
        db.delete_service_state(database_path, PASSWORD_KEY)


def absorb_env(database_path: str, environ: Mapping[str, str]) -> dict[str, str]:
    """APP_PASSWORD aus .env übernehmen (CONNECTIONS_EDIT_WITHOUT_PASSWORD nur entfernen).

    Übernommen wird nur ein neuer oder geänderter Wert – ein alter Wert in .env
    überschreibt nie eine Änderung aus der Oberfläche. Rückgabe:
    {.env-Schlüssel: Prüfsumme} der Zeilen, die aus .env entfernt werden dürfen.
    """
    state = db.get_service_state(database_path)
    try:
        seen: dict[str, str] = json.loads(state.get(ENV_SEEN_KEY) or "{}")
    except ValueError:
        seen = {}
    removable: dict[str, str] = {}
    changed = False
    if "APP_PASSWORD" in environ:
        raw = environ["APP_PASSWORD"]
        password = raw.strip("\r\n")
        if password and not verify_password(password, seen.get("APP_PASSWORD", "")):
            # ohne Mindestlänge: ältere Versionen kannten keine
            db.set_service_state(database_path, **{PASSWORD_KEY: hash_password(password)})
            seen["APP_PASSWORD"] = hash_password(password)
            changed = True
        removable["APP_PASSWORD"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if "CONNECTIONS_EDIT_WITHOUT_PASSWORD" in environ:  # früherer Schalter, ohne Bedeutung
        raw = environ["CONNECTIONS_EDIT_WITHOUT_PASSWORD"]
        removable["CONNECTIONS_EDIT_WITHOUT_PASSWORD"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if changed:
        db.set_service_state(database_path, **{ENV_SEEN_KEY: json.dumps(seen, sort_keys=True)})
    return removable


def main(argv: list[str] | None = None) -> int:
    """Auf dem Server, z. B.:  docker compose exec kalender-assistent python -m src.access passwort-weg"""
    parser = argparse.ArgumentParser(prog="python -m src.access", description="Zugangsschutz verwalten")
    parser.add_argument("befehl", choices=("status", "passwort-weg"))
    args = parser.parse_args(argv)
    database_path = os.getenv("APP_DATABASE_PATH", "/data/calendar-assistant.sqlite3")
    db.initialize(database_path)
    if args.befehl == "passwort-weg":
        set_password(database_path, None)
        print("Passwort entfernt. Neu setzen unter Einstellungen → Zugangsschutz.")
    print(f"Passwortschutz: {'an' if load(database_path) else 'aus'}")
    print("Die laufende App übernimmt Änderungen nach:  docker compose restart kalender-assistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())

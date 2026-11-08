"""Updater für den Kalender-Assistenten (eigener Container, optional).

Nur dieser Dienst hat Zugriff auf Docker. Er ist ausschließlich im internen
Compose-Netz erreichbar und kann genau eines: die App auf eine bestimmte
Versionsnummer (x.y.z) des festgelegten Images umstellen – mit Sicherung der
Konfiguration vorher und automatischer Rückkehr, wenn die neue Version nicht
sauber startet.

HTTP (intern):
  GET  /status   → aktueller bzw. letzter Vorgang (JSON)
  POST /update   {"version", "previous", "backup", "restore_backup"}
  POST /configure {"values": {...}}  – nur Ersteinrichtung, nur erlaubte Schlüssel,
                  nur solange in .env noch keine Postfach-Zugangsdaten stehen
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("updater")

VERSION_PATTERN = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
BACKUP_NAME_PATTERN = re.compile(r"^calendar-assistant-[0-9_-]+\.sqlite3$")
ENV_KEY = "KALENDER_VERSION"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- .env bearbeiten ----------

def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        inner, result, index = raw[1:-1], [], 0
        while index < len(inner):
            pair = inner[index:index + 2]
            if pair in ("\\\\", '\\"'):
                result.append(pair[1]); index += 2
            elif pair == "$$":
                result.append("$"); index += 2
            else:
                result.append(inner[index]); index += 1
        return "".join(result)
    return raw


def quote_env_value(value: str) -> str:
    """Schreibweise, die Docker Compose unverändert übernimmt (geprüft mit Compose v2)."""
    if any(ord(character) < 32 for character in value):
        raise ValueError("Steuerzeichen oder Zeilenumbrüche sind nicht erlaubt.")
    if re.fullmatch(r"[A-Za-z0-9._:/@,+-]*", value):
        return value  # einfache Werte lesbar ohne Anführungszeichen
    if "'" not in value:
        return f"'{value}'"  # wörtlich, keine $-Auswertung
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{escaped}"'


def read_env_value(path: Path, key: str) -> str | None:
    return _env_line_value(path, key) or None


def _env_line_value(path: Path, key: str) -> str | None:
    """Wert einer Zeile KEY=…; "" bei leerer Zeile, None wenn sie fehlt."""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return _unquote(stripped.split("=", 1)[1])
    return None


def set_env_values(path: Path, values: dict[str, str | None]) -> None:
    """Setzt oder entfernt KEY=value; alle anderen Zeilen bleiben unverändert."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if not any(line.strip().startswith(f"{key}=") for key in values)]
    for key, value in values.items():
        if value is not None:
            kept.append(f"{key}={quote_env_value(value)}")
    temporary = path.with_name(path.name + ".updater-tmp")
    temporary.write_text("\n".join(kept) + "\n", encoding="utf-8")
    if path.exists():
        stat = path.stat()
        os.chmod(temporary, stat.st_mode & 0o7777)
        try:
            os.chown(temporary, stat.st_uid, stat.st_gid)
        except PermissionError:
            pass
    else:
        # neu angelegt (.env ist optional): Besitzer wie compose.yaml, damit
        # `docker compose` sie auch ohne root lesen kann
        os.chmod(temporary, 0o600)
        owner = path.with_name("compose.yaml")
        try:
            stat = (owner if owner.exists() else path.parent).stat()
            os.chown(temporary, stat.st_uid, stat.st_gid)
        except (OSError, PermissionError):
            pass
    temporary.replace(path)


def set_env_value(path: Path, key: str, value: str | None) -> None:
    set_env_values(path, {key: value})


# Schlüssel, die die Ersteinrichtung schreiben darf – sonst nichts.
SETUP_KEYS = {
    "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_FOLDER",
    "AI_BASE_URL", "AI_API_KEY", "AI_MODEL",
    "CALDAV_URL", "CALDAV_USERNAME", "CALDAV_APP_PASSWORD", "CALDAV_TIMEZONE",
    "APP_LANGUAGE", "TARGET_CALENDARS", "APP_PASSWORD", "CALDAV_NAME", "CALDAV_AUTH",
    "CONNECTIONS_EDIT_WITHOUT_PASSWORD",
}
CREDENTIAL_KEYS = ("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD")
# Verbindungsdaten, die die App verschlüsselt übernommen hat und die aus .env
# entfernt werden dürfen (siehe src/connections.py) – sonst nichts.
CLEANUP_KEY_PATTERN = re.compile(
    r"(IMAP_(HOST|PORT|USERNAME|PASSWORD|NAME)|IMAP[2-9]_(HOST|PORT|USERNAME|PASSWORD|NAME|FOLDER)"
    r"|AI_BASE_URL|AI_API_KEY|CALDAV_(URL|USERNAME|APP_PASSWORD|NAME|AUTH)|APP_URL|NTFY_(URL|TOKEN)"
    r"|PUSHOVER_(TOKEN|USER)|HOMEASSISTANT_(URL|TOKEN|SERVICE)"
    r"|SMTP_(HOST|PORT|SECURITY|USERNAME|PASSWORD|FROM)|NOTIFY_EMAIL_TO"
    # Einstellungen, die die App in ihre Datenbank übernommen hat (src/settings_store.py)
    r"|APP_LANGUAGE|IMAP_(POLL_SECONDS|IDLE_POLL_SECONDS|FOLDER|INITIAL_LOOKBACK_DAYS|MAX_MESSAGE_BYTES)"
    r"|AI_(MODEL|TIMEOUT_SECONDS)|UNCERTAIN_THRESHOLD_PERCENT|NEWSLETTER_MIN_CONFIDENCE_PERCENT"
    r"|DEFAULT_EVENT_MINUTES|REMINDER_MINUTES|ALLDAY_REMINDER_HOUR|CALDAV_(TIMEZONE|TIMEOUT_SECONDS)"
    r"|CALENDAR_(BACKUP_MINUTES|KEEP_DAYS)|BACKUP_(HOUR|KEEP_DAYS)"
    # Schalter, die nur den eingebauten Standard wiederholen
    r"|APP_DATABASE_PATH|LOG_LEVEL|DEMO_MODE|UPDATE_CHECK|CONNECTIONS_EDIT_WITHOUT_PASSWORD|APP_PASSWORD|TARGET_CALENDARS)"
)
MIGRATED_MARKER = "KALENDER_VERBINDUNGEN"  # =app: Zugangsdaten liegen verschlüsselt in der App
# Ältere Versionen lesen Zugangsdaten nur aus .env – nach dem Aufräumen liefen
# sie ohne Postfach, KI und Kalender.
MIN_VERSION_AFTER_MIGRATION = (0, 28, 0)
KEEP_STATE = ("connections_v1", "app_password_hash", "connections_without_password")  # überdauert eine Datenbank-Wiederherstellung


def _carry_over_state(current: Path, restored: Path) -> None:
    """Gespeicherte Verbindungen aus der aktuellen Datenbank in die Sicherung übernehmen."""
    if not current.is_file():
        return
    marks = ",".join("?" * len(KEEP_STATE))
    try:
        with closing(sqlite3.connect(current, timeout=30)) as source:
            rows = source.execute(f"SELECT key, value FROM service_state WHERE key IN ({marks})", KEEP_STATE).fetchall()
    except sqlite3.Error:
        return
    if not rows:
        return
    with closing(sqlite3.connect(restored, timeout=30)) as target:
        target.execute("CREATE TABLE IF NOT EXISTS service_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        target.executemany("INSERT INTO service_state(key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", rows)
        target.commit()


def _has_state(database: Path, key: str) -> bool:
    if not database.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)) as connection:
            return connection.execute("SELECT 1 FROM service_state WHERE key = ?", (key,)).fetchone() is not None
    except sqlite3.Error:
        return False


# ---------- Version in compose.yaml ----------
# Die festgelegte Version steht als Vorgabe in der Image-Zeile:
#   image: ghcr.io/norcon/kalender-assistent:${KALENDER_VERSION:-0.31.0}
# Ein KALENDER_VERSION in .env (ältere Updater) hat weiterhin Vorrang.
COMPOSE_IMAGE = re.compile(
    r"^(?P<head>[ \t]*image:[ \t]*\S*/kalender-assistent:)"
    r"(?:\$\{KALENDER_VERSION:-(?P<default>[^}\s]*)\}|(?P<tag>[^\s$]+))[ \t]*$",
    re.M,
)


# env_file-Eintrag: alte compose.yaml verlangen .env, neue nicht (Compose ≥ 2.24)
LEGACY_ENV_FILE = re.compile(r"^(?P<i>[ \t]*)env_file:[ \t]*\n(?P<j>[ \t]*)- \.env[ \t]*$", re.M)
OPTIONAL_ENV_FILE = re.compile(r"-[ \t]*path:[ \t]*\.env[ \t]*\n[ \t]*required:[ \t]*false\b")


def _write_like(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".updater-tmp")
    temporary.write_text(text, encoding="utf-8")
    stat = path.stat()
    os.chmod(temporary, stat.st_mode & 0o7777)
    try:
        os.chown(temporary, stat.st_uid, stat.st_gid)
    except PermissionError:
        pass
    temporary.replace(path)


def pinned_version(project_dir: Path) -> str | None:
    from_env = read_env_value(project_dir / ".env", ENV_KEY)
    if from_env:
        return from_env
    compose = project_dir / "compose.yaml"
    match = COMPOSE_IMAGE.search(compose.read_text(encoding="utf-8")) if compose.is_file() else None
    if not match:
        return None
    value = match.group("default") if match.group("default") is not None else match.group("tag")
    return None if value in ("", "latest") else value


def pin_version(project_dir: Path, version: str | None) -> None:
    """Version in compose.yaml festlegen (None = latest); KALENDER_VERSION aus .env entfernen."""
    compose = project_dir / "compose.yaml"
    env_file = project_dir / ".env"
    text = compose.read_text(encoding="utf-8") if compose.is_file() else ""
    if not COMPOSE_IMAGE.search(text):
        # eigene compose-Datei ohne erkennbare Image-Zeile: wie bisher über .env
        set_env_value(env_file, ENV_KEY, version)
        return
    replacement = "${KALENDER_VERSION:-" + (version or "latest") + "}"
    updated = COMPOSE_IMAGE.sub(lambda match: match.group("head") + replacement, text, count=1)
    if updated != text:
        _write_like(compose, updated)
    if _env_line_value(env_file, ENV_KEY) is not None:
        set_env_value(env_file, ENV_KEY, None)


# ---------- Ablauf ----------

class Updater:
    def __init__(
        self,
        project_dir: Path,
        *,
        service: str = "kalender-assistent",
        app_url: str = "http://kalender-assistent:8000",
        data_file: str = "data/calendar-assistant.sqlite3",
        backup_dir: str = "backups",
        health_timeout: float = 240,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        fetch: Callable[[str], str] | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.service = service
        self.app_url = app_url.rstrip("/")
        self.env_file = project_dir / ".env"
        self.data_file = project_dir / data_file
        self.backup_dir = project_dir / backup_dir
        self.health_timeout = health_timeout
        self._run = run
        self._sleep = sleep
        self._fetch = fetch or self._http_get
        self._lock = threading.Lock()
        self.state: dict[str, Any] = self._load_state()

    # Zustand liegt in backups/, damit die App ihn auch nach ihrem Neustart lesen kann
    @property
    def _state_file(self) -> Path:
        return self.backup_dir / "update-status.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
            if state.get("state") == "running":  # Updater wurde mittendrin beendet
                state["state"] = "failed"
                state["message"] = "Der Updater wurde während des Vorgangs neu gestartet."
            return state
        except (OSError, ValueError):
            return {"state": "idle", "log": []}

    def _save(self) -> None:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._state_file.with_name("update-status.json.tmp")
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(self._state_file)
        except OSError as error:
            logger.warning("Status konnte nicht gespeichert werden: %s", error)

    def _step(self, text: str) -> None:
        logger.info(text)
        self.state["step"] = text
        self.state.setdefault("log", []).append(f"{_now()} {text}")
        self._save()

    @staticmethod
    def _http_get(url: str) -> str:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 – nur internes Netz
            return response.read().decode("utf-8")

    def _compose(self, *arguments: str) -> None:
        result = self._run(
            ["docker", "compose", *arguments],
            cwd=self.project_dir, capture_output=True, text=True, timeout=900,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError(f"docker compose {' '.join(arguments)} fehlgeschlagen: {' '.join(detail)}")

    def _wait_for_version(self, version: str) -> bool:
        deadline = time.monotonic() + self.health_timeout
        while time.monotonic() < deadline:
            try:
                running = json.loads(self._fetch(f"{self.app_url}/api/version")).get("version")
                if running == version:
                    self._fetch(f"{self.app_url}/health")
                    return True
            except Exception:  # noqa: BLE001 – App startet noch
                pass
            self._sleep(5)
        return False

    def _restore_database(self, backup_name: str) -> None:
        source = self.backup_dir / backup_name
        if not BACKUP_NAME_PATTERN.match(backup_name) or not source.is_file():
            raise RuntimeError(f"Sicherung {backup_name} nicht gefunden")
        owner = self.data_file.parent.stat()
        temporary = self.data_file.with_name(self.data_file.name + ".restore-tmp")
        shutil.copy2(source, temporary)
        _carry_over_state(self.data_file, temporary)
        for suffix in ("-wal", "-shm"):
            Path(str(self.data_file) + suffix).unlink(missing_ok=True)
        temporary.replace(self.data_file)
        try:
            os.chown(self.data_file, owner.st_uid, owner.st_gid)
        except PermissionError:
            pass

    def migrated(self) -> bool:
        """Liegen die Zugangsdaten schon in der App (ab 0.28.0)?"""
        return read_env_value(self.env_file, MIGRATED_MARKER) == "app" or _has_state(self.data_file, KEEP_STATE[0])

    def _save_config(self, stamp: str, prefix: str = "vor-update") -> Path:
        target = self.backup_dir / f"{prefix}-{stamp}"
        target.mkdir(parents=True, exist_ok=True)
        for name in (".env", "compose.yaml"):
            source = self.project_dir / name
            if source.exists():
                shutil.copy2(source, target / name)
                os.chmod(target / name, 0o600)
        return target

    def start(self, request: dict[str, Any]) -> tuple[bool, str]:
        version = str(request.get("version") or "")
        previous = str(request.get("previous") or "")
        backup = request.get("backup")
        restore = request.get("restore_backup")
        if not VERSION_PATTERN.match(version):
            return False, "Ungültige Versionsnummer."
        if previous and not VERSION_PATTERN.match(previous):
            return False, "Ungültige bisherige Versionsnummer."
        if (self.migrated()
                and tuple(int(part) for part in version.split(".")) < MIN_VERSION_AFTER_MIGRATION):
            return False, ("Versionen vor 0.28.0 lesen die Zugangsdaten nur aus .env; "
                           "die liegen inzwischen verschlüsselt in der App.")
        for name in (backup, restore):
            if name is not None and not BACKUP_NAME_PATTERN.match(str(name)):
                return False, "Ungültiger Sicherungsname."
        if not self._lock.acquire(blocking=False):
            return False, "Es läuft bereits ein Update."
        self.state = {
            "state": "running", "from": previous or None, "to": version,
            "backup": backup, "restored": restore, "started": _now(), "log": [],
        }
        self._save()
        threading.Thread(target=self._run_update, args=(version, previous, backup, restore), daemon=True).start()
        return True, "gestartet"

    def _run_update(self, version: str, previous: str, backup: str | None, restore: str | None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        old_value = pinned_version(self.project_dir)
        self._touched = False  # wurde die laufende App schon angefasst?
        try:
            saved = self._save_config(stamp)
            self._step(f"Konfiguration gesichert in {saved.name}.")
            pin_version(self.project_dir, version)
            self._step(f"Hole Version {version} …")
            self._compose("pull", self.service)
            self._touched = True
            if restore:
                self._step(f"Setze Datenbank auf {restore} zurück …")
                self._compose("stop", self.service)
                self._restore_database(restore)
            self._step(f"Starte Version {version} …")
            self._compose("up", "-d", self.service)
            self._step("Warte, bis die App bereit ist …")
            if not self._wait_for_version(version):
                raise RuntimeError(f"Version {version} ist nicht rechtzeitig sauber gestartet.")
            self.state.update(state="done", finished=_now(), message=f"Version {version} läuft.")
            self._step(f"Fertig: Version {version} läuft.")
        except Exception as error:  # noqa: BLE001
            logger.error("Update fehlgeschlagen: %s", error)
            self._step(f"Fehler: {error}")
            self._roll_back(old_value, previous, backup, str(error))
        finally:
            self._save()
            self._lock.release()

    def _roll_back(self, old_value: str | None, previous: str, backup: str | None, reason: str) -> None:
        try:
            # Ausdrücklich die bisherige Version eintragen – „latest“ könnte die
            # gerade fehlgeschlagene neue Version sein.
            pin_version(self.project_dir, previous or old_value)
            if not getattr(self, "_touched", False):
                self.state.update(state="failed", finished=_now(),
                                  message=f"Update nicht möglich ({reason}). An der laufenden App wurde nichts geändert.")
                self._step("Abgebrochen, bevor die App angefasst wurde.")
                return
            self._step("Kehre zur bisherigen Version zurück …")
            self._compose("stop", self.service)
            if backup:
                self._step(f"Stelle die Datenbank aus {backup} wieder her …")
                self._restore_database(backup)
            self._compose("up", "-d", self.service)
            if previous and not self._wait_for_version(previous):
                raise RuntimeError("Die bisherige Version meldet sich nicht zurück.")
            self.state.update(state="rolled_back", finished=_now(),
                              message=f"Update nicht möglich ({reason}). Die bisherige Version läuft wieder.")
            self._step("Bisherige Version läuft wieder.")
        except Exception as error:  # noqa: BLE001
            self.state.update(state="failed", finished=_now(),
                              message=f"Update und Rückkehr fehlgeschlagen: {error}. Bitte von Hand prüfen.")
            self._step(f"Rückkehr fehlgeschlagen: {error}")


# ---------- Ersteinrichtung ----------

def configured(env_file: Path, data_file: Path | None = None) -> bool:
    if read_env_value(env_file, MIGRATED_MARKER) == "app":
        return True
    if data_file is not None and _has_state(data_file, KEEP_STATE[0]):
        return True
    return all(read_env_value(env_file, key) for key in CREDENTIAL_KEYS)


def cleanup_env(updater: "Updater", keys: dict[str, Any]) -> tuple[bool, str, list[str]]:
    """Von der App übernommene Werte aus .env entfernen.

    Entfernt wird ein Schlüssel nur, wenn er erlaubt ist und sein Wert genau der
    übergebenen Prüfsumme entspricht (also das ist, was die App gespeichert hat).
    """
    if not isinstance(keys, dict) or not keys:
        return False, "Keine Schlüssel übergeben.", []
    if any(not isinstance(key, str) or not CLEANUP_KEY_PATTERN.fullmatch(key) for key in keys):
        return False, "Nicht erlaubte Schlüssel.", []
    removable = []
    for key, digest in keys.items():
        value = _env_line_value(updater.env_file, key)
        if value is not None and isinstance(digest, str) and hashlib.sha256(value.encode("utf-8")).hexdigest() == digest:
            removable.append(key)
    if not removable:
        return True, "Nichts zu entfernen.", []
    if not updater._lock.acquire(blocking=False):
        return False, "Es läuft bereits ein Vorgang.", []
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        saved = updater._save_config(stamp, prefix="env-vor-aufraeumen")
        set_env_values(updater.env_file, {key: None for key in removable})
        logger.info("Aus .env entfernt (gilt jetzt aus der App): %s; Sicherung in %s.",
                    ", ".join(removable), saved.name)
        tidy_env(updater, backup=False)
    finally:
        updater._lock.release()
    return True, "aufgeräumt", removable


def tidy_env(updater: "Updater", *, backup: bool = True) -> list[str]:
    """.env überflüssig machen: Version nach compose.yaml, Markierung weg, leere Datei löschen.

    Läuft beim Start des Updaters und nach dem Aufräumen. Gibt die Schritte zurück.
    """
    env_file = updater.env_file
    if not env_file.exists():
        return []
    steps: list[str] = []
    saved = False

    def save_once() -> None:
        nonlocal saved
        if backup and not saved:
            updater._save_config(datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S"), prefix="env-vor-aufraeumen")
            saved = True

    if _env_line_value(env_file, ENV_KEY) is not None and COMPOSE_IMAGE.search(
            (updater.project_dir / "compose.yaml").read_text(encoding="utf-8")
            if (updater.project_dir / "compose.yaml").is_file() else ""):
        save_once()
        pin_version(updater.project_dir, read_env_value(env_file, ENV_KEY))
        steps.append("Version nach compose.yaml übernommen")
    if _env_line_value(env_file, MIGRATED_MARKER) is not None and _has_state(updater.data_file, KEEP_STATE[0]):
        save_once()
        set_env_value(env_file, MIGRATED_MARKER, None)
        steps.append(f"{MIGRATED_MARKER} entfernt")
    lines = env_file.read_text(encoding="utf-8").splitlines()
    compose = updater.project_dir / "compose.yaml"
    if all(not line.strip() or line.strip().startswith("#") for line in lines) and compose.is_file():
        text = compose.read_text(encoding="utf-8")
        if LEGACY_ENV_FILE.search(text):
            save_once()
            text = LEGACY_ENV_FILE.sub(
                lambda m: f"{m['i']}env_file:\n{m['j']}- path: .env\n{m['j']}  required: false", text, count=1)
            _write_like(compose, text)
            steps.append("compose.yaml: .env ist jetzt optional")
        if OPTIONAL_ENV_FILE.search(text):
            save_once()
            env_file.unlink()
        steps.append(".env gelöscht (nichts mehr darin)")
    if steps:
        logger.info(".env aufgeräumt: %s.", "; ".join(steps))
    return steps


def configure(updater: "Updater", values: dict[str, Any]) -> tuple[bool, str]:
    """Schreibt die Werte der Ersteinrichtung – nur solange noch keine Zugangsdaten gesetzt sind."""
    if configured(updater.env_file, updater.data_file):
        return False, "Die Einrichtung ist bereits abgeschlossen; Zugangsdaten änderst du unter Einstellungen → Verbindungen."
    if not isinstance(values, dict) or not values:
        return False, "Keine Werte übergeben."
    unknown = set(values) - SETUP_KEYS
    if unknown:
        return False, "Nicht erlaubte Schlüssel: " + ", ".join(sorted(unknown))
    cleaned: dict[str, str | None] = {}
    for key, value in values.items():
        text = "" if value is None else str(value)
        if len(text) > 500 or any(ord(character) < 32 for character in text):
            return False, f"Ungültiger Wert für {key}."
        cleaned[key] = text or None
    if not all(cleaned.get(key) for key in CREDENTIAL_KEYS):
        return False, "Postfach-Server, Benutzer und Passwort sind Pflicht."
    if not updater._lock.acquire(blocking=False):
        return False, "Es läuft bereits ein Vorgang."
    try:
        updater.state = {"state": "running", "kind": "configure", "started": _now(), "log": []}
        updater._save()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        updater._save_config(stamp)
        set_env_values(updater.env_file, cleaned)
        updater._step("Einstellungen in .env geschrieben.")
    except Exception as error:  # noqa: BLE001
        updater.state.update(state="failed", finished=_now(), message=str(error))
        updater._save()
        updater._lock.release()
        return False, str(error)

    def restart() -> None:
        try:
            updater._step("Starte die App mit den neuen Einstellungen …")
            updater._compose("up", "-d", updater.service)
            updater.state.update(state="done", finished=_now(), message="Einrichtung abgeschlossen.")
            updater._step("Fertig.")
        except Exception as error:  # noqa: BLE001
            updater.state.update(state="failed", finished=_now(), message=str(error))
            updater._step(f"Fehler: {error}")
        finally:
            updater._save()
            updater._lock.release()

    threading.Thread(target=restart, daemon=True).start()
    return True, "gespeichert"


# ---------- HTTP ----------

def make_handler(updater: Updater) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/status":
                self._send(200, {**updater.state, "configured": configured(updater.env_file, updater.data_file)})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in ("/update", "/configure", "/env/cleanup"):
                self._send(404, {"error": "not found"})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 16384)
                request = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"error": "Ungültige Anfrage."})
                return
            request = request if isinstance(request, dict) else {}
            if self.path == "/env/cleanup":
                ok, message, removed = cleanup_env(updater, request.get("keys") or {})
                self._send(200 if ok else 409, {"ok": ok, "message": message, "removed": removed})
                return
            if self.path == "/configure":
                ok, message = configure(updater, request.get("values") or {})
            else:
                ok, message = updater.start(request)
            self._send(202 if ok else 409, {"ok": ok, "message": message})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug(format, *args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    project = Path(os.environ.get("PROJECT_DIR") or os.getcwd())
    updater = Updater(
        project,
        service=os.environ.get("APP_SERVICE", "kalender-assistent"),
        app_url=os.environ.get("APP_URL", "http://kalender-assistent:8000"),
        health_timeout=float(os.environ.get("HEALTH_TIMEOUT", "240")),
    )
    port = int(os.environ.get("UPDATER_PORT", "8000"))
    try:
        tidy_env(updater)
    except Exception as error:  # noqa: BLE001 – Aufräumen ist nur Kür
        logger.warning(".env konnte nicht aufgeräumt werden: %s", error)
    logger.info("Updater bereit für %s (Projekt %s).", updater.service, project)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(updater)).serve_forever()


if __name__ == "__main__":
    main()

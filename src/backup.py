"""Tägliche Sicherung der SQLite-Datenbank.

Verwendet die Online-Backup-API von SQLite: Die Kopie ist konsistent, auch wenn
die Anwendung währenddessen schreibt (WAL-Modus).

Wiederherstellen: Container stoppen, gewünschte Sicherung nach
./data/calendar-assistant.sqlite3 kopieren (die Dateien -wal und -shm daneben
löschen), Container starten.
"""

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Sicherungen nacheinander: Zwei gleichzeitige (z. B. automatisch und „Jetzt
# sichern“ in derselben Sekunde) würden sonst dieselbe Zwischendatei schreiben.
_backup_lock = threading.Lock()

_NAME_PATTERN = re.compile(
    r"^calendar-assistant-(\d{4}-\d{2}-\d{2})_(\d{6})(?:-(\d+))?\.sqlite3$"
)


def _backup_time(path: Path) -> datetime | None:
    match = _NAME_PATTERN.match(path.name)
    if not match:
        return None
    return datetime.strptime(
        f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H%M%S"
    ).replace(tzinfo=timezone.utc)


def list_backups(backup_dir: str) -> list[dict[str, Any]]:
    directory = Path(backup_dir)
    if not directory.is_dir():
        return []
    backups = []
    for path in directory.iterdir():
        created = _backup_time(path)
        if created is None or not path.is_file():
            continue
        backups.append(
            {
                "name": path.name,
                "created_at": created,
                "size_bytes": path.stat().st_size,
            }
        )
    return sorted(
        backups,
        key=lambda item: (item["created_at"], _sequence(item["name"])),
        reverse=True,
    )


def _sequence(name: str) -> int:
    match = _NAME_PATTERN.match(name)
    return int(match.group(3)) if match and match.group(3) else 0


def create_backup(*args: Any, **kwargs: Any) -> Path:
    with _backup_lock:
        return _create_backup(*args, **kwargs)


def _create_backup(
    database_path: str,
    backup_dir: str,
    keep_days: int,
    now: datetime | None = None,
    *,
    prune: bool = True,
) -> Path:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    base = f"calendar-assistant-{now:%Y-%m-%d_%H%M%S}"
    target = directory / f"{base}.sqlite3"
    sequence = 1
    while target.exists():  # zwei Sicherungen in derselben Sekunde
        target = directory / f"{base}-{sequence}.sqlite3"
        sequence += 1
    temporary = target.with_name(target.name + ".tmp")

    source = sqlite3.connect(database_path, timeout=30)
    try:
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    temporary.replace(target)
    logger.info("Datenbanksicherung erstellt: %s.", target.name)

    if not prune:
        return target
    cutoff = now - timedelta(days=keep_days)
    for item in list_backups(backup_dir)[1:]:  # neueste Sicherung immer behalten
        if item["created_at"] < cutoff:
            (directory / item["name"]).unlink(missing_ok=True)
            logger.info("Alte Datenbanksicherung gelöscht: %s.", item["name"])
    return target


def backup_due(
    backup_dir: str, backup_hour: int, local_now: datetime
) -> bool:
    """Fällig ab der eingestellten Stunde, wenn es heute (lokal) noch keine gibt."""
    if local_now.hour < backup_hour:
        return False
    local_zone = local_now.tzinfo
    return not any(
        item["created_at"].astimezone(local_zone).date() == local_now.date()
        for item in list_backups(backup_dir)
    )


def backup_path(backup_dir: str, name: str) -> Path:
    """Liefert den Pfad einer vorhandenen Sicherung; verhindert Pfad-Tricks."""
    if not _NAME_PATTERN.match(name):
        raise FileNotFoundError(name)
    path = Path(backup_dir) / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete_backup(backup_dir: str, name: str) -> None:
    backup_path(backup_dir, name).unlink()
    logger.info("Datenbanksicherung gelöscht: %s.", name)


def restore_backup(database_path: str, backup_dir: str, name: str) -> None:
    """Spielt eine Sicherung in die laufende Datenbank zurück.

    Die SQLite-Backup-API ersetzt den Inhalt in einem Schritt. Der Zähler für
    Änderungen wird danach erhöht, damit geöffnete Seiten neu laden.
    """
    source_path = backup_path(backup_dir, name)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
    try:
        check = source.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"Sicherung {name} ist beschädigt ({check}).")
        destination = sqlite3.connect(database_path, timeout=30)
        try:
            previous_version = destination.execute(
                "SELECT version FROM suggestion_meta WHERE singleton = 1"
            ).fetchone()[0]
            source.backup(destination)
            destination.execute(
                "UPDATE suggestion_meta SET version = MAX(version, ?) + 1 "
                "WHERE singleton = 1",
                (previous_version,),
            )
            destination.commit()
        finally:
            destination.close()
    finally:
        source.close()
    logger.info("Datenbank aus Sicherung %s wiederhergestellt.", name)

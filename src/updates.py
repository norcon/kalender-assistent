"""Update auf Knopfdruck: verfügbare Versionen (GitHub) und Updater-Dienst."""

import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VERSION_PATTERN = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
_CACHE_SECONDS = 3600
_FAILURE_CACHE_SECONDS = 300  # Fehlschlag nicht lange merken (z. B. Repository gerade öffentlich geworden)
_cache: dict[str, tuple[float, list[dict[str, Any]] | None]] = {}


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = VERSION_PATTERN.match((value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def releases(repository: str, *, refresh: bool = False) -> list[dict[str, Any]] | None:
    """Veröffentlichte Versionen, neueste zuerst; None, wenn GitHub nicht erreichbar ist."""
    cached = _cache.get(repository)
    if cached and not refresh:
        limit = _CACHE_SECONDS if cached[1] is not None else _FAILURE_CACHE_SECONDS
        if time.monotonic() - cached[0] < limit:
            return cached[1]
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{repository}/releases",
            params={"per_page": 30},
            headers={"Accept": "application/vnd.github+json", "User-Agent": "kalender-assistent-update-check"},
            timeout=10,
        )
        response.raise_for_status()
        items = []
        for release in response.json():
            version = parse_version(str(release.get("tag_name") or ""))
            if not version or release.get("draft") or release.get("prerelease"):
                continue
            items.append({
                "version": ".".join(map(str, version)),
                "key": version,
                "published": release.get("published_at"),
                "notes": (release.get("body") or "").strip()[:4000],
            })
        items.sort(key=lambda item: item["key"], reverse=True)
        result: list[dict[str, Any]] | None = items
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Verfügbare Versionen konnten nicht abgefragt werden (%s).", type(error).__name__)
        result = None
    _cache[repository] = (time.monotonic(), result)
    return result


def updater_status(url: str) -> dict[str, Any] | None:
    """Zustand des Updater-Dienstes; None, wenn er nicht eingerichtet ist."""
    try:
        response = httpx.get(f"{url.rstrip('/')}/status", timeout=3)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def start_update(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        response = httpx.post(f"{url.rstrip('/')}/update", json=payload, timeout=10)
        data = response.json()
        return response.status_code == 202, str(data.get("message") or "")
    except (httpx.HTTPError, ValueError) as error:
        return False, type(error).__name__


def backup_calendar_store(store_path: str, backup_dir: str, keep: int = 3) -> Path | None:
    """Kopie der Kalender-Sicherung vor einem Update (die letzten `keep` bleiben)."""
    source = Path(store_path)
    if not source.exists():
        return None
    directory = Path(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    target = directory / f"calendar-history-vor-update-{stamp}.sqlite3"
    with closing(sqlite3.connect(source, timeout=30)) as origin, closing(sqlite3.connect(target)) as copy:
        origin.backup(copy)
    old = sorted(directory.glob("calendar-history-vor-update-*.sqlite3"))[:-keep]
    for path in old:
        path.unlink(missing_ok=True)
    return target

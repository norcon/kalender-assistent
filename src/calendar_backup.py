"""Versioniertes Backup der iCloud-Kalender.

Speichert jede Termin-Version genau einmal und hält fest, in welchem Zeitraum
(von Sicherung A bis vor Sicherung B) sie im Kalender stand. Dadurch kostet eine
stündliche Sicherung nur so viel Platz, wie sich tatsächlich geändert hat, und
der Stand zu jeder Sicherung lässt sich trotzdem exakt rekonstruieren.

Ablauf einer Sicherung pro Kalender:
1. PROPFIND: alle Termin-Pfade mit ETag (klein, nur Metadaten)
2. Vergleich mit dem zuletzt bekannten Stand → neu / geändert / gelöscht
3. calendar-multiget nur für neue und geänderte Termine
"""

import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import formatting, recurrence

logger = logging.getLogger(__name__)

# Erhöhen, wenn parse_event neue Felder liefert; ältere Einträge werden dann
# einmalig nachträglich ausgewertet (siehe backfill).
PARSER_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    name TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    status TEXT NOT NULL,
    calendars TEXT NOT NULL DEFAULT '[]',
    added INTEGER NOT NULL DEFAULT 0,
    changed INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS event_versions (
    hash TEXT PRIMARY KEY,
    ics TEXT NOT NULL,
    uid TEXT,
    summary TEXT,
    dtstart TEXT,
    location TEXT,
    recurring INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS presence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar TEXT NOT NULL,
    href TEXT NOT NULL,
    etag TEXT NOT NULL,
    version_hash TEXT NOT NULL REFERENCES event_versions(hash),
    valid_from INTEGER NOT NULL,
    valid_to INTEGER
);
CREATE INDEX IF NOT EXISTS presence_open ON presence(calendar, href) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS presence_range ON presence(valid_from, valid_to);
"""


_MIGRATIONS = {
    "event_versions": (
        ("dtend", "TEXT"),
        ("rrule", "TEXT"),
        ("exdates", "TEXT"),
        ("parser_version", "INTEGER NOT NULL DEFAULT 1"),
    ),
    "calendars": (("color", "TEXT"),),
}


def connect(store_path: str) -> sqlite3.Connection:
    Path(store_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    for table, columns in _MIGRATIONS.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns:
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return connection


# --- iCalendar-Auswertung (nur was für Anzeige und Suche gebraucht wird) ---


def _unfold(ics: str) -> list[str]:
    lines: list[str] = []
    for raw in ics.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n").replace("\\N", "\n")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def _ical_datetime(value: str, params: dict[str, str]) -> str:
    value = value.strip()
    try:
        if params.get("VALUE") == "DATE" or len(value) == 8:
            return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()
        if value.endswith("Z"):
            parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        parsed = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        if "TZID" in params:
            try:
                return parsed.replace(tzinfo=ZoneInfo(params["TZID"])).isoformat()
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return parsed.strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return value


def parse_event(ics: str) -> dict[str, Any]:
    """Liest UID, Titel, Beginn, Ort aus dem Haupt-VEVENT (nicht aus Ausnahmen)."""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _unfold(ics):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *raw_params = head.split(";")
        params = dict(
            param.split("=", 1) for param in raw_params if "=" in param
        )
        name = name.upper()
        if name in ("UID", "SUMMARY", "LOCATION"):
            current[name.lower()] = _unescape(value)
        elif name == "DTSTART":
            current["dtstart"] = _ical_datetime(value, params)
        elif name == "DTEND":
            current["dtend"] = _ical_datetime(value, params)
        elif name == "DURATION":
            current["duration"] = value.strip()
        elif name == "RRULE":
            current["recurring"] = True
            current["rrule"] = value.strip()
        elif name == "EXDATE":
            current.setdefault("exdates", []).extend(
                _ical_datetime(part, params)[:10] for part in value.split(",") if part.strip()
            )
        elif name == "RECURRENCE-ID":
            current["exception"] = True
    master = next((event for event in events if not event.get("exception")), None)
    master = master or (events[0] if events else {})
    dtend = master.get("dtend")
    if not dtend and master.get("dtstart") and master.get("duration"):
        dtend = _add_duration(master["dtstart"], master["duration"])
    return {
        "uid": master.get("uid"),
        "summary": master.get("summary") or "(ohne Titel)",
        "dtstart": master.get("dtstart"),
        "dtend": dtend,
        "location": master.get("location"),
        "recurring": bool(master.get("recurring")),
        "rrule": master.get("rrule"),
        "exdates": sorted(set(master.get("exdates") or [])),
    }


_DURATION = re.compile(r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _add_duration(start: str, duration: str) -> str | None:
    match = _DURATION.match(duration.lstrip("+"))
    if not match:
        return None
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    delta = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    if len(start) == 10:
        return (date.fromisoformat(start) + timedelta(days=delta.days or 1)).isoformat()
    try:
        parsed = datetime.fromisoformat(start)
    except ValueError:
        return None
    result = parsed + delta
    return result.isoformat() if parsed.tzinfo else result.strftime("%Y-%m-%dT%H:%M")


# --- Sicherung ---


def take_snapshot(
    store_path: str,
    calendars: list[dict[str, str]],
    list_etags: Callable[[str], dict[str, str]],
    fetch: Callable[[str, list[str]], dict[str, tuple[str, str]]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sichert die übergebenen Kalender ({name, url}); speichert nur Änderungen.

    Schlägt ein Kalender fehl, bleibt sein letzter Stand unverändert (es wird
    nichts als gelöscht markiert) und die Sicherung gilt als unvollständig.
    """
    now = now or datetime.now(timezone.utc)
    summary = {"added": 0, "changed": 0, "deleted": 0, "total": 0, "errors": []}
    with closing(connect(store_path)) as connection, connection:
        snapshot_id = connection.execute(
            "INSERT INTO snapshots(taken_at, status, calendars) VALUES (?, 'running', ?)",
            (now.isoformat(), json.dumps([c["name"] for c in calendars], ensure_ascii=False)),
        ).lastrowid
        for calendar in calendars:
            name, url = calendar["name"], calendar["url"]
            connection.execute(
                "INSERT INTO calendars(name, url, last_seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET url = excluded.url, "
                "last_seen_at = excluded.last_seen_at",
                (name, url, now.isoformat()),
            )
            try:
                etags = list_etags(url)
                open_rows = {
                    row["href"]: row
                    for row in connection.execute(
                        "SELECT id, href, etag FROM presence "
                        "WHERE calendar = ? AND valid_to IS NULL",
                        (name,),
                    )
                }
                to_fetch = [
                    href for href, etag in etags.items()
                    if href not in open_rows or open_rows[href]["etag"] != etag
                ]
                fetched = fetch(url, to_fetch) if to_fetch else {}
            except Exception as error:  # Kalender überspringen, Rest weiter sichern
                logger.warning(
                    "Kalender „%s“ konnte nicht gesichert werden: %s", name, error
                )
                summary["errors"].append(f"{name}: {type(error).__name__}")
                continue

            gone = [href for href in open_rows if href not in etags]
            # Zwischen PROPFIND und REPORT gelöschte Termine ebenfalls als gelöscht.
            gone += [
                href for href in to_fetch if href not in fetched and href in open_rows
            ]
            for href in gone:
                connection.execute(
                    "UPDATE presence SET valid_to = ? WHERE id = ?",
                    (snapshot_id, open_rows[href]["id"]),
                )
            summary["deleted"] += len(gone)

            for href, (etag, ics) in fetched.items():
                version_hash = sha256(ics.encode("utf-8")).hexdigest()
                info = parse_event(ics)
                connection.execute(
                    "INSERT OR IGNORE INTO event_versions "
                    "(hash, ics, uid, summary, dtstart, location, recurring, "
                    "dtend, rrule, exdates, parser_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        version_hash, ics, info["uid"], info["summary"],
                        info["dtstart"], info["location"], int(info["recurring"]),
                        info["dtend"], info["rrule"], json.dumps(info["exdates"]),
                        PARSER_VERSION,
                    ),
                )
                if href in open_rows:
                    connection.execute(
                        "UPDATE presence SET valid_to = ? WHERE id = ?",
                        (snapshot_id, open_rows[href]["id"]),
                    )
                    summary["changed"] += 1
                else:
                    summary["added"] += 1
                connection.execute(
                    "INSERT INTO presence "
                    "(calendar, href, etag, version_hash, valid_from) VALUES (?, ?, ?, ?, ?)",
                    (name, href, etag, version_hash, snapshot_id),
                )
            summary["total"] += len(etags) - len(
                [href for href in to_fetch if href not in fetched]
            )

        connection.execute(
            "UPDATE snapshots SET status = ?, added = ?, changed = ?, deleted = ?, "
            "total = ?, error = ? WHERE id = ?",
            (
                "partial" if summary["errors"] else "ok",
                summary["added"], summary["changed"], summary["deleted"],
                summary["total"], "; ".join(summary["errors"]) or None, snapshot_id,
            ),
        )
    summary["snapshot_id"] = snapshot_id
    logger.info(
        "Kalendersicherung %s: %d Termine, %d neu, %d geändert, %d gelöscht%s.",
        snapshot_id, summary["total"], summary["added"], summary["changed"],
        summary["deleted"],
        f" (Fehler: {', '.join(summary['errors'])})" if summary["errors"] else "",
    )
    return summary


def prune(store_path: str, keep_days: int, now: datetime | None = None) -> int:
    """Löscht Sicherungen älter als keep_days und nicht mehr benötigte Versionen."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=keep_days)).isoformat()
    with closing(connect(store_path)) as connection, connection:
        oldest_kept = connection.execute(
            "SELECT MIN(id) FROM snapshots WHERE taken_at >= ?", (cutoff,)
        ).fetchone()[0]
        if oldest_kept is None:  # neueste Sicherung immer behalten
            oldest_kept = connection.execute("SELECT MAX(id) FROM snapshots").fetchone()[0]
        if oldest_kept is None:
            return 0
        removed = connection.execute(
            "DELETE FROM snapshots WHERE id < ?", (oldest_kept,)
        ).rowcount
        # Zeilen, die vor der ältesten behaltenen Sicherung endeten, sind unsichtbar.
        connection.execute(
            "DELETE FROM presence WHERE valid_to IS NOT NULL AND valid_to <= ?",
            (oldest_kept,),
        )
        connection.execute(
            "DELETE FROM event_versions WHERE hash NOT IN "
            "(SELECT DISTINCT version_hash FROM presence)"
        )
    if removed:
        logger.info("%d alte Kalendersicherung(en) entfernt.", removed)
    return removed


# --- Abfragen für Oberfläche und Wiederherstellung ---


def remember_calendars(store_path: str, calendars: list[dict[str, str]]) -> None:
    """Merkt alle gefundenen Kalender (für die Auswahl in der Oberfläche)."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect(store_path)) as connection, connection:
        connection.executemany(
            "INSERT INTO calendars(name, url, last_seen_at, color) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET url = excluded.url, "
            "last_seen_at = excluded.last_seen_at, "
            "color = COALESCE(excluded.color, calendars.color)",
            [(c["name"], c["url"], now, c.get("color")) for c in calendars],
        )


def list_snapshots(store_path: str, limit: int = 1000) -> list[dict[str, Any]]:
    if not Path(store_path).exists():
        return []
    with closing(connect(store_path)) as connection:
        rows = connection.execute(
            "SELECT * FROM snapshots WHERE status != 'running' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def known_calendars(store_path: str) -> list[str]:
    if not Path(store_path).exists():
        return []
    with closing(connect(store_path)) as connection:
        return [
            row["name"]
            for row in connection.execute("SELECT name FROM calendars ORDER BY name")
        ]


def storage_info(store_path: str) -> dict[str, int]:
    path = Path(store_path)
    if not path.exists():
        return {"bytes": 0, "versions": 0}
    with closing(connect(store_path)) as connection:
        versions = connection.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0]
    size = sum(
        candidate.stat().st_size
        for candidate in path.parent.glob(path.name + "*")
        if candidate.is_file()
    )
    return {"bytes": size, "versions": versions}


STATUS_SQL = (
    "CASE WHEN c.id IS NULL THEN 'gelöscht' "
    "WHEN c.version_hash = p.version_hash THEN 'unverändert' ELSE 'geändert' END"
)
PERIODS = ("kommend", "vergangen", "alle")
_FROM_SQL = """
    FROM presence p
    JOIN event_versions v ON v.hash = p.version_hash
    LEFT JOIN presence c
        ON c.calendar = p.calendar AND c.href = p.href AND c.valid_to IS NULL
    LEFT JOIN event_versions cv ON cv.hash = c.version_hash
"""
# Kommend: beginnt heute oder später – oder ist eine Serie, die noch läuft.
_UPCOMING_SQL = (
    "(substr(v.dtstart, 1, 10) >= :today OR (v.recurring = 1 AND ("
    "v.rrule IS NULL OR instr(v.rrule, 'UNTIL=') = 0 OR "
    "substr(v.rrule, instr(v.rrule, 'UNTIL=') + 6, 8) >= :today_compact)))"
)


def _conditions(
    snapshot_id: int, text: str, calendar: str, period: str, today: date | None
) -> tuple[list[str], dict[str, Any]]:
    conditions = [
        "p.valid_from <= :snapshot",
        "(p.valid_to IS NULL OR p.valid_to > :snapshot)",
    ]
    params: dict[str, Any] = {"snapshot": snapshot_id}
    if text.strip():
        conditions.append(
            "(v.summary LIKE :text ESCAPE '\\' OR v.location LIKE :text ESCAPE '\\')"
        )
        escaped = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["text"] = f"%{escaped}%"
    if calendar:
        conditions.append("p.calendar = :calendar")
        params["calendar"] = calendar
    if period in ("kommend", "vergangen"):
        today = today or date.today()
        params.update(today=today.isoformat(), today_compact=today.strftime("%Y%m%d"))
        conditions.append(_UPCOMING_SQL if period == "kommend" else f"NOT {_UPCOMING_SQL}")
    return conditions, params


def search(
    store_path: str,
    snapshot_id: int,
    *,
    text: str = "",
    calendar: str = "",
    only_changed: bool = False,
    status: str = "",
    period: str = "alle",
    today: date | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Termine im Stand einer Sicherung, verglichen mit dem neuesten Stand.

    status: unverändert / geändert / gelöscht (bezogen auf die neueste Sicherung).
    Reihenfolge: kommende Termine aufsteigend, danach vergangene absteigend.
    """
    conditions, params = _conditions(snapshot_id, text, calendar, period, today)
    params.update(limit=limit, offset=offset, today=params.get("today", (today or date.today()).isoformat()))
    params.setdefault("today_compact", (today or date.today()).strftime("%Y%m%d"))
    if only_changed:
        conditions.append("(c.id IS NULL OR c.version_hash != p.version_hash)")
    if status == "gelöscht":
        conditions.append("c.id IS NULL")
    elif status == "geändert":
        conditions.append("c.id IS NOT NULL AND c.version_hash != p.version_hash")
    query = f"""
        SELECT p.id, p.calendar, p.href, v.summary, v.dtstart, v.dtend, v.location, v.recurring,
               {STATUS_SQL} AS status, {_UPCOMING_SQL} AS upcoming,
               cv.summary AS current_summary, cv.dtstart AS current_dtstart,
               cv.dtend AS current_dtend, cv.location AS current_location
        {_FROM_SQL}
        WHERE {' AND '.join(conditions)}
        ORDER BY upcoming DESC,
                 -- laufende Serien (Beginn liegt zurück) nach den datierten Terminen
                 CASE WHEN v.recurring = 1 AND substr(v.dtstart, 1, 10) < :today THEN 1 ELSE 0 END,
                 CASE WHEN {_UPCOMING_SQL} THEN v.dtstart END ASC,
                 v.dtstart DESC, v.summary
        LIMIT :limit OFFSET :offset
    """
    with closing(connect(store_path)) as connection:
        return [dict(row) for row in connection.execute(query, params)]


def count_by_status(
    store_path: str,
    snapshot_id: int,
    *,
    text: str = "",
    calendar: str = "",
    period: str = "alle",
    today: date | None = None,
) -> dict[str, int]:
    """Anzahl je Status (gelöscht/geändert/unverändert) plus „alle“."""
    conditions, params = _conditions(snapshot_id, text, calendar, period, today)
    query = f"""
        SELECT {STATUS_SQL} AS status, COUNT(*) AS n
        {_FROM_SQL}
        WHERE {' AND '.join(conditions)}
        GROUP BY status
    """
    counts = {"gelöscht": 0, "geändert": 0, "unverändert": 0}
    with closing(connect(store_path)) as connection:
        for row in connection.execute(query, params):
            counts[row["status"]] = row["n"]
    counts["alle"] = sum(counts.values())
    return counts


def change_points(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sicherungen mit Änderungen (neueste zuerst) samt dem Stand direkt davor.

    Zwischen zwei Änderungen ist der Kalender gleich – diese Punkte decken
    also alle unterschiedlichen Stände ab. snapshots: wie list_snapshots.
    """
    points = []
    for index, item in enumerate(snapshots):
        if not (item["added"] or item["changed"] or item["deleted"]):
            continue
        before = snapshots[index + 1] if index + 1 < len(snapshots) else None
        points.append({**item, "before_id": before["id"] if before else None,
                       "before_taken_at": before["taken_at"] if before else None})
    return points


def get_version(store_path: str, presence_id: int) -> dict[str, Any] | None:
    with closing(connect(store_path)) as connection:
        row = connection.execute(
            "SELECT p.id, p.calendar, p.href, v.ics, v.summary, v.dtstart "
            "FROM presence p JOIN event_versions v ON v.hash = p.version_hash "
            "WHERE p.id = ?",
            (presence_id,),
        ).fetchone()
    return dict(row) if row else None


def current_events_near(
    store_path: str, iso_dates: set[str]
) -> list[dict[str, Any]]:
    """Aktuell gesicherte Termine, deren Beginn um diese Tage herum liegt.

    Liefert bewusst ±1 Tag (Zeitzonen); die genaue lokale Prüfung macht der Aufrufer.
    Serientermine werden nur über ihren ersten Termin erfasst.
    """
    if not iso_dates or not Path(store_path).exists():
        return []
    candidates: set[str] = set()
    for value in iso_dates:
        day = datetime.fromisoformat(value).date()
        candidates.update((day + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1))
    placeholders = ",".join("?" for _ in candidates)
    with closing(connect(store_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT p.calendar, v.uid, v.summary, v.dtstart
            FROM presence p JOIN event_versions v ON v.hash = p.version_hash
            WHERE p.valid_to IS NULL AND substr(v.dtstart, 1, 10) IN ({placeholders})
            """,
            sorted(candidates),
        ).fetchall()
    return [dict(row) for row in rows]


def calendar_colors(store_path: str) -> dict[str, str]:
    if not Path(store_path).exists():
        return {}
    with closing(connect(store_path)) as connection:
        return {
            row["name"]: row["color"]
            for row in connection.execute("SELECT name, color FROM calendars WHERE color IS NOT NULL")
        }


def backfill(store_path: str) -> int:
    """Wertet ältere gespeicherte Versionen mit dem aktuellen Parser neu aus."""
    if not Path(store_path).exists():
        return 0
    updated = 0
    with closing(connect(store_path)) as connection, connection:
        rows = connection.execute(
            "SELECT hash, ics FROM event_versions WHERE parser_version < ?",
            (PARSER_VERSION,),
        ).fetchall()
        for row in rows:
            info = parse_event(row["ics"])
            connection.execute(
                "UPDATE event_versions SET dtend = ?, rrule = ?, exdates = ?, "
                "parser_version = ? WHERE hash = ?",
                (info["dtend"], info["rrule"], json.dumps(info["exdates"]), PARSER_VERSION, row["hash"]),
            )
            updated += 1
    if updated:
        logger.info("%d gesicherte Termin-Versionen nachträglich ausgewertet.", updated)
    return updated


_SUPPORTED_RRULE_KEYS = {"FREQ", "INTERVAL", "BYDAY", "UNTIL", "COUNT", "WKST", "BYMONTHDAY", "BYMONTH"}


def rule_from_rrule(
    rrule: str | None, start: date, exdates: list[str] | None = None
) -> dict[str, Any] | None:
    """Übersetzt gängige iCloud-RRULEs in eine Regel; Unbekanntes → None."""
    if not rrule:
        return None
    try:
        parts = dict(part.split("=", 1) for part in rrule.upper().split(";") if part)
    except ValueError:
        return None
    if not set(parts) <= _SUPPORTED_RRULE_KEYS or parts.get("WKST", "MO") != "MO":
        return None
    frequency = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly", "YEARLY": "yearly"}.get(
        parts.get("FREQ", "")
    )
    if not frequency:
        return None
    weekdays = parts.get("BYDAY", "").split(",") if parts.get("BYDAY") else []
    if weekdays and (frequency != "weekly" or not all(day in recurrence.WEEKDAYS for day in weekdays)):
        return None  # z. B. „2MO“ (zweiter Montag) wird nicht unterstützt
    if "BYMONTHDAY" in parts and parts["BYMONTHDAY"] != str(start.day):
        return None
    if "BYMONTH" in parts and parts["BYMONTH"] != str(start.month):
        return None
    until = parts.get("UNTIL")
    try:
        return recurrence.normalize(
            {
                "frequency": frequency,
                "interval": parts.get("INTERVAL") or 1,
                "weekdays": weekdays,
                "until": f"{until[:4]}-{until[4:6]}-{until[6:8]}" if until else None,
                "count": parts.get("COUNT"),
                "exdates": exdates or [],
            },
            start,
            fix_weekday=True,
        )
    except ValueError:
        return None


def events_on_day(store_path: str, day: date, timezone_name: str) -> list[dict[str, Any]]:
    """Aktuelle iCloud-Termine an einem lokalen Tag, inklusive Serienterminen.

    Ergebnis je Termin: calendar, uid, summary, all_day, start/end (naive lokale
    datetime; bei ganztägigen Tagesgrenzen). Einzeln verschobene Termine einer
    Serie erscheinen zur ursprünglichen Zeit.
    """
    if not Path(store_path).exists():
        return []
    before, after = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
    with closing(connect(store_path)) as connection:
        rows = connection.execute(
            """
            SELECT p.calendar, v.uid, v.summary, v.dtstart, v.dtend, v.rrule, v.exdates
            FROM presence p JOIN event_versions v ON v.hash = p.version_hash
            WHERE p.valid_to IS NULL AND substr(v.dtstart, 1, 10) <= :after AND (
                v.rrule IS NOT NULL OR substr(COALESCE(v.dtend, v.dtstart), 1, 10) >= :before
            )
            """,
            {"before": before, "after": after},
        ).fetchall()
    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    result = []
    for row in rows:
        start = formatting.parse_event_value(row["dtstart"], timezone_name)
        if start is None:
            continue
        end = formatting.parse_event_value(row["dtend"], timezone_name) if row["dtend"] else None
        all_day = start[1] is None
        begin = start[1] or datetime.combine(start[0], datetime.min.time())
        if end is not None:
            finish = end[1] or datetime.combine(end[0], datetime.min.time())
        else:
            finish = begin + (timedelta(days=1) if all_day else timedelta(0))
        duration = max(finish - begin, timedelta(0))
        if row["rrule"]:
            rule = rule_from_rrule(row["rrule"], start[0], json.loads(row["exdates"] or "[]"))
            if rule is None or not recurrence.is_occurrence(rule, start[0], day):
                continue
            begin = datetime.combine(day, begin.time())
            finish = begin + duration
        elif not (begin < day_end and (finish > day_start or (duration == timedelta(0) and begin >= day_start))):
            continue
        result.append(
            {
                "calendar": row["calendar"],
                "uid": row["uid"],
                "summary": row["summary"],
                "all_day": all_day,
                "start": begin,
                "end": finish,
            }
        )
    result.sort(key=lambda item: (not item["all_day"], item["start"], item["summary"] or ""))
    return result

"""Termine direkt aus Kalendereinladungen (.ics / text/calendar) lesen.

Die Angaben einer Einladung sind exakt – sie brauchen keine KI. Ergebnis hat
dasselbe Format wie validierte KI-Vorschläge (siehe ai.validate_events).
"""

import logging
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import calendar_backup, person_learning

logger = logging.getLogger(__name__)

# Outlook/Exchange schreibt Windows-Zeitzonennamen statt IANA-Namen.
_WINDOWS_ZONES = {
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "romance standard time": "Europe/Paris",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "e. europe standard time": "Europe/Chisinau",
    "fle standard time": "Europe/Kiev",
    "gtb standard time": "Europe/Bucharest",
    "utc": "UTC",
}
_TZID = re.compile(r'TZID=("?)([^;:"]+)\1', re.IGNORECASE)


def _iana_tzid(match: re.Match[str]) -> str:
    name = match.group(2).strip()
    candidates = [_WINDOWS_ZONES.get(name.casefold()), name]
    parts = [part for part in name.split("/") if part]
    if len(parts) >= 2:  # z. B. /mozilla.org/20050126_1/Europe/Berlin
        candidates.append("/".join(parts[-2:]))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return f"TZID={candidate}"
    return f"TZID={name}"


def _blocks(text: str) -> tuple[str, list[list[str]]]:
    """METHOD und die einzelnen VEVENT-Blöcke (Zeilen bereits entfaltet)."""
    method = ""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    depth = 0
    for line in calendar_backup._unfold(text):
        upper = line.upper()
        if upper.startswith("METHOD:") and current is None:
            method = upper.split(":", 1)[1].strip()
        if upper == "BEGIN:VEVENT":
            current, depth = [line], 0
            continue
        if current is None:
            continue
        if upper.startswith("BEGIN:"):
            depth += 1  # z. B. VALARM – gehört nicht zum Termin
        elif upper.startswith("END:") and upper != "END:VEVENT":
            depth -= 1
        elif upper == "END:VEVENT":
            current.append(line)
            blocks.append(current)
            current = None
            continue
        if depth == 0 and not upper.startswith("END:"):
            current.append(line)
    return method, blocks


def _property(lines: list[str], name: str) -> str | None:
    for line in lines:
        head, _, value = line.partition(":")
        if head.split(";", 1)[0].upper() == name:
            return calendar_backup._unescape(value).strip()
    return None


def _local(value: str | None, timezone_name: str) -> str | None:
    """ISO-Wert aus dem Parser → Datum oder lokale Zeit ohne Offset."""
    if not value:
        return None
    if len(value) == 10:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%dT%H:%M")


def _exclusive_allday_end(start: str, end: str | None) -> str | None:
    """iCalendar-Ganztagsende ist exklusiv; Vorschläge speichern das letzte Tag."""
    if not end or len(start) != 10 or len(end) != 10:
        return end
    last = date.fromordinal(date.fromisoformat(end).toordinal() - 1)
    return None if last.isoformat() <= start else last.isoformat()


def parse_invitation(
    text: str, timezone_name: str, source_part: str | None = None
) -> list[dict[str, Any]]:
    text = _TZID.sub(_iana_tzid, text)
    method, blocks = _blocks(text)
    by_uid: dict[str, list[list[str]]] = {}
    for index, block in enumerate(blocks):
        uid = _property(block, "UID") or f"ohne-uid-{index}"
        by_uid.setdefault(uid, []).append(block)

    events: list[dict[str, Any]] = []
    for uid, group in by_uid.items():
        masters = [b for b in group if _property(b, "RECURRENCE-ID") is None]
        for block in masters or group:
            parsed = calendar_backup.parse_event("\n".join(block))
            start = _local(parsed["dtstart"], timezone_name)
            if not start:
                logger.warning("Kalendereinladung ohne gültigen Beginn übersprungen (UID %s).", uid)
                continue
            end = _exclusive_allday_end(start, _local(parsed["dtend"], timezone_name))
            status = (_property(block, "STATUS") or "").upper()
            is_occurrence = _property(block, "RECURRENCE-ID") is not None
            cancelled = method == "CANCEL" or status == "CANCELLED"
            notes = _property(block, "DESCRIPTION")
            confidence = 1.0
            rule = None
            if parsed["rrule"] and not is_occurrence:
                rule = calendar_backup.rule_from_rrule(
                    parsed["rrule"], date.fromisoformat(start[:10]), parsed["exdates"]
                )
                if rule is None:
                    confidence = 0.6
                    hint = (
                        "Die Einladung beschreibt eine Wiederholung, die hier nicht "
                        f"abgebildet werden kann ({parsed['rrule']}). Bitte von Hand prüfen."
                    )
                    notes = f"{hint}\n\n{notes}" if notes else hint
            summary = parsed["summary"]
            events.append(
                {
                    "title": summary[:300],
                    "start": start,
                    "end": end,
                    "confidence": confidence,
                    "action": "cancel" if cancelled else "new",
                    "location": parsed["location"][:2000] if parsed["location"] else None,
                    "person": None,
                    "notes": notes[:2000] if notes else None,
                    "recurrence": rule,
                    "quote": " ".join(summary.split())[:300],
                    "quote_source": "ics",
                    "source_part": source_part or "Kalendereinladung",
                    "ics_uid": uid,
                }
            )
    return events


def _same_moment(a: str, b: str) -> bool:
    return a[:16] == b[:16]


def drop_duplicates_of(
    ai_events: list[dict[str, Any]], ics_events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """KI-Funde verwerfen, die eine Einladung bereits exakt liefert."""
    kept = []
    for event in ai_events:
        start = str(event.get("start") or "")
        duplicate = any(
            _same_moment(start, str(ics["start"]))
            or (
                start[:10] == str(ics["start"])[:10]
                and person_learning.title_similarity(str(event.get("title") or ""), str(ics["title"])) >= 0.5
            )
            for ics in ics_events
        )
        if not duplicate:
            kept.append(event)
    return kept, len(ai_events) - len(kept)

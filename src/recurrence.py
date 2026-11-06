"""Wiederkehrende Termine: Regel prüfen, Termine berechnen, beschreiben, RRULE.

Eine Regel ist ein dict:
    {"frequency": "daily" | "weekly" | "monthly" | "yearly",
     "interval": 1..99,
     "weekdays": ["MO", ...]        # nur bei weekly, sonst []
     "until": "YYYY-MM-DD" | None,  # letzter möglicher Tag (einschließlich)
     "count": 1..500 | None,        # Anzahl Termine (vor Abzug der Ausnahmen)
     "exdates": ["YYYY-MM-DD", ...]}  # ausgelassene Termine

Die Berechnung folgt RFC 5545: Der Beginn ist der erste Termin, COUNT zählt
vor dem Abzug der Ausnahmen, Monatstage, die es nicht gibt (z. B. 31.), werden
übersprungen, Wochen beginnen am Montag.
"""

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import i18n
from .i18n import _

FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_NAMES = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")
MAX_OCCURRENCES = 2000  # Schutz vor endlosen Berechnungen


def _parse_date(value: Any, field: str) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        if "." in text:  # deutsches Format TT.MM.JJJJ
            day, month, year = (int(part) for part in text.split("."))
            return date(year, month, day)
        return date.fromisoformat(text[:10])
    except (ValueError, TypeError) as error:
        raise ValueError(_("{field}: „{text}“ ist kein gültiges Datum.", field=_(field), text=text)) from error


def normalize(raw: Any, start: date, *, fix_weekday: bool = False) -> dict[str, Any] | None:
    """Prüft eine Regel. None = keine Wiederholung. Fehler → ValueError (deutsch).

    fix_weekday: Fällt der Beginn nicht auf einen der Wochentage (KI-Fehler),
    wird sein Wochentag ergänzt statt abgelehnt.
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise ValueError(_("Die Wiederholung hat ein ungültiges Format."))
    frequency = str(raw.get("frequency") or "").strip().lower()
    if frequency in ("", "none", "keine"):
        return None
    if frequency not in FREQUENCIES:
        raise ValueError(_("Unbekannte Wiederholungsart."))
    try:
        raw_interval = raw.get("interval")
        interval = 1 if raw_interval in (None, "") else int(raw_interval)
    except (TypeError, ValueError) as error:
        raise ValueError(_("Das Intervall muss eine ganze Zahl sein.")) from error
    if not 1 <= interval <= 99:
        raise ValueError(_("Das Intervall muss zwischen 1 und 99 liegen."))

    weekdays: list[str] = []
    if frequency == "weekly":
        raw_days = raw.get("weekdays") or []
        if not isinstance(raw_days, list):
            raise ValueError(_("Die Wochentage haben ein ungültiges Format."))
        chosen = {str(day).strip().upper()[:2] for day in raw_days}
        if not chosen <= set(WEEKDAYS):
            raise ValueError(_("Unbekannter Wochentag."))
        start_day = WEEKDAYS[start.weekday()]
        if chosen and start_day not in chosen:
            if not fix_weekday:
                raise ValueError(
                    _("Der Beginn muss auf einen der gewählten Wochentage fallen.")
                )
            chosen.add(start_day)
        weekdays = [day for day in WEEKDAYS if day in chosen]

    until = raw.get("until")
    count = raw.get("count")
    until_date = _parse_date(until, "Ende") if until else None
    if until_date and until_date < start:
        raise ValueError(_("Das Ende der Wiederholung liegt vor dem Beginn."))
    count_value = None
    if count not in (None, "") and not until_date:
        try:
            count_value = int(count)
        except (TypeError, ValueError) as error:
            raise ValueError(_("Die Anzahl muss eine ganze Zahl sein.")) from error
        if not 1 <= count_value <= 500:
            raise ValueError(_("Die Anzahl muss zwischen 1 und 500 liegen."))

    raw_exdates = raw.get("exdates") or []
    if isinstance(raw_exdates, str):
        raw_exdates = [part for part in raw_exdates.replace(";", ",").split(",") if part.strip()]
    if not isinstance(raw_exdates, list) or len(raw_exdates) > 100:
        raise ValueError(_("Zu viele oder ungültige Ausnahmen."))
    exdates = sorted({_parse_date(value, "Ausnahme").isoformat() for value in raw_exdates})

    return {
        "frequency": frequency,
        "interval": interval,
        "weekdays": weekdays,
        "until": until_date.isoformat() if until_date else None,
        "count": count_value,
        "exdates": exdates,
    }


def _add_months(value: date, months: int) -> date | None:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    if value.day > calendar.monthrange(year, month)[1]:
        return None  # Tag existiert in diesem Monat nicht → überspringen
    return date(year, month, value.day)


def _raw_occurrences(rule: dict[str, Any], start: date) -> Iterator[date]:
    interval = rule["interval"]
    frequency = rule["frequency"]
    step = 0
    while True:
        if frequency == "daily":
            yield start + timedelta(days=step * interval)
        elif frequency == "weekly":
            if rule["weekdays"]:
                monday = start - timedelta(days=start.weekday()) + timedelta(weeks=step * interval)
                for day in rule["weekdays"]:
                    candidate = monday + timedelta(days=WEEKDAYS.index(day))
                    if candidate >= start:
                        yield candidate
            else:
                yield start + timedelta(weeks=step * interval)
        elif frequency == "monthly":
            candidate = _add_months(start, step * interval)
            if candidate:
                yield candidate
        else:  # yearly
            candidate = _add_months(start, 12 * step * interval)
            if candidate:
                yield candidate
        step += 1
        if step > MAX_OCCURRENCES * 10:
            return


def occurrences(rule: dict[str, Any], start: date) -> Iterator[date]:
    """Alle Termine der Serie (ohne Ausnahmen), höchstens MAX_OCCURRENCES."""
    until = date.fromisoformat(rule["until"]) if rule.get("until") else None
    count = rule.get("count")
    excluded = set(rule.get("exdates") or [])
    produced = 0
    for candidate in _raw_occurrences(rule, start):
        if until and candidate > until:
            return
        if count is not None and produced >= count:
            return
        produced += 1
        if produced > MAX_OCCURRENCES:
            return
        if candidate.isoformat() not in excluded:
            yield candidate


def is_finite(rule: dict[str, Any]) -> bool:
    return bool(rule.get("until") or rule.get("count"))


def next_occurrence(rule: dict[str, Any], start: date, today: date) -> date | None:
    for candidate in occurrences(rule, start):
        if candidate >= today:
            return candidate
    return None


def last_occurrence(rule: dict[str, Any], start: date) -> date | None:
    """Letzter Termin; None bei offenen Serien oder wenn alle ausgenommen sind."""
    if not is_finite(rule):
        return None
    last = None
    for last in occurrences(rule, start):
        pass
    return last


def is_occurrence(rule: dict[str, Any], start: date, day: date) -> bool:
    for candidate in occurrences(rule, start):
        if candidate == day:
            return True
        if candidate > day:
            return False
    return False


WEEKDAY_NAMES_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if i18n.current() == "en":
        return f"{parsed.day} {_MONTHS_EN[parsed.month - 1]} {parsed.year}"
    return f"{parsed.day}.{parsed.month}.{parsed.year}"


def _join(items: list[str], word: str) -> str:
    return f" {word} ".join([", ".join(items[:-1]), items[-1]]) if len(items) > 1 else (items[0] if items else "")


def _describe_en(rule: dict[str, Any]) -> str:
    interval = rule["interval"]
    frequency = rule["frequency"]
    if frequency == "weekly":
        day_text = _join([WEEKDAY_NAMES_EN[WEEKDAYS.index(day)] for day in rule["weekdays"]], "and")
        if interval == 1:
            text = f"Every {day_text}" if day_text else "Weekly"
        else:
            text = f"Every {interval} weeks" + (f" on {day_text}" if day_text else "")
    else:
        single = {"daily": "Daily", "monthly": "Monthly", "yearly": "Yearly"}[frequency]
        unit = {"daily": "days", "monthly": "months", "yearly": "years"}[frequency]
        text = single if interval == 1 else f"Every {interval} {unit}"
    if rule.get("until"):
        text += f" until {_short_date(rule['until'])}"
    elif rule.get("count"):
        text += f", {rule['count']} times"
    else:
        text += ", no end date"
    if rule.get("exdates"):
        text += ", except " + ", ".join(_short_date(value) for value in rule["exdates"])
    return text


def describe(rule: dict[str, Any] | None) -> str:
    if not rule:
        return _("Keine")
    if i18n.current() == "en":
        return _describe_en(rule)
    interval = rule["interval"]
    frequency = rule["frequency"]
    if frequency == "weekly":
        days = [WEEKDAY_NAMES[WEEKDAYS.index(day)] for day in rule["weekdays"]]
        day_text = _join(days, "und")
        if interval == 1:
            text = f"Jeden {day_text}" if day_text else "Wöchentlich"
        else:
            text = f"Alle {interval} Wochen" + (f" am {day_text}" if day_text else "")
    else:
        single = {"daily": "Täglich", "monthly": "Monatlich", "yearly": "Jährlich"}[frequency]
        unit = {"daily": "Tage", "monthly": "Monate", "yearly": "Jahre"}[frequency]
        text = single if interval == 1 else f"Alle {interval} {unit}"
    if rule.get("until"):
        text += f" bis {_short_date(rule['until'])}"
    elif rule.get("count"):
        text += f", {rule['count']} Termine"
    else:
        text += ", ohne Ende"
    if rule.get("exdates"):
        text += ", außer " + ", ".join(_short_date(value) for value in rule["exdates"])
    return text


def ical_lines(
    rule: dict[str, Any], *, all_day: bool, timezone_name: str, start_time: str
) -> list[str]:
    """RRULE- und EXDATE-Zeilen. start_time: lokale Startzeit HHMMSS (bei Uhrzeit)."""
    parts = [f"FREQ={rule['frequency'].upper()}"]
    if rule["interval"] > 1:
        parts.append(f"INTERVAL={rule['interval']}")
    if rule["weekdays"]:
        parts.append("BYDAY=" + ",".join(rule["weekdays"]))
    if rule.get("until"):
        until = date.fromisoformat(rule["until"])
        if all_day:
            parts.append(f"UNTIL={until:%Y%m%d}")
        else:
            # Bei DTSTART mit TZID muss UNTIL in UTC angegeben werden.
            local_end = datetime.combine(until, datetime.max.time().replace(microsecond=0))
            utc_end = local_end.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)
            parts.append(f"UNTIL={utc_end:%Y%m%dT%H%M%SZ}")
    elif rule.get("count"):
        parts.append(f"COUNT={rule['count']}")
    lines = ["RRULE:" + ";".join(parts)]
    for value in rule.get("exdates") or []:
        day = date.fromisoformat(value)
        if all_day:
            lines.append(f"EXDATE;VALUE=DATE:{day:%Y%m%d}")
        else:
            lines.append(f"EXDATE;TZID={timezone_name}:{day:%Y%m%d}T{start_time}")
    return lines

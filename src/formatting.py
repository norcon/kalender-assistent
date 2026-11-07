"""Gut lesbare Darstellung von Zeitangaben und Verlaufseinträgen (Deutsch/Englisch)."""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import i18n, recurrence
from .i18n import _, ngettext

WEEKDAYS = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")
MONTHS = (
    "Jan.", "Feb.", "März", "Apr.", "Mai", "Juni",
    "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez.",
)
WEEKDAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _english() -> bool:
    return i18n.current() == "en"


def _clock(moment: datetime) -> str:
    return f"{moment:%H:%M}" if _english() else f"{moment:%H:%M} Uhr"

HISTORY_LABELS = {
    "created": "Erkannt",
    "edited": "Bearbeitet",
    "exported": "Nach iCloud übertragen",
    "resent": "Erneut an iCloud gesendet",
    "archived": "Archiviert",
    "auto_archived": "Automatisch archiviert (Termin vorbei)",
    "restored": "Aus dem Archiv wiederhergestellt",
    "dismiss_undone": "Entfernen rückgängig gemacht",
    "email_change_reopened": "Ablehnung der Folge-Mail rückgängig gemacht",
    "cancelled": "Absage bestätigt",
    "occurrence_cancelled": "Einzeltermin der Serie abgesagt",
    "updated_from_email": "Aus Folge-Mail aktualisiert",
    "email_change_linked": "Folge-Mail zugeordnet",
    "email_change_accepted": "Änderung aus Folge-Mail bestätigt",
    "email_change_rejected": "Änderung aus Folge-Mail abgelehnt",
    "learning_rule_deleted": "Gelernte Korrektur gelöscht",
    "hidden": "Ausgeblendet (Absender ignoriert)",
    "unhidden": "Wieder eingeblendet",
    "reminder_changed": "Erinnerung geändert",
}

TIMED_REMINDERS = (0, 5, 10, 15, 30, 60, 120, 1440, 2880, 10080)
# Ganztägig: Minuten vor 0 Uhr des Tages (negativ = am Tag selbst)
ALLDAY_REMINDERS = (-480, -420, 360, 720, 1800, 9000)


def reminder_label(minutes: int, all_day: bool) -> str:
    if all_day:
        days_before = (minutes + 1439) // 1440 if minutes > 0 else 0
        hour = (-minutes // 60) if minutes <= 0 else (24 * days_before * 60 - minutes) // 60
        if days_before == 0:
            return _("Am Tag um {hour} Uhr", hour=hour)
        if days_before == 1:
            return _("Am Vortag um {hour} Uhr", hour=hour)
        if days_before == 7:
            return _("1 Woche vorher um {hour} Uhr", hour=hour)
        return _("{days} Tage vorher um {hour} Uhr", days=days_before, hour=hour)
    if minutes == 0:
        return _("Zu Beginn")
    if minutes % 10080 == 0:
        return ngettext("{count} Woche vorher", "{count} Wochen vorher", minutes // 10080)
    if minutes % 1440 == 0:
        return ngettext("{count} Tag vorher", "{count} Tage vorher", minutes // 1440)
    if minutes % 60 == 0:
        return _("{count} Std. vorher", count=minutes // 60)
    return _("{count} Min. vorher", count=minutes)


def reminder_options(
    current: str | None, all_day: bool, default_minutes: int, allday_hour: int
) -> list[tuple[str, str, bool]]:
    """(Wert, Beschriftung, ausgewählt) für die Auswahl auf der Karte."""
    if all_day:
        default = _("keine") if allday_hour >= 24 else reminder_label((24 - allday_hour) * 60, True)
        values = ALLDAY_REMINDERS
    else:
        default = reminder_label(default_minutes, False) if default_minutes else _("keine")
        values = TIMED_REMINDERS
    options = [
        ("", _("Standard ({value})", value=default), current in (None, "")),
        ("none", _("Keine"), current == "none"),
    ]
    known = {str(v) for v in values}
    if current not in (None, "", "none") and current not in known:
        try:
            options.append((current, reminder_label(int(current), all_day), True))
        except ValueError:
            pass
    options.extend((str(v), reminder_label(v, all_day), current == str(v)) for v in values)
    return options


def relative_day(day: date | None, today: date) -> str:
    """„heute“, „morgen“, „in 3 Tagen“, „in 2 Wochen“ …"""
    if day is None:
        return ""
    delta = (day - today).days
    if delta == 0:
        return _("heute")
    if delta == 1:
        return _("morgen")
    if delta == 2:
        return _("übermorgen")
    if delta == -1:
        return _("gestern")
    if delta < 0:
        return _("vor {count} Tagen", count=-delta)
    if delta < 14:
        return _("in {count} Tagen", count=delta)
    if delta < 60:
        return _("in {count} Wochen", count=delta // 7)
    return ngettext("in {count} Monat", "in {count} Monaten", round(delta / 30.44))


def history_label(action: str) -> str:
    label = HISTORY_LABELS.get(action)
    return _(label) if label else action.replace("_", " ")


def _date_text(value: date, *, with_weekday: bool = True, with_year: bool = True) -> str:
    if _english():
        text = f"{value.day} {MONTHS_EN[value.month - 1]}"
        if with_year:
            text += f" {value.year}"
        return f"{WEEKDAYS_EN[value.weekday()]}, {text}" if with_weekday else text
    text = f"{value.day}. {MONTHS[value.month - 1]}"
    if with_year:
        text += f" {value.year}"
    if with_weekday:
        text = f"{WEEKDAYS[value.weekday()]}, {text}"
    return text


def parse_event_value(value: Any, timezone_name: str) -> tuple[date, datetime | None] | None:
    """Termin-Zeitangabe → (lokales Datum, lokale Uhrzeit oder None bei ganztägig)."""
    if not value:
        return None
    raw = str(value).strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw), None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
    return parsed.date(), parsed


def event_time(value: Any, timezone_name: str) -> str:
    parsed = parse_event_value(value, timezone_name)
    if parsed is None:
        return str(value or "")
    day, moment = parsed
    text = _date_text(day)
    return f"{text}, {_clock(moment)}" if moment else text


def event_range(start: Any, end: Any, timezone_name: str) -> str:
    start_text = event_time(start, timezone_name)
    parsed_start = parse_event_value(start, timezone_name)
    parsed_end = parse_event_value(end, timezone_name)
    if parsed_end is None:
        return start_text
    if parsed_start and parsed_end[0] == parsed_start[0] and parsed_end[1]:
        return f"{start_text} – {_clock(parsed_end[1])}"
    return f"{start_text} – {event_time(end, timezone_name)}"


MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)
MONTH_NAMES_EN = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def month_label(value: date) -> str:
    names = MONTH_NAMES_EN if _english() else MONTH_NAMES
    return f"{names[value.month - 1]} {value.year}"


def week_label(monday: date) -> str:
    """„KW 45 · 2.–8. Nov. 2026“ (ISO-Kalenderwoche, Montag bis Sonntag)."""
    sunday = monday + timedelta(days=6)
    week = monday.isocalendar().week
    if _english():
        if monday.month == sunday.month:
            span = f"{monday.day}–{sunday.day} {MONTHS_EN[sunday.month - 1]} {sunday.year}"
        else:
            span = f"{_date_text(monday, with_weekday=False, with_year=monday.year != sunday.year)} – {_date_text(sunday, with_weekday=False)}"
        return f"Week {week} · {span}"
    if monday.month == sunday.month:
        span = f"{monday.day}.–{sunday.day}. {MONTHS[sunday.month - 1]} {sunday.year}"
    else:
        span = f"{_date_text(monday, with_weekday=False, with_year=monday.year != sunday.year)} – {_date_text(sunday, with_weekday=False)}"
    return f"KW {week} · {span}"


def day_label(value: date) -> str:
    if _english():
        return f"{WEEKDAYS_EN[value.weekday()]} {value.day}"
    return f"{WEEKDAYS[value.weekday()]} {value.day}."


def clock_text(value: Any, timezone_name: str) -> str:
    """Nur die Uhrzeit (lokal) – oder „ganztägig“."""
    parsed = parse_event_value(value, timezone_name)
    if parsed is None or parsed[1] is None:
        return _("ganztägig")
    return f"{parsed[1]:%H:%M}"


def timestamp(value: Any, timezone_name: str) -> str:
    """Zeitpunkt (z. B. Empfang, Verlauf) in lokaler Zeit."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(ZoneInfo(timezone_name))
    return f"{_date_text(local.date())}, {local:%H:%M}"


def relative_time(value: Any, now: datetime | None = None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = ((now or datetime.now(timezone.utc)) - parsed).total_seconds()
    minutes = int(max(seconds, 0) // 60)
    if minutes < 1:
        return _("gerade eben")
    if minutes < 60:
        return _("vor {count} Min.", count=minutes)
    hours = minutes // 60
    if hours < 24:
        return _("vor {count} Std.", count=hours)
    days = hours // 24
    if days == 1:
        return _("gestern")
    if days < 30:
        return _("vor {count} Tagen", count=days)
    return ngettext("vor {count} Monat", "vor {count} Monaten", days // 30)


def _shift_days(value: Any, days: int) -> Any:
    """Verschiebt eine ISO-Zeitangabe um ganze Tage (Uhrzeit bleibt)."""
    if not value or not days:
        return value
    text = str(value)
    shifted = (date.fromisoformat(text[:10]) + timedelta(days=days)).isoformat()
    return shifted + text[10:]


def next_occurrence(item: dict[str, Any], today: date, timezone_name: str) -> date | None:
    """Nächster Termin einer Serie ab heute (None: Serie vorbei)."""
    parsed = parse_event_value(item.get("starts_at"), timezone_name)
    rule = item.get("recurrence")
    if parsed is None or not rule:
        return None
    return recurrence.next_occurrence(rule, parsed[0], today)


def event_has_ended(
    item: dict[str, Any],
    now_local: datetime,
    timezone_name: str,
    default_event_minutes: int,
) -> bool:
    """Ist der Termin vorbei? now_local ist eine naive lokale Zeit."""
    start = parse_event_value(item.get("starts_at"), timezone_name)
    if start is None:
        return False
    rule = item.get("recurrence")
    if rule:
        # Serie: vorbei erst nach dem letzten Termin; offene Serien nie.
        if not recurrence.is_finite(rule):
            return False
        last = recurrence.last_occurrence(rule, start[0])
        if last is None:
            return True
        offset = (last - start[0]).days
        item = {
            "starts_at": _shift_days(item.get("starts_at"), offset),
            "ends_at": _shift_days(item.get("ends_at"), offset),
        }
        start = parse_event_value(item["starts_at"], timezone_name)
    end = parse_event_value(item.get("ends_at"), timezone_name)
    if end is not None:
        end_day, end_moment = end
        return end_moment <= now_local if end_moment else end_day < now_local.date()
    start_day, start_moment = start
    if start_moment is None:
        return start_day < now_local.date()
    return start_moment + timedelta(minutes=default_event_minutes) <= now_local


def group_suggestions(
    suggestions: Iterable[dict[str, Any]], today: date, timezone_name: str
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Ordnet Vorschläge Zeiträumen zu: Heute, Morgen, Diese Woche, …"""
    end_of_week = today + timedelta(days=6 - today.weekday())
    end_of_next_week = end_of_week + timedelta(days=7)
    order = (
        "Vergangen", "Heute", "Morgen", "Diese Woche", "Nächste Woche",
        "Später", "Ohne gültiges Datum",
    )
    groups: dict[str, list[dict[str, Any]]] = {label: [] for label in order}
    for item in suggestions:
        parsed = parse_event_value(item.get("starts_at"), timezone_name)
        if parsed is None:
            label = "Ohne gültiges Datum"
        else:
            day = parsed[0]
            if item.get("recurrence"):
                day = next_occurrence(item, today, timezone_name) or day
            if day < today:
                label = "Vergangen"
            elif day == today:
                label = "Heute"
            elif day == today + timedelta(days=1):
                label = "Morgen"
            elif day <= end_of_week:
                label = "Diese Woche"
            elif day <= end_of_next_week:
                label = "Nächste Woche"
            else:
                label = "Später"
        groups[label].append(item)
    return [(_(label), items) for label, items in groups.items() if items]


def file_size(value: int | float | None) -> str:
    size = float(value or 0)
    for unit in ("Bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            text = f"{size:.0f}" if unit == "Bytes" or size >= 100 else f"{size:.1f}"
            if not _english():
                text = text.replace(".", ",")
            return f"{text} {unit}"
        size /= 1024
    return f"{size:.0f} TB"


def duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "–"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min {rest:02d} s" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"

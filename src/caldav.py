import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape

import httpx

from .i18n import _
from . import recurrence

logger = logging.getLogger(__name__)

_DAV_NS = "DAV:"
_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
_APPLE_NS = "http://apple.com/ns/ical/"
_PROPFIND = f"""<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="{_DAV_NS}" xmlns:c="{_CALDAV_NS}" xmlns:a="{_APPLE_NS}">
  <d:prop>
    <a:calendar-color />
    <d:current-user-principal />
    <c:calendar-home-set />
    <d:resourcetype />
    <d:displayname />
    <d:current-user-privilege-set />
  </d:prop>
</d:propfind>"""


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _property_value(response: ET.Element, name: str) -> ET.Element | None:
    for propstat in response:
        if _local_name(propstat) != "propstat":
            continue
        status = next(
            (child.text or "" for child in propstat if _local_name(child) == "status"),
            "",
        )
        if " 200 " not in f" {status} ":
            continue
        prop = next(
            (child for child in propstat if _local_name(child) == "prop"), None
        )
        if prop is None:
            continue
        for item in prop:
            if _local_name(item) == name:
                return item
    return None


def _href(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    for child in element.iter():
        if _local_name(child) == "href" and child.text:
            return child.text.strip()
    return None


def _request(
    client: httpx.Client, url: str, depth: str
) -> tuple[httpx.Response, ET.Element]:
    response = client.request(
        "PROPFIND",
        url,
        headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        content=_PROPFIND.encode("utf-8"),
    )
    if response.status_code not in (200, 207):
        raise httpx.HTTPStatusError(
            f"CalDAV discovery returned HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as error:
        raise ValueError("iCloud returned an invalid XML response") from error
    return response, root


def _responses(root: ET.Element) -> list[ET.Element]:
    return [child for child in root if _local_name(child) == "response"]


def _normalize_color(element: ET.Element | None) -> str | None:
    """Apple liefert #RRGGBB oder #RRGGBBAA; nur gültige Werte übernehmen."""
    if element is None or not element.text:
        return None
    value = element.text.strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", value):
        return value[:7].lower()
    return None


def discover_icloud_calendars(
    base_url: str, username: str, app_password: str, timeout_seconds: int
) -> list[dict[str, str | bool | None]]:
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10))
    with httpx.Client(
        auth=(username, app_password),
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "FamilyCalendarAssistant/0.1"},
    ) as client:
        service_response, service_root = _request(client, base_url.rstrip("/") + "/", "0")
        service_url = str(service_response.url)
        principal_href = next(
            (
                _href(_property_value(response, "current-user-principal"))
                for response in _responses(service_root)
                if _property_value(response, "current-user-principal") is not None
            ),
            None,
        )
        if not principal_href:
            raise ValueError("iCloud did not return a calendar principal")

        principal_url = urljoin(service_url, principal_href)
        principal_response, principal_root = _request(client, principal_url, "0")
        home_href = next(
            (
                _href(_property_value(response, "calendar-home-set"))
                for response in _responses(principal_root)
                if _property_value(response, "calendar-home-set") is not None
            ),
            None,
        )
        if not home_href:
            raise ValueError("iCloud did not return a calendar home")

        home_url = urljoin(str(principal_response.url), home_href)
        home_response, home_root = _request(client, home_url, "1")
        calendars: list[dict[str, str | bool | None]] = []
        seen_hrefs: set[str] = set()
        for response in _responses(home_root):
            resource_type = _property_value(response, "resourcetype")
            if resource_type is None or not any(
                _local_name(item) == "calendar" for item in resource_type.iter()
            ):
                continue
            href_value = _href(response)
            if not href_value:
                continue
            calendar_url = urljoin(str(home_response.url), href_value)
            if calendar_url in seen_hrefs:
                continue
            seen_hrefs.add(calendar_url)

            display_name = _property_value(response, "displayname")
            privileges = _property_value(response, "current-user-privilege-set")
            privilege_names = (
                {_local_name(item) for item in privileges.iter()}
                if privileges is not None
                else set()
            )
            has_write = (
                True
                if "write" in privilege_names or "write-content" in privilege_names
                else False
                if privileges is not None
                else None
            )
            calendars.append(
                {
                    "name": (
                        (display_name.text or "").strip()
                        if display_name is not None
                        else ""
                    )
                    or "Unbenannter Kalender",
                    "write_access": has_write,
                    "calendar_url": calendar_url,
                    "color": _normalize_color(_property_value(response, "calendar-color")),
                }
            )

    logger.info("CalDAV-Erkennung abgeschlossen; %d Kalender gefunden.", len(calendars))
    return calendars


def _escape_ical_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _format_ical_datetime(value: str, timezone_name: str) -> tuple[str, bool]:
    try:
        if len(value) == 10:
            return date.fromisoformat(value).strftime("%Y%m%d"), True
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(_("Der Termin enthält ein ungültiges Datum oder eine Uhrzeit.")) from error

    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            _("Die konfigurierte Zeitzone „{zone}“ ist ungültig.", zone=timezone_name)
        ) from error
    if parsed.tzinfo is None:
        first = parsed.replace(tzinfo=local_zone, fold=0)
        second = parsed.replace(tzinfo=local_zone, fold=1)
        first_valid = (
            first.astimezone(timezone.utc).astimezone(local_zone).replace(tzinfo=None)
            == parsed
        )
        second_valid = (
            second.astimezone(timezone.utc).astimezone(local_zone).replace(tzinfo=None)
            == parsed
        )
        if not first_valid or not second_valid:
            raise ValueError(
                _("Die lokale Uhrzeit existiert wegen einer Zeitumstellung nicht.")
            )
        if first.utcoffset() != second.utcoffset():
            raise ValueError(
                _("Die lokale Uhrzeit ist wegen der Zeitumstellung doppeldeutig.")
            )
        parsed = first
    return parsed.astimezone(local_zone).strftime("%Y%m%dT%H%M%S"), False


def _format_utc_offset(value: timedelta) -> str:
    total_seconds = int(value.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted = f"{sign}{hours:02d}{minutes:02d}"
    return formatted if seconds == 0 else f"{formatted}{seconds:02d}"


def _timezone_transitions(
    local_zone: ZoneInfo, start_year: int, end_year: int
) -> list[tuple[datetime, timedelta, timedelta, str, bool]]:
    range_start = datetime(start_year, 1, 1, tzinfo=timezone.utc)
    range_end = datetime(end_year + 1, 1, 1, tzinfo=timezone.utc)
    transitions: list[tuple[datetime, timedelta, timedelta, str, bool]] = []
    previous = range_start
    previous_offset = previous.astimezone(local_zone).utcoffset()
    step = timedelta(hours=6)

    while previous < range_end:
        current = min(previous + step, range_end)
        current_offset = current.astimezone(local_zone).utcoffset()
        if current_offset != previous_offset:
            lower = previous
            upper = current
            while (upper - lower).total_seconds() > 1:
                midpoint = lower + (upper - lower) / 2
                if midpoint.astimezone(local_zone).utcoffset() == previous_offset:
                    lower = midpoint
                else:
                    upper = midpoint
            transition = upper.replace(microsecond=0)
            before = (transition - timedelta(seconds=1)).astimezone(local_zone)
            after = transition.astimezone(local_zone)
            old_offset = before.utcoffset()
            new_offset = after.utcoffset()
            if old_offset is not None and new_offset is not None:
                transitions.append(
                    (
                        transition,
                        old_offset,
                        new_offset,
                        after.tzname() or "",
                        after.dst() not in (None, timedelta(0)),
                    )
                )
            previous = transition
            previous_offset = new_offset
        else:
            previous = current
            previous_offset = current_offset
    return transitions


def _vtimezone_component(timezone_name: str, years: set[int]) -> list[str]:
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            _("Die konfigurierte Zeitzone „{zone}“ ist ungültig.", zone=timezone_name)
        ) from error
    start_year = max(1, min(years) - 1)
    end_year = min(9998, max(years) + 1)
    transitions = _timezone_transitions(local_zone, start_year, end_year)
    lines = ["BEGIN:VTIMEZONE", f"TZID:{_escape_ical_text(timezone_name)}"]
    if not transitions:
        offset = datetime(start_year, 1, 1, tzinfo=timezone.utc).astimezone(
            local_zone
        ).utcoffset()
        if offset is None:
            raise ValueError(_("Für die konfigurierte Zeitzone konnte kein Offset ermittelt werden."))
        lines.extend(
            [
                "BEGIN:STANDARD",
                f"DTSTART:{start_year}0101T000000",
                f"TZOFFSETFROM:{_format_utc_offset(offset)}",
                f"TZOFFSETTO:{_format_utc_offset(offset)}",
                "END:STANDARD",
            ]
        )
    else:
        for transition, old_offset, new_offset, name, is_daylight in transitions:
            observance = "DAYLIGHT" if is_daylight else "STANDARD"
            local_transition = (
                transition.replace(tzinfo=None) + old_offset
            ).strftime("%Y%m%dT%H%M%S")
            lines.extend(
                [
                    f"BEGIN:{observance}",
                    f"DTSTART:{local_transition}",
                    f"TZOFFSETFROM:{_format_utc_offset(old_offset)}",
                    f"TZOFFSETTO:{_format_utc_offset(new_offset)}",
                ]
            )
            if name:
                lines.append(f"TZNAME:{_escape_ical_text(name)}")
            lines.append(f"END:{observance}")
    lines.append("END:VTIMEZONE")
    return lines


def _fold_ical_line(line: str) -> str:
    chunks: list[str] = []
    current = ""
    byte_limit = 75
    for character in line:
        if current and len((current + character).encode("utf-8")) > byte_limit:
            chunks.append(current)
            current = character
            byte_limit = 74
        else:
            current += character
    chunks.append(current)
    return "\r\n ".join(chunks)


def event_uid(suggestion_id: int) -> str:
    return f"family-calendar-assistant-{suggestion_id}@local.invalid"


def alarm_trigger(
    reminder: str | None,
    all_day: bool,
    reminder_minutes: int | None,
    allday_reminder_hour: int | None,
) -> str | None:
    """VALARM-TRIGGER: eigene Erinnerung des Vorschlags oder der Standard.

    Eigene Werte sind Minuten vor Beginn (bei Ganztagsterminen vor 0 Uhr des
    Tages); negative Werte liegen danach, z. B. -480 = am Tag um 8 Uhr.
    """
    if reminder == "none":
        return None
    if reminder not in (None, ""):
        try:
            minutes = int(str(reminder))
        except ValueError:
            minutes = None
        if minutes is not None:
            return f"-PT{minutes}M" if minutes > 0 else f"PT{-minutes}M"
    if all_day:
        if allday_reminder_hour is not None and 0 <= allday_reminder_hour < 24:
            # Relativ zum Tagesbeginn (00:00): z. B. 18 Uhr am Vortag = -PT6H.
            return f"-PT{24 - allday_reminder_hour}H"
        return None
    return f"-PT{reminder_minutes}M" if reminder_minutes else None


def _build_icalendar(
    suggestion: dict[str, object],
    uid: str,
    timezone_name: str,
    default_event_minutes: int = 60,
    reminder_minutes: int | None = None,
    allday_reminder_hour: int | None = None,
) -> bytes:
    start, all_day = _format_ical_datetime(str(suggestion["starts_at"]), timezone_name)
    raw_end = suggestion.get("ends_at")
    end = None
    if raw_end:
        end, end_is_date = _format_ical_datetime(str(raw_end), timezone_name)
        if end_is_date != all_day:
            raise ValueError(
                _("Beginn und Ende müssen beide ganztägig oder beide mit Uhrzeit sein.")
            )
    rule = suggestion.get("recurrence")
    if not all_day:
        years = {int(start[:4])}
        if end:
            years.add(int(end[:4]))
        if rule:
            # Zeitzonenregeln bis zum Serienende mitliefern (offene Serien: 5 Jahre).
            last = (
                recurrence.last_occurrence(rule, datetime.strptime(start[:8], "%Y%m%d").date())
                if recurrence.is_finite(rule)
                else None
            )
            years.add(last.year if last else int(start[:4]) + 5)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Family Calendar Assistant//EN",
        "CALSCALE:GREGORIAN",
        *(
            _vtimezone_component(timezone_name, years)
            if not all_day
            else []
        ),
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{start}"
        if all_day
        else f"DTSTART;TZID={_escape_ical_text(timezone_name)}:{start}",
        f"SUMMARY:{_escape_ical_text(str(suggestion['title']))}",
    ]

    if raw_end:
        if all_day:
            if date.fromisoformat(str(raw_end)) < date.fromisoformat(
                str(suggestion["starts_at"])
            ):
                raise ValueError(_("Das Terminende liegt vor dem Beginn."))
            end_date = date.fromisoformat(str(raw_end)) + timedelta(days=1)
            end = end_date.strftime("%Y%m%d")
            lines.append(f"DTEND;VALUE=DATE:{end}")
        else:
            start_local = datetime.strptime(start, "%Y%m%dT%H%M%S")
            end_local = datetime.strptime(end, "%Y%m%dT%H%M%S")
            local_zone = ZoneInfo(timezone_name)
            if end_local.replace(tzinfo=local_zone) < start_local.replace(
                tzinfo=local_zone
            ):
                raise ValueError(_("Das Terminende liegt vor dem Beginn."))
            lines.append(f"DTEND;TZID={_escape_ical_text(timezone_name)}:{end}")
    elif all_day:
        end_date = date.fromisoformat(str(suggestion["starts_at"])) + timedelta(days=1)
        lines.append(f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}")
    else:
        # iCloud lehnt Termine mit Uhrzeit, aber ohne DTEND ab (HTTP 404).
        # Ohne bekanntes Ende wird deshalb die eingestellte Standarddauer
        # angenommen. Die Rechnung über UTC bleibt bei Zeitumstellungen gültig.
        local_zone = ZoneInfo(timezone_name)
        start_local = datetime.strptime(start, "%Y%m%dT%H%M%S").replace(
            tzinfo=local_zone
        )
        default_end = (
            start_local.astimezone(timezone.utc)
            + timedelta(minutes=default_event_minutes)
        ).astimezone(local_zone)
        lines.append(
            f"DTEND;TZID={_escape_ical_text(timezone_name)}:"
            f"{default_end.strftime('%Y%m%dT%H%M%S')}"
        )

    if rule:
        lines.extend(
            recurrence.ical_lines(
                rule,
                all_day=all_day,
                timezone_name=timezone_name,
                start_time=start[9:] if not all_day else "",
            )
        )
    location = suggestion.get("location")
    if location:
        lines.append(f"LOCATION:{_escape_ical_text(str(location))}")
    notes = suggestion.get("notes")
    if notes:
        lines.append(f"DESCRIPTION:{_escape_ical_text(str(notes))}")
    trigger = alarm_trigger(
        suggestion.get("reminder"), all_day, reminder_minutes, allday_reminder_hour
    )
    if trigger:
        lines.extend(
            [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape_ical_text(str(suggestion['title']))}",
                f"TRIGGER:{trigger}",
                "END:VALARM",
            ]
        )
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(_fold_ical_line(line) for line in lines).encode("utf-8")


def _send_icloud_event(
    base_url: str,
    username: str,
    app_password: str,
    timeout_seconds: int,
    timezone_name: str,
    calendar_name: str,
    suggestion: dict[str, object],
    overwrite_existing: bool,
    default_event_minutes: int = 60,
    reminder_minutes: int | None = None,
    allday_reminder_hour: int | None = None,
) -> str:
    calendars = discover_icloud_calendars(
        base_url, username, app_password, timeout_seconds
    )
    matches = [calendar for calendar in calendars if calendar["name"] == calendar_name]
    if len(matches) != 1:
        raise ValueError(
            _("Der iCloud-Kalender „{calendar}“ wurde nicht eindeutig gefunden.", calendar=calendar_name)
        )
    calendar = matches[0]
    if calendar["write_access"] is not True:
        raise ValueError(
            _("iCloud meldet keine bestätigte Schreibberechtigung für „{calendar}“.", calendar=calendar_name)
        )

    suggestion_id = int(suggestion["id"])
    uid = event_uid(suggestion_id)
    filename = sha256(uid.encode("utf-8")).hexdigest() + ".ics"
    calendar_url = str(calendar["calendar_url"])
    event_url = urljoin(calendar_url.rstrip("/") + "/", filename)
    event_data = _build_icalendar(
        suggestion,
        uid,
        timezone_name,
        default_event_minutes,
        reminder_minutes=reminder_minutes,
        allday_reminder_hour=allday_reminder_hour,
    )

    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10))
    with httpx.Client(
        auth=(username, app_password),
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "FamilyCalendarAssistant/0.1"},
    ) as client:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if not overwrite_existing:
            headers["If-None-Match"] = "*"
        response = client.put(
            event_url,
            headers=headers,
            content=event_data,
        )
    if response.status_code == 412:
        logger.info(
            "CalDAV-Termin für Vorschlag %s „%s“ existiert bereits.",
            suggestion_id,
            suggestion["title"],
        )
        return "already_exists"
    if response.status_code not in (200, 201, 204):
        raise httpx.HTTPStatusError(
            f"CalDAV event creation returned HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    result = "updated" if overwrite_existing else "created"
    logger.info(
        "CalDAV-Termin für Vorschlag %s „%s“ in Kalender „%s“ %s.",
        suggestion_id,
        suggestion["title"],
        calendar_name,
        "aktualisiert" if overwrite_existing else "angelegt",
    )
    return result


def create_icloud_event(
    base_url: str,
    username: str,
    app_password: str,
    timeout_seconds: int,
    timezone_name: str,
    calendar_name: str,
    suggestion: dict[str, object],
    *,
    default_event_minutes: int = 60,
    reminder_minutes: int | None = None,
    allday_reminder_hour: int | None = None,
) -> str:
    return _send_icloud_event(
        base_url,
        username,
        app_password,
        timeout_seconds,
        timezone_name,
        calendar_name,
        suggestion,
        overwrite_existing=False,
        default_event_minutes=default_event_minutes,
        reminder_minutes=reminder_minutes,
        allday_reminder_hour=allday_reminder_hour,
    )


def resend_icloud_event(
    base_url: str,
    username: str,
    app_password: str,
    timeout_seconds: int,
    timezone_name: str,
    calendar_name: str,
    suggestion: dict[str, object],
    *,
    default_event_minutes: int = 60,
    reminder_minutes: int | None = None,
    allday_reminder_hour: int | None = None,
) -> str:
    return _send_icloud_event(
        base_url,
        username,
        app_password,
        timeout_seconds,
        timezone_name,
        calendar_name,
        suggestion,
        overwrite_existing=True,
        default_event_minutes=default_event_minutes,
        reminder_minutes=reminder_minutes,
        allday_reminder_hour=allday_reminder_hour,
    )


def delete_icloud_event(
    base_url: str,
    username: str,
    app_password: str,
    timeout_seconds: int,
    calendar_name: str,
    suggestion: dict[str, object],
) -> str:
    calendars = discover_icloud_calendars(
        base_url, username, app_password, timeout_seconds
    )
    matches = [calendar for calendar in calendars if calendar["name"] == calendar_name]
    if len(matches) != 1:
        raise ValueError(
            _("Der iCloud-Kalender „{calendar}“ wurde nicht eindeutig gefunden.", calendar=calendar_name)
        )
    calendar = matches[0]
    if calendar["write_access"] is not True:
        raise ValueError(
            _("iCloud meldet keine bestätigte Schreibberechtigung für „{calendar}“.", calendar=calendar_name)
        )

    uid = event_uid(int(suggestion["id"]))
    filename = sha256(uid.encode("utf-8")).hexdigest() + ".ics"
    event_url = urljoin(str(calendar["calendar_url"]).rstrip("/") + "/", filename)
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10))
    with httpx.Client(
        auth=(username, app_password),
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "FamilyCalendarAssistant/0.1"},
    ) as client:
        response = client.delete(event_url)
    if response.status_code in {404, 410}:
        return "already_absent"
    if response.status_code not in {200, 202, 204}:
        raise httpx.HTTPStatusError(
            f"CalDAV event deletion returned HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    return "deleted"


# --- Lesen und Zurückschreiben einzelner Kalenderressourcen (Kalender-Backup) ---

_ETAG_PROPFIND = f"""<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="{_DAV_NS}">
  <d:prop><d:getetag /><d:resourcetype /></d:prop>
</d:propfind>"""


def open_client(username: str, app_password: str, timeout_seconds: int) -> httpx.Client:
    return httpx.Client(
        auth=(username, app_password),
        timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10)),
        follow_redirects=True,
        headers={"User-Agent": "FamilyCalendarAssistant/0.1"},
    )


def _href_path(value: str) -> str:
    return urlsplit(value).path


def list_event_etags(client: httpx.Client, calendar_url: str) -> dict[str, str]:
    """Alle Termin-Ressourcen eines Kalenders mit ETag (nur Metadaten, klein)."""
    response = client.request(
        "PROPFIND",
        calendar_url,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        content=_ETAG_PROPFIND.encode("utf-8"),
    )
    if response.status_code not in (200, 207):
        raise httpx.HTTPStatusError(
            f"CalDAV PROPFIND returned HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as error:
        raise ValueError("iCloud returned an invalid XML response") from error
    collection_path = _href_path(calendar_url).rstrip("/")
    etags: dict[str, str] = {}
    for item in _responses(root):
        href = _href(item)
        if not href or _href_path(href).rstrip("/") == collection_path:
            continue
        resource_type = _property_value(item, "resourcetype")
        if resource_type is not None and any(
            _local_name(child) == "collection" for child in resource_type.iter()
        ):
            continue
        etag = _property_value(item, "getetag")
        if etag is not None and etag.text:
            etags[_href_path(href)] = etag.text.strip()
    return etags


def fetch_events(
    client: httpx.Client, calendar_url: str, hrefs: list[str], batch_size: int = 100
) -> dict[str, tuple[str, str]]:
    """Lädt Termine gesammelt per calendar-multiget: {href: (etag, iCalendar)}."""
    result: dict[str, tuple[str, str]] = {}
    for start in range(0, len(hrefs), batch_size):
        batch = hrefs[start : start + batch_size]
        href_xml = "".join(
            f"<d:href>{escape(href)}</d:href>" for href in batch
        )
        body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            f'<c:calendar-multiget xmlns:d="{_DAV_NS}" xmlns:c="{_CALDAV_NS}">'
            "<d:prop><d:getetag /><c:calendar-data /></d:prop>"
            f"{href_xml}</c:calendar-multiget>"
        )
        response = client.request(
            "REPORT",
            calendar_url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=body.encode("utf-8"),
        )
        if response.status_code not in (200, 207):
            raise httpx.HTTPStatusError(
                f"CalDAV REPORT returned HTTP {response.status_code}",
                request=response.request,
                response=response,
            )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as error:
            raise ValueError("iCloud returned an invalid XML response") from error
        for item in _responses(root):
            href = _href(item)
            etag = _property_value(item, "getetag")
            data = _property_value(item, "calendar-data")
            if href and etag is not None and data is not None and data.text:
                result[_href_path(href)] = ((etag.text or "").strip(), data.text)
    return result


def restore_event_resource(
    client: httpx.Client, calendar_url: str, filename: str, ics: str
) -> str:
    """Schreibt eine gesicherte Termin-Version zurück (neu anlegen oder überschreiben)."""
    event_url = urljoin(calendar_url.rstrip("/") + "/", filename)
    current = client.get(event_url)
    headers = {"Content-Type": "text/calendar; charset=utf-8"}
    if current.status_code == 404:
        headers["If-None-Match"] = "*"
        result = "created"
    elif current.status_code == 200:
        etag = current.headers.get("ETag")
        if etag:
            headers["If-Match"] = etag
        result = "overwritten"
    else:
        raise httpx.HTTPStatusError(
            f"CalDAV GET returned HTTP {current.status_code}",
            request=current.request,
            response=current,
        )
    response = client.put(event_url, headers=headers, content=ics.encode("utf-8"))
    if response.status_code not in (200, 201, 204):
        raise httpx.HTTPStatusError(
            f"CalDAV restore returned HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    return result

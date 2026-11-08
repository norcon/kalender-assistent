"""Über die Weboberfläche änderbare Einstellungen.

Nur Verhaltensparameter. Zugangsdaten und Server-Adressen stehen verschlüsselt
in connections.py und sind nur mit APP_PASSWORD änderbar (Einstellungen →
Verbindungen) – sonst könnte jeder im Netz alle Mails umleiten.
"""

import logging
from dataclasses import dataclass, replace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .i18n import _

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Parameter:
    key: str
    group: str
    label: str
    help: str
    kind: str = "int"  # "int", "text", "timezone" oder "choice"
    unit: str = ""
    factor: int = 1  # gespeicherter Wert = Eingabe * factor
    minimum: int | None = None
    maximum: int | None = None
    max_length: int = 200
    choices: tuple[tuple[str, str], ...] = ()  # (Wert, Beschriftung) bei kind="choice"


PARAMETERS: tuple[Parameter, ...] = (
    Parameter(
        "language", "Oberfläche", "Sprache",
        "Standardsprache der Oberfläche. „Automatisch“ folgt der Sprache des "
        "Browsers; jedes Gerät kann zusätzlich oben im Menü umschalten.",
        kind="choice", choices=(("auto", "Automatisch"), ("de", "Deutsch"), ("en", "English")),
    ),
    Parameter(
        "imap_poll_seconds", "Postfach", "Abrufintervall ohne Sofort-Erkennung",
        "Wie oft das Postfach durchsucht wird, solange keine IMAP-IDLE-Verbindung "
        "besteht (Server unterstützt es nicht, Verbindung unterbrochen).",
        unit="Minuten", factor=60, minimum=1, maximum=1440,
    ),
    Parameter(
        "imap_idle_poll_seconds", "Postfach", "Abrufintervall mit Sofort-Erkennung",
        "Bei aktiver IMAP-IDLE-Verbindung meldet der Server neue E-Mails sofort; "
        "der regelmäßige Abruf ist dann nur eine Absicherung und kann seltener laufen.",
        unit="Minuten", factor=60, minimum=1, maximum=1440,
    ),
    Parameter(
        "imap_folder", "Postfach", "IMAP-Ordner",
        "Ordner, der durchsucht wird. Bei einem Wechsel werden im neuen Ordner "
        "zunächst nur E-Mails des Rückblick-Zeitraums berücksichtigt.",
        kind="text", max_length=200,
    ),
    Parameter(
        "imap_initial_lookback_days", "Postfach", "Rückblick beim ersten Abruf",
        "Wie weit beim allerersten Abruf eines Ordners zurückgeschaut wird.",
        unit="Tage", minimum=1, maximum=365,
    ),
    Parameter(
        "imap_max_message_bytes", "Postfach", "Maximale E-Mail-Größe",
        "Größere E-Mails (inklusive Anhänge) werden übersprungen.",
        unit="MB", factor=1024 * 1024, minimum=1, maximum=100,
    ),
    Parameter(
        "ai_model", "KI-Auswertung", "KI-Modell",
        "Name des Modells am KI-Server (Adresse unter Einstellungen → Verbindungen).",
        kind="text", max_length=200,
    ),
    Parameter(
        "ai_timeout_seconds", "KI-Auswertung", "Zeitlimit pro E-Mail",
        "Maximale Wartezeit auf die Antwort der KI für eine E-Mail.",
        unit="Minuten", factor=60, minimum=1, maximum=120,
    ),
    Parameter(
        "uncertain_threshold_percent", "Vorschläge", "Schwelle für unsichere Vorschläge",
        "Vorschläge, bei denen die KI weniger sicher ist, werden farbig markiert "
        "(0 = nie markieren).",
        unit="%", minimum=0, maximum=100,
    ),
    Parameter(
        "newsletter_min_confidence_percent", "Vorschläge", "Mindest-Sicherheit bei Newslettern",
        "Mails mit Abmelde-Link im Mail-Kopf (Newsletter, Werbung): Vorschläge mit "
        "geringerer KI-Sicherheit landen unter „Ausgeblendet“. Ausgenommen sind "
        "Absender, von denen schon etwas übertragen wurde (0 = aus).",
        unit="%", minimum=0, maximum=100,
    ),
    Parameter(
        "default_event_minutes", "Kalender", "Standarddauer ohne Endzeit",
        "Termine mit Uhrzeit, aber ohne erkanntes Ende, werden mit dieser Dauer "
        "nach iCloud übertragen.",
        unit="Minuten", minimum=5, maximum=1440,
    ),
    Parameter(
        "reminder_minutes", "Kalender", "Erinnerung bei Terminen mit Uhrzeit",
        "So viele Minuten vor Beginn erinnern alle Geräte, die den Kalender sehen "
        "(0 = keine Erinnerung). Gilt für neu übertragene Termine.",
        unit="Minuten vorher", minimum=0, maximum=10080,
    ),
    Parameter(
        "allday_reminder_hour", "Kalender", "Erinnerung bei ganztägigen Terminen",
        "Uhrzeit am Vortag (24 = keine Erinnerung).",
        unit="Uhr am Vortag", minimum=0, maximum=24,
    ),
    Parameter(
        "caldav_timezone", "Kalender", "Zeitzone",
        "Zeitzone für Zeitangaben ohne ausdrückliche Zeitzone, z. B. Europe/Berlin.",
        kind="timezone", max_length=64,
    ),
    Parameter(
        "caldav_timeout_seconds", "Kalender", "Zeitlimit für iCloud",
        "Maximale Wartezeit auf eine Antwort von iCloud.",
        unit="Sekunden", minimum=5, maximum=300,
    ),
    Parameter(
        "calendar_backup_minutes", "Kalender-Sicherung", "Intervall",
        "Wie oft die iCloud-Kalender gesichert werden. Übertragen und gespeichert "
        "werden nur Änderungen. Dieselben Daten nutzt die Prüfung auf bereits "
        "vorhandene iCloud-Termine.",
        unit="Minuten", minimum=5, maximum=1440,
    ),
    Parameter(
        "calendar_keep_days", "Kalender-Sicherung", "Aufbewahrung",
        "Wie weit man auf frühere Stände zurückgreifen kann.",
        unit="Tage", minimum=1, maximum=365,
    ),
    Parameter(
        "notify_suggestions", "Benachrichtigungen", "Neue Vorschläge",
        "Nach jedem Abruf mit neuen Terminvorschlägen eine Nachricht senden.",
        kind="choice", choices=(("1", "An"), ("0", "Aus")),
    ),
    Parameter(
        "notify_detail", "Benachrichtigungen", "Inhalt der Nachricht",
        "„Mit Titeln“ nennt Termin und Datum. „Nur Anzahl“ verrät nichts über die "
        "Termine – sinnvoll, wenn die Nachricht über fremde Server läuft (z. B. ntfy.sh).",
        kind="choice", choices=(("titel", "Mit Titeln"), ("anzahl", "Nur Anzahl")),
    ),
    Parameter(
        "notify_outages", "Benachrichtigungen", "Störungen",
        "Melden, wenn Postfach, KI-Server oder iCloud länger gestört sind (und wenn es "
        "wieder läuft) sowie bei auffällig vielen gelöschten Kalenderterminen.",
        kind="choice", choices=(("1", "An"), ("0", "Aus")),
    ),
    Parameter(
        "notify_outage_minutes", "Benachrichtigungen", "Störung melden nach",
        "So lange muss eine Störung bestehen, bevor eine Nachricht kommt.",
        unit="Minuten", minimum=10, maximum=1440,
    ),
    Parameter(
        "notify_updates", "Benachrichtigungen", "Neue Version",
        "Einmal melden, wenn auf GitHub eine neue Version bereitsteht.",
        kind="choice", choices=(("1", "An"), ("0", "Aus")),
    ),
    Parameter(
        "backup_hour", "Backup", "Uhrzeit der täglichen Sicherung",
        "Volle Stunde in der eingestellten Zeitzone. Wurde die Sicherung "
        "verpasst (z. B. Server aus), wird sie später am Tag nachgeholt.",
        unit="Uhr", minimum=0, maximum=23,
    ),
    Parameter(
        "backup_keep_days", "Backup", "Aufbewahrung",
        "Ältere Sicherungen werden automatisch gelöscht; die neueste bleibt immer erhalten.",
        unit="Tage", minimum=1, maximum=365,
    ),
)
PARAMETERS_BY_KEY = {parameter.key: parameter for parameter in PARAMETERS}


def display_value(parameter: Parameter, settings: Settings) -> str:
    value = getattr(settings, parameter.key)
    if parameter.kind == "int":
        scaled = value / parameter.factor
        return str(int(scaled)) if scaled == int(scaled) else f"{scaled:g}"
    return str(value)


def parse_value(parameter: Parameter, raw: str) -> Any:
    """Prüft eine Eingabe und gibt den zu speichernden Wert zurück."""
    value = raw.strip()
    if parameter.kind == "int":
        try:
            number = int(value)
        except ValueError as error:
            raise ValueError(_("Bitte eine ganze Zahl eingeben.")) from error
        if parameter.minimum is not None and number < parameter.minimum:
            raise ValueError(_("Mindestens {value} {unit}.", value=parameter.minimum, unit=_(parameter.unit)).replace(" .", "."))
        if parameter.maximum is not None and number > parameter.maximum:
            raise ValueError(_("Höchstens {value} {unit}.", value=parameter.maximum, unit=_(parameter.unit)).replace(" .", "."))
        return number * parameter.factor
    if parameter.kind == "choice":
        if value not in {choice for choice, _label in parameter.choices}:
            raise ValueError(_("Bitte einen der angebotenen Werte wählen."))
        return value
    if not value:
        raise ValueError(_("Darf nicht leer sein."))
    if len(value) > parameter.max_length:
        raise ValueError(_("Höchstens {count} Zeichen.", count=parameter.max_length))
    if any(ord(character) < 32 for character in value):
        raise ValueError(_("Enthält ungültige Steuerzeichen."))
    if parameter.kind == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(_("Unbekannte Zeitzone, z. B. Europe/Berlin verwenden.")) from error
    return value


def parse_form(form: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for parameter in PARAMETERS:
        if parameter.key not in form:
            continue
        try:
            values[parameter.key] = parse_value(parameter, form[parameter.key])
        except ValueError as error:
            errors[parameter.key] = str(error)
    return values, errors


def apply_overrides(base: Settings, stored: dict[str, str]) -> Settings:
    """Legt gespeicherte Werte über die .env-Werte; ungültige werden ignoriert."""
    changes: dict[str, Any] = {}
    for key, raw in stored.items():
        parameter = PARAMETERS_BY_KEY.get(key)
        if parameter is None:
            continue
        try:
            if parameter.kind == "int":
                number = int(raw)
                parse_value(parameter, str(number // parameter.factor))
                if number % parameter.factor:
                    raise ValueError("kein Vielfaches der Einheit")
                changes[key] = number
            else:
                changes[key] = parse_value(parameter, raw)
        except ValueError as error:
            logger.warning(
                "Gespeicherte Einstellung %s wird ignoriert (%s).", key, error
            )
    return replace(base, **changes)

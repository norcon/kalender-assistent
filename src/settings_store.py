"""Über die Weboberfläche änderbare Einstellungen.

Nur Verhaltensparameter. Zugangsdaten und Server-Adressen stehen verschlüsselt
in connections.py und sind nur mit APP_PASSWORD änderbar (Einstellungen →
Verbindungen) – sonst könnte jeder im Netz alle Mails umleiten.

Werte für diese Parameter in `.env` (aus älteren Versionen oder vom
Einrichtungsassistenten) übernimmt `absorb_env` beim Start in die Datenbank;
danach entfernt der Updater sie aus `.env`.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
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
    Parameter(
        "update_check", "System", "Nach neuen Versionen suchen",
        "Fragt einmal täglich die öffentliche Versionsliste bei GitHub ab. Dabei "
        "werden keine Daten aus der App übertragen.",
        kind="choice", choices=(("1", "An"), ("0", "Aus")),
    ),
    Parameter(
        "log_level", "System", "Ausführlichkeit der Protokolle",
        "„Fehlersuche“ schreibt deutlich mehr in die Logs (docker compose logs).",
        kind="choice", choices=(("INFO", "Normal"), ("DEBUG", "Fehlersuche"), ("WARNING", "Nur Warnungen")),
    ),
)
PARAMETERS_BY_KEY = {parameter.key: parameter for parameter in PARAMETERS}


def display_value(parameter: Parameter, settings: Settings) -> str:
    value = getattr(settings, parameter.key)
    if isinstance(value, bool):
        return "1" if value else "0"
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
                if isinstance(getattr(base, key), bool):
                    changes[key] = changes[key] == "1"
        except ValueError as error:
            logger.warning(
                "Gespeicherte Einstellung %s wird ignoriert (%s).", key, error
            )
    return replace(base, **changes)


# .env-Name je Parameter (Benachrichtigungs-Parameter gab es nie in .env)
ENV_NAMES = {
    parameter.key: "APP_LANGUAGE" if parameter.key == "language" else parameter.key.upper()
    for parameter in PARAMETERS if not parameter.key.startswith("notify_")
}
_TRUE = {"1", "true", "yes", "ja"}
# Nur-.env-Schalter: entfernbar, solange sie den eingebauten Standard wiederholen
DEFAULT_ONLY = {
    "APP_DATABASE_PATH": "/data/calendar-assistant.sqlite3",
    "DEMO_MODE": "0",
    "TARGET_CALENDARS": "",
}
ENV_HASHES_STATE = "env_settings_hashes"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stored_text(parameter: Parameter, value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(int(value)) if parameter.kind == "int" else str(value)


def absorb_env(database_path: str, environ: dict[str, str] | Any, *, targets_set: bool) -> tuple[list[str], dict[str, str]]:
    """Einstellungswerte aus .env in die Datenbank übernehmen.

    Ein Wert gilt als neu, wenn er sich seit der letzten Übernahme geändert hat;
    ein in der Oberfläche geänderter Wert wird von einem unveränderten alten
    .env-Wert nie überschrieben. Rückgabe: (übernommene Parameter,
    {.env-Schlüssel: Prüfsumme} der Zeilen, die aus .env entfernt werden dürfen).
    """
    current = Settings.from_env(environ=environ)
    defaults = Settings.from_env(environ={})
    stored = db.get_app_settings(database_path)
    state = db.get_service_state(database_path)
    try:
        hashes: dict[str, str] = json.loads(state.get(ENV_HASHES_STATE) or "{}")
    except ValueError:
        hashes = {}
    absorbed: dict[str, str] = {}
    removable: dict[str, str] = {}
    for key, name in ENV_NAMES.items():
        if name not in environ:
            continue
        raw = environ[name]
        digest = _digest(raw)
        parameter = PARAMETERS_BY_KEY[key]
        value = getattr(current, key)
        known = name in hashes
        changed = known and hashes[name] != digest
        if raw.strip() and (changed or (not known and key not in stored and value != getattr(defaults, key))):
            text = _stored_text(parameter, value)
            try:
                if parameter.kind == "int":
                    if value % parameter.factor:
                        raise ValueError("kein Vielfaches der Einheit")
                    parse_value(parameter, str(value // parameter.factor))
                else:
                    parse_value(parameter, text)
            except ValueError:
                logger.warning("Wert für %s in .env ist ungültig und bleibt dort.", name)
                continue
            absorbed[key] = text
        hashes[name] = digest
        removable[name] = digest
    for name, default in DEFAULT_ONLY.items():
        if name not in environ:
            continue
        raw = environ[name]
        if raw.strip() in {"", default} or (name == "TARGET_CALENDARS" and targets_set):
            removable[name] = _digest(raw)
    if absorbed:
        db.save_app_settings(database_path, absorbed)
        logger.info("Einstellungen aus .env übernommen: %s.", ", ".join(ENV_NAMES[key] for key in absorbed))
    db.set_service_state(database_path, **{ENV_HASHES_STATE: json.dumps(hashes, sort_keys=True)})
    return list(absorbed), removable

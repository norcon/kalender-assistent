"""Einrichtungsassistent: Verbindungen testen und die Werte für .env erzeugen.

Läuft nur im Einrichtungsmodus (Postfach-Zugangsdaten fehlen). Geschrieben wird
.env über den Updater; ohne Updater zeigt der Assistent die Zeilen zum Kopieren.
"""

import imaplib
import re
import ssl
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import ai, caldav
from .i18n import _

SAMPLE_DE = (
    "Liebe Eltern,\n\nder Elternabend der Klasse 4b findet am {weekday}, {date} um "
    "19:30 Uhr in Raum 12 statt. Bitte bringen Sie die Unterlagen zur Klassenfahrt mit.\n\n"
    "Außerdem: Ihre Bestellung wurde versendet und kommt voraussichtlich morgen.\n\n"
    "Viele Grüße\nIhre Klassenleitung"
)
SAMPLE_EN = (
    "Dear parents,\n\nthe parents' evening for class 4b takes place on {weekday}, {date} at "
    "7:30 pm in room 12. Please bring the school trip forms.\n\n"
    "Also: your order has been shipped and should arrive tomorrow.\n\n"
    "Kind regards\nYour class teacher"
)
_WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class SetupError(Exception):
    """Verständliche Meldung für den Assistenten."""


def imap_test(host: str, port: int, username: str, password: str, folder: str) -> list[str]:
    """Anmelden, Ordner prüfen, Ordnerliste zurückgeben (nur lesend)."""
    try:
        context = ssl.create_default_context()
        with imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=20) as client:
            client.login(username, password)
            status, data = client.list()
            folders = []
            for item in data or []:
                line = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
                # Letztes Element der LIST-Zeile: "Name mit Leerzeichen" oder Name
                match = re.search(r'(?:"((?:[^"\\]|\\.)*)"|([^\s"]+))\s*$', line)
                if match:
                    folders.append(match.group(1) or match.group(2))
            status, _data = client.select(folder or "INBOX", readonly=True)
            if status != "OK":
                raise SetupError(_("Der Ordner „{folder}“ wurde nicht gefunden.", folder=folder))
            return sorted(set(folders), key=str.casefold)
    except imaplib.IMAP4.error as error:
        raise SetupError(_("Anmeldung am Postfach fehlgeschlagen. Benutzer und Passwort prüfen.")) from error
    except (OSError, ssl.SSLError) as error:
        raise SetupError(_("Der Postfach-Server ist nicht erreichbar ({error}). Adresse und Port prüfen.", error=type(error).__name__)) from error


# Voreinstellungen für bekannte KI-Anbieter (OpenAI-kompatible Schnittstelle).
# (Schlüssel, Name, Adresse, lokal?, Hinweis) – None bei „lokal?“ = unbekannt.
AI_PROVIDERS: list[tuple[str, str, str, bool | None, str]] = [
    ("lmstudio", "LM Studio", "http://host.docker.internal:1234/v1", True,
     "Läuft lokal auf deinem Rechner oder Server. In LM Studio den Server starten und ein Modell laden; "
     "für Fotos und Scans ein Modell mit Bilderkennung (Vision). API-Schlüssel leer lassen."),
    ("ollama", "Ollama", "http://host.docker.internal:11434/v1", True,
     "Läuft lokal. Das Modell vorher mit „ollama pull“ laden, für Fotos und Scans ein Modell mit "
     "Bilderkennung. API-Schlüssel leer lassen."),
    ("openai", "OpenAI (ChatGPT)", "https://api.openai.com/v1", False,
     "API-Schlüssel auf platform.openai.com unter „API keys“ anlegen (Guthaben nötig). Ein günstiges "
     "Modell mit Bilderkennung genügt, z. B. eines der „mini“-Modelle."),
    ("anthropic", "Anthropic (Claude)", "https://api.anthropic.com/v1", False,
     "API-Schlüssel in der Claude Console (platform.claude.com) unter „API Keys“ anlegen. Günstig und "
     "mit Bilderkennung: claude-haiku-5-5."),
    ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", False,
     "API-Schlüssel in Google AI Studio (aistudio.google.com) anlegen. Günstig und mit Bilderkennung: "
     "ein Modell der „Flash“-Reihe. Achtung: Im kostenlosen Kontingent darf Google die Daten auswerten."),
    ("mistral", "Mistral AI", "https://api.mistral.ai/v1", False,
     "Anbieter aus der EU. API-Schlüssel unter console.mistral.ai anlegen; für Fotos und Scans ein "
     "Modell mit Bilderkennung wählen, z. B. Mistral Small oder Medium."),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", False,
     "Ein Schlüssel für Modelle vieler Anbieter (openrouter.ai), abgerechnet je Anfrage."),
    ("other", "Anderer Server", "", None,
     "Jeder Server mit OpenAI-kompatibler Schnittstelle, z. B. vLLM, LocalAI oder ein Proxy. "
     "Die Adresse endet meist auf /v1."),
]


def _ai_headers(base_url: str, api_key: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if api_key and "api.anthropic.com" in base_url:
        # Die Modellliste von Anthropic erwartet den nativen Schlüssel-Header.
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    return headers


# Modelle, die keine Chat-Antworten liefern (OpenAI listet alles auf einmal).
_NOT_CHAT = re.compile(r"embed|tts|whisper|dall-e|moderation|transcribe|realtime", re.IGNORECASE)


def ai_models(base_url: str, api_key: str) -> list[str]:
    headers = _ai_headers(base_url, api_key)
    try:
        params = {"limit": 1000} if "api.anthropic.com" in base_url else None
        response = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, params=params, timeout=15)
        response.raise_for_status()
        models = [
            str(item.get("id")).removeprefix("models/")  # Gemini liefert „models/…“
            for item in response.json().get("data", [])
            if item.get("id") and not _NOT_CHAT.search(str(item.get("id")))
        ]
    except (httpx.HTTPError, ValueError, AttributeError) as error:
        raise SetupError(_("Der KI-Server antwortet nicht wie erwartet ({error}). Adresse prüfen – sie endet meist auf /v1.", error=type(error).__name__)) from error
    if not models:
        raise SetupError(_("Der KI-Server meldet keine Modelle. Bitte zuerst ein Modell laden."))
    return sorted(models, key=str.casefold)


def sample_mail(language: str, today: date | None = None) -> tuple[str, str]:
    day = (today or date.today()) + timedelta(days=7)
    if language == "en":
        return "Parents' evening class 4b", SAMPLE_EN.format(weekday=_WEEKDAYS_EN[day.weekday()], date=day.strftime("%d %B %Y"))
    return "Elternabend Klasse 4b", SAMPLE_DE.format(weekday=_WEEKDAYS_DE[day.weekday()], date=day.strftime("%d.%m.%Y"))


def ai_probe(base_url: str, api_key: str, model: str, calendars: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    subject, text = sample_mail(language)
    try:
        return ai.extract_events(
            base_url.rstrip("/"), api_key, model, 300, subject, "schule@example.org",
            date.today().isoformat(), f"Nachrichtentext:\n{text}", calendars=calendars,
        )
    except Exception as error:  # noqa: BLE001 – jede Ursache verständlich melden
        raise SetupError(_("Die Probe-Auswertung ist fehlgeschlagen ({error}). Modell und Server prüfen.", error=type(error).__name__)) from error


def caldav_test(url: str, username: str, password: str, auth_mode: str | None = None) -> list[dict[str, Any]]:
    try:
        calendars = caldav.discover_icloud_calendars(url, username, password, 30, auth_mode=auth_mode)
    except httpx.HTTPStatusError as error:
        raise SetupError(_("Der Kalender-Server hat die Anmeldung abgelehnt (HTTP {status}). Benutzer, Passwort und Adresse prüfen.", status=error.response.status_code)) from error
    except (httpx.HTTPError, ValueError) as error:
        raise SetupError(_("Der Kalender-Server ist nicht erreichbar oder antwortet nicht wie erwartet ({error}). Adresse prüfen.", error=type(error).__name__)) from error
    return [{"name": c["name"], "color": c.get("color"), "write_access": c.get("write_access")} for c in calendars]


def valid_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


# ---------- .env ----------

def quote_env_value(value: str) -> str:
    """Wie im Updater: Schreibweise, die Docker Compose unverändert übernimmt."""
    if any(ord(character) < 32 for character in value):
        raise ValueError("control characters")
    if re.fullmatch(r"[A-Za-z0-9._:/@,+-]*", value):
        return value
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{escaped}"'


def env_values(form: dict[str, Any]) -> dict[str, str]:
    values = {
        "IMAP_HOST": form.get("imap_host", ""),
        "IMAP_PORT": str(form.get("imap_port") or 993),
        "IMAP_USERNAME": form.get("imap_username", ""),
        "IMAP_PASSWORD": form.get("imap_password", ""),
        "IMAP_FOLDER": form.get("imap_folder") or "INBOX",
        "AI_BASE_URL": form.get("ai_base_url", ""),
        "AI_API_KEY": form.get("ai_api_key", ""),
        "AI_MODEL": form.get("ai_model", ""),
        "CALDAV_URL": form.get("caldav_url") or "https://caldav.icloud.com",
        "CALDAV_USERNAME": form.get("caldav_username", ""),
        "CALDAV_APP_PASSWORD": form.get("caldav_password", ""),
        "CALDAV_TIMEZONE": form.get("timezone") or "Europe/Berlin",
        "CALDAV_NAME": form.get("caldav_name", ""),
        "APP_LANGUAGE": form.get("language") or "auto",
        "APP_PASSWORD": form.get("app_password", ""),
    }
    if form.get("edit_without_password") and not form.get("app_password"):
        # nur wenn gewählt – ältere Updater kennen den Schlüssel nicht
        values["CONNECTIONS_EDIT_WITHOUT_PASSWORD"] = "1"
    secret = ("IMAP_PASSWORD", "CALDAV_APP_PASSWORD", "AI_API_KEY", "APP_PASSWORD")
    return {key: str(value).strip() if key not in secret else str(value) for key, value in values.items()}


def env_text(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={quote_env_value(value)}" for key, value in values.items() if value != "") + "\n"

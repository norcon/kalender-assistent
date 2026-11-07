import os
from dataclasses import dataclass, replace
from typing import Mapping
from urllib.parse import urlsplit


# Verbindungsdaten: werden aus .env in die verschlüsselte Ablage übernommen (siehe connections.py)
_MAILBOX_FIELDS = ("HOST", "PORT", "USERNAME", "PASSWORD", "NAME")
CONNECTION_KEYS: tuple[str, ...] = (
    *(f"IMAP_{field}" for field in _MAILBOX_FIELDS),
    *(f"IMAP{number}_{field}" for number in range(2, 10) for field in (*_MAILBOX_FIELDS, "FOLDER")),
    "AI_BASE_URL", "AI_API_KEY",
    "CALDAV_URL", "CALDAV_USERNAME", "CALDAV_APP_PASSWORD", "CALDAV_NAME", "CALDAV_AUTH",
    "APP_URL", "NTFY_URL", "NTFY_TOKEN", "PUSHOVER_TOKEN", "PUSHOVER_USER",
    "HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN", "HOMEASSISTANT_SERVICE",
    "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM",
    "NOTIFY_EMAIL_TO",
)

KNOWN_SERVICES = {
    "icloud.com": "iCloud",
    "fastmail.com": "Fastmail",
    "mailbox.org": "mailbox.org",
    "posteo.de": "Posteo",
    "gmx.net": "GMX",
    "web.de": "WEB.DE",
}


def extra_mailboxes_from_env(getenv=os.getenv) -> tuple:
    """IMAP2_* bis IMAP9_*: weitere Postfächer oder Ordner (unvollständige werden ignoriert)."""
    boxes = []
    for number in range(2, 10):
        prefix = f"IMAP{number}_"
        host = getenv(prefix + "HOST", "").strip()
        username = getenv(prefix + "USERNAME", "").strip()
        password = getenv(prefix + "PASSWORD", "")
        if not (host and username and password):
            continue
        boxes.append({
            "host": host,
            "port": int(getenv(prefix + "PORT", "993") or 993),
            "username": username,
            "password": password.strip("\r\n"),
            "folder": getenv(prefix + "FOLDER", "").strip() or "INBOX",
            "name": getenv(prefix + "NAME", "").strip(),
        })
    return tuple(boxes)


def service_name_for(url: str) -> str:
    """Anzeigename aus der CalDAV-Adresse: bekannte Dienste, sonst der Hostname."""
    host = (urlsplit(url.strip()).hostname or "").lower()
    if not host:
        return "iCloud"  # Standard-Adresse
    for domain, name in KNOWN_SERVICES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


@dataclass(frozen=True)
class Settings:
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    imap_folder: str
    imap_poll_seconds: int
    imap_initial_lookback_days: int
    imap_max_message_bytes: int
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    ai_timeout_seconds: int
    caldav_url: str
    caldav_username: str
    caldav_app_password: str
    caldav_timeout_seconds: int
    caldav_timezone: str
    database_path: str
    log_level: str
    default_event_minutes: int = 60
    backup_dir: str = "/backups"
    backup_hour: int = 3
    backup_keep_days: int = 14
    uncertain_threshold_percent: int = 60
    calendar_backup_minutes: int = 60
    reminder_minutes: int = 60
    allday_reminder_hour: int = 18
    calendar_keep_days: int = 30
    imap_idle_poll_seconds: int = 1800
    newsletter_min_confidence_percent: int = 95
    demo_mode: bool = False
    setup_mode: bool = False  # Zugangsdaten fehlen → nur der Einrichtungsassistent läuft
    mail_configured: bool = True  # Postfach-Zugangsdaten vorhanden (oder Demo)
    language: str = "auto"
    update_repository: str = "norcon/kalender-assistent"
    updater_url: str = "http://updater:8000"
    update_check: bool = True
    app_password: str = ""  # leer = kein Zugangsschutz
    app_url: str = ""  # Adresse für Links in Benachrichtigungen
    imap_name: str = ""  # Anzeigename des (ersten) Postfachs
    extra_mailboxes: tuple = ()  # weitere Postfächer aus IMAP2_* … IMAP9_*
    caldav_name: str = "iCloud"  # Anzeigename des Kalenderdienstes
    caldav_auth: str = "basic"  # basic oder digest
    ntfy_url: str = ""
    ntfy_token: str = ""
    pushover_token: str = ""
    pushover_user: str = ""
    homeassistant_url: str = ""
    homeassistant_token: str = ""
    homeassistant_service: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"  # starttls, ssl oder none
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email_to: str = ""
    notify_suggestions: str = "1"
    notify_outages: str = "1"
    notify_updates: str = "1"
    notify_detail: str = "titel"  # titel oder anzahl
    notify_outage_minutes: int = 60
    target_calendars: str = ""  # Erstbelegung der Zielkalender, z. B. "Alex,Sam,Familie"

    def mailboxes(self) -> list["Settings"]:
        """Erstes Postfach und alle weiteren – jeweils als eigene Einstellungen."""
        boxes = [self]
        for box in self.extra_mailboxes:
            boxes.append(replace(
                self, imap_host=box["host"], imap_port=box["port"], imap_username=box["username"],
                imap_password=box["password"], imap_folder=box["folder"], imap_name=box["name"],
                extra_mailboxes=(),
            ))
        return boxes

    @property
    def mailbox_label(self) -> str:
        return self.imap_name or f"{self.imap_username} · {self.imap_folder}"

    @property
    def calendar_store_path(self) -> str:
        return os.path.join(self.backup_dir, "calendar-history.sqlite3")

    @classmethod
    def from_env(cls, connections: Mapping[str, str] | None = None) -> "Settings":
        """Einstellungen aus der Umgebung; mit `connections` kommen Verbindungsdaten nur von dort."""

        def getenv(name: str, default: str = "") -> str:
            if connections is not None and name in CONNECTION_KEYS:
                return connections.get(name, default)
            return os.getenv(name, default)

        demo = os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "ja"}

        imap = {name: getenv(name, "").strip() for name in ("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD")}
        # Ohne Postfach-Zugangsdaten startet die App im Einrichtungsmodus (Assistent).
        setup = not demo and not all(imap.values())

        def required(name: str) -> str:
            return getenv(name, "")

        return cls(
            demo_mode=demo,
            setup_mode=setup,
            mail_configured=demo or all(imap.values()),
            language=getenv("APP_LANGUAGE", "auto").strip().lower() or "auto",
            update_repository=getenv("UPDATE_REPOSITORY", "norcon/kalender-assistent").strip(),
            updater_url=getenv("UPDATER_URL", "http://updater:8000").strip(),
            app_password=getenv("APP_PASSWORD", "").strip("\r\n"),
            app_url=getenv("APP_URL", "").strip(),
            caldav_name=getenv("CALDAV_NAME", "").strip() or service_name_for(getenv("CALDAV_URL", "")),
            caldav_auth=(getenv("CALDAV_AUTH", "basic").strip().lower() or "basic"),
            ntfy_url=getenv("NTFY_URL", "").strip(),
            ntfy_token=getenv("NTFY_TOKEN", "").strip(),
            pushover_token=getenv("PUSHOVER_TOKEN", "").strip(),
            pushover_user=getenv("PUSHOVER_USER", "").strip(),
            homeassistant_url=getenv("HOMEASSISTANT_URL", "").strip(),
            homeassistant_token=getenv("HOMEASSISTANT_TOKEN", "").strip(),
            homeassistant_service=getenv("HOMEASSISTANT_SERVICE", "").strip(),
            smtp_host=getenv("SMTP_HOST", "").strip(),
            smtp_port=int(getenv("SMTP_PORT", "587") or 587),
            smtp_security=(getenv("SMTP_SECURITY", "starttls").strip().lower() or "starttls"),
            smtp_username=getenv("SMTP_USERNAME", "").strip(),
            smtp_password=getenv("SMTP_PASSWORD", "").strip("\r\n"),
            smtp_from=getenv("SMTP_FROM", "").strip(),
            notify_email_to=getenv("NOTIFY_EMAIL_TO", "").strip(),
            update_check=getenv("UPDATE_CHECK", "1").strip().lower() not in {"0", "false", "no", "nein"},
            target_calendars=getenv("TARGET_CALENDARS", "").strip(),
            imap_host=required("IMAP_HOST"),
            imap_port=int(getenv("IMAP_PORT", "993")),
            imap_username=required("IMAP_USERNAME"),
            imap_password=required("IMAP_PASSWORD"),
            imap_folder=getenv("IMAP_FOLDER", "INBOX"),
            imap_name=getenv("IMAP_NAME", "").strip(),
            extra_mailboxes=extra_mailboxes_from_env(getenv),
            imap_poll_seconds=max(30, int(getenv("IMAP_POLL_SECONDS", "300"))),
            imap_idle_poll_seconds=max(
                30, int(getenv("IMAP_IDLE_POLL_SECONDS", "1800"))
            ),
            imap_initial_lookback_days=max(
                1, int(getenv("IMAP_INITIAL_LOOKBACK_DAYS", "14"))
            ),
            imap_max_message_bytes=max(
                1024, int(getenv("IMAP_MAX_MESSAGE_BYTES", "10485760"))
            ),
            ai_base_url=getenv("AI_BASE_URL", "http://host.docker.internal:1234/v1")
            .rstrip("/"),
            ai_api_key=getenv("AI_API_KEY", ""),
            ai_model=getenv("AI_MODEL", "local-model"),
            ai_timeout_seconds=max(5, int(getenv("AI_TIMEOUT_SECONDS", "1800"))),
            caldav_url=getenv("CALDAV_URL", "https://caldav.icloud.com").rstrip("/"),
            caldav_username=getenv("CALDAV_USERNAME", "").strip(),
            caldav_app_password=getenv("CALDAV_APP_PASSWORD", ""),
            caldav_timeout_seconds=max(5, int(getenv("CALDAV_TIMEOUT_SECONDS", "30"))),
            caldav_timezone=getenv("CALDAV_TIMEZONE", "Europe/Berlin").strip(),
            database_path=getenv(
                "APP_DATABASE_PATH", "/data/calendar-assistant.sqlite3"
            ),
            log_level=getenv("LOG_LEVEL", "INFO").upper(),
            default_event_minutes=min(
                1440, max(5, int(getenv("DEFAULT_EVENT_MINUTES", "60")))
            ),
            backup_dir=getenv("APP_BACKUP_DIR", "/backups"),
            backup_hour=min(23, max(0, int(getenv("BACKUP_HOUR", "3")))),
            backup_keep_days=max(1, int(getenv("BACKUP_KEEP_DAYS", "14"))),
            calendar_backup_minutes=min(
                1440, max(5, int(getenv("CALENDAR_BACKUP_MINUTES", "60")))
            ),
            calendar_keep_days=max(1, int(getenv("CALENDAR_KEEP_DAYS", "30"))),
            reminder_minutes=min(10080, max(0, int(getenv("REMINDER_MINUTES", "60")))),
            allday_reminder_hour=min(
                24, max(0, int(getenv("ALLDAY_REMINDER_HOUR", "18")))
            ),
            newsletter_min_confidence_percent=min(
                100, max(0, int(getenv("NEWSLETTER_MIN_CONFIDENCE_PERCENT", "95")))
            ),
            uncertain_threshold_percent=min(
                100, max(0, int(getenv("UNCERTAIN_THRESHOLD_PERCENT", "60")))
            ),
        )

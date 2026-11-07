import os
from dataclasses import dataclass


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
    target_calendars: str = ""  # Erstbelegung der Zielkalender, z. B. "Alex,Sam,Familie"

    @property
    def calendar_store_path(self) -> str:
        return os.path.join(self.backup_dir, "calendar-history.sqlite3")

    @classmethod
    def from_env(cls) -> "Settings":
        demo = os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "ja"}

        imap = {name: os.getenv(name, "").strip() for name in ("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD")}
        # Ohne Postfach-Zugangsdaten startet die App im Einrichtungsmodus (Assistent).
        setup = not demo and not all(imap.values())

        def required(name: str) -> str:
            return os.getenv(name, "")

        return cls(
            demo_mode=demo,
            setup_mode=setup,
            mail_configured=demo or all(imap.values()),
            language=os.getenv("APP_LANGUAGE", "auto").strip().lower() or "auto",
            update_repository=os.getenv("UPDATE_REPOSITORY", "norcon/kalender-assistent").strip(),
            updater_url=os.getenv("UPDATER_URL", "http://updater:8000").strip(),
            app_password=os.getenv("APP_PASSWORD", "").strip("\r\n"),
            update_check=os.getenv("UPDATE_CHECK", "1").strip().lower() not in {"0", "false", "no", "nein"},
            target_calendars=os.getenv("TARGET_CALENDARS", "").strip(),
            imap_host=required("IMAP_HOST"),
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            imap_username=required("IMAP_USERNAME"),
            imap_password=required("IMAP_PASSWORD"),
            imap_folder=os.getenv("IMAP_FOLDER", "INBOX"),
            imap_poll_seconds=max(30, int(os.getenv("IMAP_POLL_SECONDS", "300"))),
            imap_idle_poll_seconds=max(
                30, int(os.getenv("IMAP_IDLE_POLL_SECONDS", "1800"))
            ),
            imap_initial_lookback_days=max(
                1, int(os.getenv("IMAP_INITIAL_LOOKBACK_DAYS", "14"))
            ),
            imap_max_message_bytes=max(
                1024, int(os.getenv("IMAP_MAX_MESSAGE_BYTES", "10485760"))
            ),
            ai_base_url=os.getenv("AI_BASE_URL", "http://host.docker.internal:1234/v1")
            .rstrip("/"),
            ai_api_key=os.getenv("AI_API_KEY", ""),
            ai_model=os.getenv("AI_MODEL", "local-model"),
            ai_timeout_seconds=max(5, int(os.getenv("AI_TIMEOUT_SECONDS", "1800"))),
            caldav_url=os.getenv("CALDAV_URL", "https://caldav.icloud.com").rstrip("/"),
            caldav_username=os.getenv("CALDAV_USERNAME", "").strip(),
            caldav_app_password=os.getenv("CALDAV_APP_PASSWORD", ""),
            caldav_timeout_seconds=max(5, int(os.getenv("CALDAV_TIMEOUT_SECONDS", "30"))),
            caldav_timezone=os.getenv("CALDAV_TIMEZONE", "Europe/Berlin").strip(),
            database_path=os.getenv(
                "APP_DATABASE_PATH", "/data/calendar-assistant.sqlite3"
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            default_event_minutes=min(
                1440, max(5, int(os.getenv("DEFAULT_EVENT_MINUTES", "60")))
            ),
            backup_dir=os.getenv("APP_BACKUP_DIR", "/backups"),
            backup_hour=min(23, max(0, int(os.getenv("BACKUP_HOUR", "3")))),
            backup_keep_days=max(1, int(os.getenv("BACKUP_KEEP_DAYS", "14"))),
            calendar_backup_minutes=min(
                1440, max(5, int(os.getenv("CALENDAR_BACKUP_MINUTES", "60")))
            ),
            calendar_keep_days=max(1, int(os.getenv("CALENDAR_KEEP_DAYS", "30"))),
            reminder_minutes=min(10080, max(0, int(os.getenv("REMINDER_MINUTES", "60")))),
            allday_reminder_hour=min(
                24, max(0, int(os.getenv("ALLDAY_REMINDER_HOUR", "18")))
            ),
            newsletter_min_confidence_percent=min(
                100, max(0, int(os.getenv("NEWSLETTER_MIN_CONFIDENCE_PERCENT", "95")))
            ),
            uncertain_threshold_percent=min(
                100, max(0, int(os.getenv("UNCERTAIN_THRESHOLD_PERCENT", "60")))
            ),
        )

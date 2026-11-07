import asyncio
import json
import logging
import os
import shutil
import signal
import hashlib
import hmac
import secrets
import re
import threading
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from functools import partial
import time
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    Response,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import (
    version,
    backup,
    calendar_backup,
    caldav,
    db,
    formatting,
    health,
    mail,
    mail_idle,
    demo,
    i18n,
    notify,
    setup_wizard,
    updates,
    person_learning,
    recurrence,
    settings_store,
)
from .config import Settings
from .i18n import _

# Werte aus .env; gespeicherte Einstellungen der Weboberfläche liegen darüber.
_env_settings = Settings.from_env()
settings = _env_settings
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
# Filter lesen die aktuelle Zeitzone bei jedem Aufruf (Einstellungen sind änderbar).
templates.env.filters["event_time"] = lambda value: formatting.event_time(
    value, settings.caldav_timezone
)
templates.env.filters["event_range"] = lambda start, end: formatting.event_range(
    start, end, settings.caldav_timezone
)
templates.env.filters["timestamp"] = lambda value: formatting.timestamp(
    value, settings.caldav_timezone
)
templates.env.filters["relative_time"] = formatting.relative_time
templates.env.globals["app_version"] = version.VERSION
templates.env.globals["app_released"] = version.RELEASED
templates.env.globals["demo_mode"] = False  # wird in _reload_settings gesetzt
templates.env.globals["setup_mode"] = False
templates.env.globals["setup_skipped"] = False
SETUP_SKIPPED_KEY = "setup_skipped"
templates.env.globals["_"] = i18n.gettext
templates.env.globals["_n"] = i18n.ngettext
templates.env.globals["current_lang"] = i18n.current
templates.env.globals["languages"] = i18n.LANGUAGES
templates.env.filters["history_label"] = formatting.history_label
templates.env.filters["recurrence_text"] = recurrence.describe
templates.env.filters["sender_address"] = db.sender_address
templates.env.filters["sender_domain"] = db.sender_domain
templates.env.filters["filesize"] = formatting.file_size
templates.env.filters["duration"] = formatting.duration
def _calendar_names() -> list[str]:
    """Zielkalender, in die die App schreiben darf (Einstellungen → Zielkalender)."""
    return [item["name"] for item in db.list_target_calendars(settings.database_path)]
_mail_check_lock = asyncio.Lock()
_suggestion_lock = asyncio.Lock()
_activity_lock = threading.Lock()
_activity_entries: deque[dict[str, object]] = deque(maxlen=200)
_next_activity_id = 1
_check_state: dict[str, object] = {
    "status": "idle",
    "started_at": None,
    "completed_at": None,
}


class _ActivityLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _next_activity_id
        if not record.name.startswith(
            (
                "src.app",
                "src.mail",
                "src.backup",
                "src.calendar_backup",
                "src.mail_idle",
                "src.settings_store",
            )
        ):
            return
        try:
            message = record.getMessage()
            message = re.sub(
                r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[Adresse]", message
            )
            message = re.sub(
                r"(://[^:/\s]+:)[^@\s/]+@", r"\1[redacted]@", message
            )
            message = " ".join(message.split())[:500]
            with _activity_lock:
                _activity_entries.append(
                    {
                        "id": _next_activity_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "level": record.levelname,
                        "message": message,
                    }
                )
                _next_activity_id += 1
        except Exception:
            self.handleError(record)


logging.getLogger("src").addHandler(_ActivityLogHandler())


def _update_check_state(
    status: str, *, started_at: str | None = None, completed_at: str | None = None
) -> None:
    _check_state["status"] = status
    if started_at is not None:
        _check_state["started_at"] = started_at
    if completed_at is not None:
        _check_state["completed_at"] = completed_at


_mail_recheck_requested = False


def _remember_mail_result(**values: str) -> None:
    try:
        db.set_service_state(settings.database_path, **values)
    except Exception as error:  # Status ist Zusatzinfo, darf Abruf nicht stören
        logger.warning("Abrufstatus konnte nicht gespeichert werden: %s", error)


async def _perform_mail_check(trigger: str) -> bool:
    started_at = datetime.now(timezone.utc).isoformat()
    _update_check_state("running", started_at=started_at)
    logger.info("Postfachprüfung gestartet (%s).", trigger)
    try:
        before_id = await asyncio.to_thread(db.max_suggestion_id, settings.database_path)
    except Exception:  # noqa: BLE001 – nur für Benachrichtigungen
        before_id = None
    try:
        await asyncio.to_thread(mail.poll_mailbox, settings)
    except Exception as error:
        completed_at = datetime.now(timezone.utc).isoformat()
        _update_check_state("error", completed_at=completed_at)
        _remember_mail_result(
            mail_last_error=completed_at, mail_last_error_type=type(error).__name__
        )
        logger.error("Postfachprüfung fehlgeschlagen (%s).", type(error).__name__)
        return False

    completed_at = datetime.now(timezone.utc).isoformat()
    _update_check_state("success", completed_at=completed_at)
    _remember_mail_result(mail_last_success=completed_at)
    logger.info("Postfachprüfung abgeschlossen.")
    if before_id is not None:
        automatic = await _auto_export(before_id)
        await _notify_new_suggestions(before_id, automatic)
    return True


async def _auto_export(before_id: int) -> list[dict[str, Any]]:
    """Neue Vorschläge mit Auto-Übernahme-Regel direkt nach iCloud übertragen."""
    if not _caldav_configured() or settings.demo_mode:
        return []
    try:
        candidates = await asyncio.to_thread(
            db.auto_export_candidates, settings.database_path, before_id,
            datetime.now(ZoneInfo(settings.caldav_timezone)).date().isoformat(),
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Auto-Übernahme: Prüfung fehlgeschlagen (%s).", type(error).__name__)
        return []
    exported = []
    for item in candidates:
        try:
            async with _suggestion_lock:
                await _send_suggestion_to_icloud(item["id"], resend=False)
        except HTTPException as error:
            logger.warning("Auto-Übernahme von „%s“ nicht möglich: %s – bleibt als Vorschlag.", item["title"], error.detail)
            continue
        db.add_suggestion_history(
            settings.database_path, item["id"], "auto_exported",
            {"rule": item["rule"], "min_confidence": item["min_confidence"]},
        )
        logger.info(
            "„%s“ automatisch in „%s“ eingetragen (Regel %s, ab %d %%).",
            item["title"], item["person"], item["rule"], item["min_confidence"],
        )
        exported.append(item)
    return exported


# ---------- Benachrichtigungen ----------

def _in_language(function, *args, **kwargs):
    token = i18n.activate(settings.language if settings.language in i18n.LANGUAGES else "de")
    try:
        return function(*args, **kwargs)
    finally:
        i18n.reset(token)


async def _notify_new_suggestions(before_id: int, automatic: list[dict[str, Any]] | None = None) -> None:
    if settings.notify_suggestions != "1" or not notify.channels(settings):
        return
    automatic = automatic or []
    try:
        auto_ids = {item["id"] for item in automatic}
        items = [item for item in await asyncio.to_thread(db.new_open_suggestions, settings.database_path, before_id)
                 if item["id"] not in auto_ids]
        if not items and not automatic:
            return

        def text() -> tuple[str, str]:
            if items:
                title = i18n.ngettext("{count} neuer Terminvorschlag", "{count} neue Terminvorschläge", len(items))
            else:
                title = i18n.ngettext("{count} Termin automatisch eingetragen", "{count} Termine automatisch eingetragen", len(automatic))
            if settings.notify_detail == "anzahl":
                lines = [_("Zum Prüfen in der App öffnen.")]
                if items and automatic:
                    lines.insert(0, i18n.ngettext("{count} Termin automatisch eingetragen", "{count} Termine automatisch eingetragen", len(automatic)) + ".")
                return title, "\n".join(lines)
            lines = [f"• {item['title']} – {formatting.event_time(item['starts_at'], settings.caldav_timezone)}" for item in items[:8]]
            if len(items) > 8:
                lines.append(_("… und {count} weitere", count=len(items) - 8))
            if automatic:
                if items:
                    lines.append("")
                lines.append(_("Automatisch eingetragen:"))
                lines += [f"• {item['title']} → {item['person']}" for item in automatic[:8]]
            return title, "\n".join(lines)

        title, message = _in_language(text)
        await asyncio.to_thread(notify.send, settings, title, message)
    except Exception as error:  # noqa: BLE001 – Benachrichtigung darf den Abruf nicht stören
        logger.warning("Benachrichtigung über neue Vorschläge fehlgeschlagen: %s", type(error).__name__)


_outage_since: dict[str, float] = {}
OUTAGE_KEYS = {"mail": "Postfach", "ai": "KI-Server", "icloud": "iCloud"}


def _check_outages(now: float | None = None) -> None:
    """Länger andauernde Fehler melden – und die Entwarnung."""
    now = now if now is not None else time.monotonic()
    state = db.get_service_state(settings.database_path)
    items = {item["key"]: item for item in _in_language(_status_summary)}
    for key, label in OUTAGE_KEYS.items():
        item = items.get(key)
        flag = f"notified_outage_{key}"
        if item and item["state"] == "error":
            started = _outage_since.setdefault(key, now)
            if not state.get(flag) and now - started >= settings.notify_outage_minutes * 60:
                _in_language(lambda: notify.send(
                    settings, _("Störung: {name}", name=_(label)),
                    _("{name} ist seit über {minutes} Minuten gestört: {text}", name=_(label),
                      minutes=settings.notify_outage_minutes, text=item["text"]),
                    path="/protokoll", urgent=True,
                ))
                db.set_service_state(settings.database_path, **{flag: "1"})
        else:
            _outage_since.pop(key, None)
            if state.get(flag):
                _in_language(lambda: notify.send(
                    settings, _("Wieder in Ordnung: {name}", name=_(label)),
                    _("{name} funktioniert wieder.", name=_(label)),
                ))
                db.set_service_state(settings.database_path, **{flag: ""})


def _check_new_version() -> None:
    if not settings.update_check:
        return
    available = updates.releases(settings.update_repository)
    current = updates.parse_version(version.VERSION)
    if not available or not current or available[0]["key"] <= current:
        return
    newest = available[0]["version"]
    if db.get_service_state(settings.database_path).get("notified_version") == newest:
        return
    _in_language(lambda: notify.send(
        settings, _("Neue Version {version}", version=newest),
        _("Kalender-Assistent {version} ist verfügbar (installiert: {current}). Update unter Sicherungen → Version und Updates.",
          version=newest, current=version.VERSION),
        path="/sicherungen#version",
    ))
    db.set_service_state(settings.database_path, notified_version=newest)


async def _notification_worker() -> None:
    await asyncio.sleep(60)  # Start abwarten, Status soll erst ermittelt sein
    last_version_check = 0.0
    while True:
        try:
            if notify.channels(settings):
                if settings.notify_outages == "1":
                    await asyncio.to_thread(_check_outages)
                if settings.notify_updates == "1" and time.monotonic() - last_version_check > 6 * 3600:
                    last_version_check = time.monotonic()
                    await asyncio.to_thread(_check_new_version)
        except Exception as error:  # noqa: BLE001
            logger.warning("Prüfung für Benachrichtigungen fehlgeschlagen: %s", type(error).__name__)
        await asyncio.sleep(120)


async def _perform_with_followups(trigger: str) -> bool:
    """Abruf; wurde währenddessen eine neue Mail gemeldet, direkt nochmal."""
    global _mail_recheck_requested
    _mail_recheck_requested = False
    succeeded = await _perform_mail_check(trigger)
    while _mail_recheck_requested:
        _mail_recheck_requested = False
        succeeded = await _perform_mail_check("während des Abrufs neu gemeldet")
    return succeeded


async def _on_new_mail() -> None:
    global _mail_recheck_requested
    if _mail_check_lock.locked():
        _mail_recheck_requested = True
        logger.info("Neue E-Mail wird direkt nach dem laufenden Abruf geprüft.")
        return
    async with _mail_check_lock:
        await _perform_with_followups("neue E-Mail")


async def _run_scheduled_mail_check() -> None:
    if _mail_check_lock.locked():
        logger.info(
            "Automatische Postfachprüfung verschoben; ein anderer Abruf läuft bereits."
        )
        async with _mail_check_lock:
            pass
        return
    async with _mail_check_lock:
        await _perform_with_followups("automatisch")


_mail_worker_interval = [0]


def _poll_seconds() -> int:
    return health.effective_poll_seconds(
        settings, _idle_watcher.state if _idle_watcher else None
    )


async def _mail_worker() -> None:
    while True:
        await _run_scheduled_mail_check()
        # In kurzen Schritten warten, damit ein geändertes Intervall und ein
        # Ausfall der IDLE-Verbindung (→ wieder häufiger abrufen) sofort gelten.
        last_run = time.monotonic()
        interval = _poll_seconds()
        if interval != _mail_worker_interval[0]:
            if _mail_worker_interval[0]:
                logger.info(
                    "Automatischer Postfachabruf jetzt alle %d Minuten (%s).",
                    round(interval / 60),
                    "Sofort-Erkennung aktiv"
                    if health.idle_active(_idle_watcher.state if _idle_watcher else None)
                    else "ohne Sofort-Erkennung",
                )
            _mail_worker_interval[0] = interval
        while time.monotonic() - last_run < _poll_seconds():
            await asyncio.sleep(5)


def _reload_settings() -> None:
    global settings
    settings = settings_store.apply_overrides(
        _env_settings, db.get_app_settings(_env_settings.database_path)
    )
    # Übersprungene Einrichtung: App läuft ohne Postfach, bis .env ergänzt ist.
    skipped = settings.setup_mode and bool(
        db.get_service_state(settings.database_path).get(SETUP_SKIPPED_KEY)
    )
    if skipped:
        settings = replace(settings, setup_mode=False)
    templates.env.globals["demo_mode"] = settings.demo_mode
    templates.env.globals["setup_mode"] = settings.setup_mode
    templates.env.globals["setup_skipped"] = skipped


async def _run_backup(*, prune: bool = True) -> Path:
    return await asyncio.to_thread(
        partial(
            backup.create_backup,
            settings.database_path,
            settings.backup_dir,
            settings.backup_keep_days,
            prune=prune,
        )
    )


async def _backup_worker() -> None:
    while True:
        try:
            local_now = datetime.now(ZoneInfo(settings.caldav_timezone))
            due = await asyncio.to_thread(
                backup.backup_due, settings.backup_dir, settings.backup_hour, local_now
            )
            if due:
                await _run_backup()
        except Exception as error:
            logger.error("Datenbanksicherung fehlgeschlagen: %s", error)
            await asyncio.sleep(3600)  # nicht im Minutentakt wiederholen
        await asyncio.sleep(300)


def _local_now() -> datetime:
    return datetime.now(ZoneInfo(settings.caldav_timezone)).replace(tzinfo=None)


def archive_past_suggestions() -> int:
    now_local = _local_now()
    ended = [
        item["id"]
        for item in db.list_suggestions(settings.database_path)
        if not item["keep_active"]
        and formatting.event_has_ended(
            item, now_local, settings.caldav_timezone, settings.default_event_minutes
        )
    ]
    archived = db.auto_archive_suggestions(settings.database_path, ended)
    if archived:
        logger.info(
            "%d vergangene(r) Terminvorschlag/-vorschläge automatisch archiviert.",
            archived,
        )
    return archived


async def _archive_worker() -> None:
    while True:
        try:
            async with _suggestion_lock:
                archive_past_suggestions()
        except Exception as error:
            logger.error("Automatisches Archivieren fehlgeschlagen: %s", error)
        await asyncio.sleep(60)


_calendar_backup_lock = asyncio.Lock()
EXCLUDED_CALENDARS_KEY = "calendar_backup_excluded"


def _caldav_configured() -> bool:
    return settings.demo_mode or bool(settings.caldav_username and settings.caldav_app_password)


def _excluded_calendars() -> set[str]:
    raw = db.get_app_settings(settings.database_path).get(EXCLUDED_CALENDARS_KEY)
    try:
        return set(json.loads(raw)) if raw else set()
    except (ValueError, TypeError):
        return set()


def _calendar_backup_sync() -> dict[str, object]:
    """Kalender ermitteln, ausgewählte sichern, Alte aufräumen (läuft im Thread)."""
    found = caldav.discover_icloud_calendars(
        settings.caldav_url,
        settings.caldav_username,
        settings.caldav_app_password,
        settings.caldav_timeout_seconds,
    )
    calendars = [
        {"name": str(item["name"]), "url": str(item["calendar_url"]), "color": item.get("color")}
        for item in found
    ]
    store = settings.calendar_store_path
    calendar_backup.remember_calendars(store, calendars)
    excluded = _excluded_calendars()
    selected = [item for item in calendars if item["name"] not in excluded]
    with caldav.open_client(
        settings.caldav_username,
        settings.caldav_app_password,
        settings.caldav_timeout_seconds,
    ) as client:
        result = calendar_backup.take_snapshot(
            store,
            selected,
            lambda url: caldav.list_event_etags(client, url),
            lambda url, hrefs: caldav.fetch_events(client, url, hrefs),
        )
    _check_mass_deletion(result)
    calendar_backup.prune(store, settings.calendar_keep_days)
    return result


MASS_DELETE_KEY = "mass_delete_alert"


def _check_mass_deletion(result: dict[str, Any]) -> None:
    """Fehlen plötzlich viele Termine, Warnung merken (Startseite, Sicherungen)."""
    hits = calendar_backup.mass_deletion(result)
    if not hits:
        return
    store = settings.calendar_store_path
    snapshot_id = int(result["snapshot_id"])
    alert = {
        "snapshot_id": snapshot_id,
        "before_id": calendar_backup.previous_snapshot_id(store, snapshot_id),
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "deleted": sum(hit["deleted"] for hit in hits),
        "calendars": hits,
    }
    db.set_service_state(settings.database_path, **{MASS_DELETE_KEY: json.dumps(alert, ensure_ascii=False)})
    logger.warning(
        "Kalendersicherung: auffällig viele Termine fehlen plötzlich (%s).",
        ", ".join(f"{hit['calendar']}: {hit['deleted']} von {hit['before']}" for hit in hits),
    )
    if settings.notify_outages == "1" and notify.channels(settings):
        _in_language(lambda: notify.send(
            settings, _("Auffällig viele Termine fehlen"),
            _("Bei der Kalendersicherung fehlten plötzlich {count} Termine ({calendars}). Wurde versehentlich etwas gelöscht?",
              count=alert["deleted"], calendars=", ".join(hit["calendar"] for hit in hits)),
            path=(_mass_alert() or {}).get("link") or "/sicherungen/kalender", urgent=True,
        ))


def _mass_alert() -> dict[str, Any] | None:
    raw = db.get_service_state(settings.database_path).get(MASS_DELETE_KEY)
    try:
        alert = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not alert or not alert.get("before_id"):
        return alert
    alert["link"] = "/sicherungen/kalender?" + urlencode(
        {"stand": alert["before_id"], "status": "gelöscht", "zeitraum": "alle"}
    )
    return alert


templates.env.globals["mass_alert"] = _mass_alert


async def _run_calendar_backup() -> dict[str, object]:
    async with _calendar_backup_lock:
        return await asyncio.to_thread(_calendar_backup_sync)


def _calendar_backup_due(now: datetime) -> bool:
    snapshots = calendar_backup.list_snapshots(settings.calendar_store_path, limit=1)
    if not snapshots:
        return True
    last = datetime.fromisoformat(str(snapshots[0]["taken_at"]))
    return now - last >= timedelta(minutes=settings.calendar_backup_minutes)


async def _calendar_backup_worker() -> None:
    try:
        await asyncio.to_thread(calendar_backup.backfill, settings.calendar_store_path)
    except Exception as error:
        logger.warning("Nachträgliche Auswertung der Kalendersicherung fehlgeschlagen: %s", error)
    await asyncio.sleep(20)  # Start nicht mit Netzwerkzugriffen ausbremsen
    while True:
        try:
            if (
                _caldav_configured()
                and not _calendar_backup_lock.locked()
                and await asyncio.to_thread(
                    _calendar_backup_due, datetime.now(timezone.utc)
                )
            ):
                await _run_calendar_backup()
        except Exception as error:
            logger.error(
                "Kalendersicherung fehlgeschlagen: %s",
                f"HTTP {error.response.status_code}"
                if isinstance(error, httpx.HTTPStatusError)
                else type(error).__name__,
            )
            await asyncio.sleep(600)  # nicht im Minutentakt wiederholen
        await asyncio.sleep(60)


_ai_state: dict[str, object] | None = None
_idle_watcher: mail_idle.IdleWatcher | None = None
_workers: dict[str, asyncio.Task] = {}
_heartbeat = time.monotonic()
_stop_threads = threading.Event()
HANG_SECONDS = 300


async def _ai_check_worker() -> None:
    global _ai_state
    while True:
        try:
            _ai_state = await asyncio.to_thread(health.check_ai_server, settings)
        except Exception as error:
            logger.warning("KI-Server-Prüfung fehlgeschlagen: %s", type(error).__name__)
        await asyncio.sleep(300)


def _start_idle_watcher(loop: asyncio.AbstractEventLoop) -> None:
    global _idle_watcher
    _idle_watcher = mail_idle.IdleWatcher(
        lambda: settings,
        lambda: asyncio.run_coroutine_threadsafe(_on_new_mail(), loop),
        _stop_threads,
    )
    threading.Thread(target=_idle_watcher.run, name="imap-idle", daemon=True).start()


def _hang_watchdog() -> None:
    """Eigener Thread: Reagiert die App minutenlang nicht, Prozess beenden.

    Docker startet den Container dank restart: unless-stopped neu.
    """
    while not _stop_threads.wait(30):
        if time.monotonic() - _heartbeat > HANG_SECONDS:
            logger.critical(
                "Die Anwendung reagiert seit über %d Sekunden nicht; Neustart.",
                HANG_SECONDS,
            )
            os._exit(1)


async def _heartbeat_worker() -> None:
    """Hält den Watchdog wach und erkennt ausgefallene Hintergrundprozesse."""
    global _heartbeat
    while True:
        _heartbeat = time.monotonic()
        dead = [name for name, task in _workers.items() if task.done()]
        if dead:
            logger.critical(
                "Hintergrundprozess ausgefallen (%s); Dienst wird neu gestartet.",
                ", ".join(dead),
            )
            os.kill(os.getpid(), signal.SIGTERM)
            return
        await asyncio.sleep(10)


def _start_watchdog() -> None:
    threading.Thread(target=_hang_watchdog, name="watchdog", daemon=True).start()


def _start_workers() -> None:
    """Startet die passenden Hintergrundprozesse, die noch nicht laufen."""
    workers = {
        "Datenbanksicherung": _backup_worker,
        "Archivierung": _archive_worker,
    }
    if not (settings.demo_mode or settings.setup_mode):  # Demo/Einrichtung: keine Verbindungen nach außen
        workers.update({
            "Kalendersicherung": _calendar_backup_worker,
            "KI-Prüfung": _ai_check_worker,
            "Benachrichtigungen": _notification_worker,
        })
        if settings.mail_configured:
            workers["Postfachabruf"] = _mail_worker
    for name, worker in workers.items():
        if name not in _workers:
            _workers[name] = asyncio.create_task(worker())
    if "Postfachabruf" in workers and _idle_watcher is None:
        _start_idle_watcher(asyncio.get_running_loop())


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _heartbeat, _idle_watcher
    db.initialize(settings.database_path)
    _reload_settings()
    if settings.demo_mode:
        demo_language = "en" if settings.language == "en" else "de"
        demo.install_fakes(demo_language)
        demo.seed(settings.database_path, settings.calendar_store_path, demo_language)
    adopted = db.seed_target_calendars(
        settings.database_path,
        calendar_backup.known_calendars(settings.calendar_store_path),
        settings.target_calendars.split(","),
    )
    if adopted:
        logger.info(
            "Zielkalender aus bisherigen Daten übernommen: %s. Anpassen unter "
            "Einstellungen → Zielkalender.",
            ", ".join(adopted),
        )
    _stop_threads.clear()
    _heartbeat = time.monotonic()
    _workers.clear()
    _idle_watcher = None
    _start_workers()
    heartbeat = asyncio.create_task(_heartbeat_worker())
    _start_watchdog()
    try:
        yield
    finally:
        _stop_threads.set()
        heartbeat.cancel()
        tasks = [*_workers.values(), heartbeat]
        _workers.clear()
        for worker in tasks:
            worker.cancel()
        for worker in tasks:
            try:
                await worker
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Kalender-Assistent", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent.parent / "static")),
    name="static",
)

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_same_origin_request(request: Request) -> bool:
    """Schützt vor CSRF: Die Oberfläche hat keinen Login, daher dürfen fremde
    Webseiten im Browser eines Heimnetz-Nutzers keine Aktionen auslösen."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in {"same-origin", "none"}
    origin = request.headers.get("origin")
    if origin is None:
        # Kein Browser-Kontext (z. B. curl) oder sehr alter Browser.
        return True
    return urlsplit(origin).netloc.casefold() == request.headers.get(
        "host", ""
    ).casefold()


@app.middleware("http")
async def reject_cross_site_requests(request: Request, call_next):
    if request.method in _UNSAFE_METHODS and not _is_same_origin_request(request):
        logger.warning(
            "Schreibende Anfrage einer fremden Webseite abgewiesen (%s %s).",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "Anfragen von fremden Webseiten sind nicht erlaubt."},
        )
    return await call_next(request)


@app.middleware("http")
async def select_language(request: Request, call_next):
    language = i18n.negotiate(
        request.cookies.get("lang"), settings.language, request.headers.get("accept-language")
    )
    token = i18n.activate(language)
    try:
        response = await call_next(request)
    finally:
        i18n.reset(token)
    response.headers.setdefault("Content-Language", language)
    response.headers["Vary"] = ", ".join(
        filter(None, [response.headers.get("Vary"), "Cookie, Accept-Language"])
    )
    return response


_SETUP_OPEN = ("/einrichtung", "/api/setup/", "/static/", "/i18n/", "/health", "/api/version")


@app.middleware("http")
async def setup_redirect(request: Request, call_next):
    if settings.setup_mode and request.method == "GET" and not request.url.path.startswith(_SETUP_OPEN):
        return RedirectResponse("/einrichtung", status_code=303)
    return await call_next(request)


# ---------- Zugangsschutz (optional, APP_PASSWORD in .env) ----------

SESSION_COOKIE = "ka_session"
SESSION_LONG = 30 * 24 * 3600
SESSION_SHORT = 12 * 3600
_AUTH_OPEN = ("/anmelden", "/static/", "/i18n/", "/health", "/api/version")
_login_failures: dict[str, tuple[int, float]] = {}  # IP → (Fehlversuche, gesperrt bis)
LOGIN_FAILURE_DELAY = 1.0  # Sekunden Pause nach jedem Fehlversuch


def _session_secret() -> bytes:
    state = db.get_service_state(settings.database_path)
    secret = state.get("session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        db.set_service_state(settings.database_path, session_secret=secret)
    # Das Passwort fließt mit ein: ein neues Passwort meldet alle Geräte ab.
    return hashlib.sha256(f"{secret}:{settings.app_password}".encode()).digest()


def _session_token(expires: int) -> str:
    signature = hmac.new(_session_secret(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def _valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires_text, _signature = token.split(".", 1)
    if not expires_text.isdigit() or int(expires_text) < time.time():
        return False
    return hmac.compare_digest(token, _session_token(int(expires_text)))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not settings.app_password or request.url.path.startswith(_AUTH_OPEN):
        return await call_next(request)
    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if request.method != "GET" or request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": _("Bitte zuerst anmelden.")})
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse("/anmelden?" + urlencode({"weiter": target}), status_code=303)


@app.get("/anmelden", response_class=HTMLResponse)
async def login_page(request: Request, weiter: str = Query(default="/", max_length=2000), fehler: str = ""):
    if not settings.app_password:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "weiter": _safe_next(weiter), "fehler": fehler,
    })


@app.post("/anmelden")
async def login_submit(request: Request):
    if not settings.app_password:
        return RedirectResponse("/", status_code=303)
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    password = (form.get("passwort") or [""])[-1]
    target = _safe_next((form.get("weiter") or ["/"])[-1])
    remember = bool((form.get("bleiben") or [""])[-1])
    ip = _client_ip(request)
    failures, locked_until = _login_failures.get(ip, (0, 0.0))
    now = time.monotonic()
    if locked_until > now:
        wait = int(locked_until - now) + 1
        return RedirectResponse("/anmelden?" + urlencode({"weiter": target, "fehler": _("Zu viele Fehlversuche. Bitte {seconds} Sekunden warten.", seconds=wait)}), status_code=303)
    if not hmac.compare_digest(password.encode(), settings.app_password.encode()):
        failures += 1
        # Ab dem 5. Fehlversuch zunehmend länger sperren (30 s, 60 s, … bis 15 Min.)
        lock = min(900, 30 * 2 ** (failures - 5)) if failures >= 5 else 0
        _login_failures[ip] = (failures, now + lock)
        await asyncio.sleep(LOGIN_FAILURE_DELAY)
        logger.warning("Fehlgeschlagene Anmeldung von %s (%d. Versuch).", ip, failures)
        return RedirectResponse("/anmelden?" + urlencode({"weiter": target, "fehler": _("Das Passwort stimmt nicht.")}), status_code=303)
    _login_failures.pop(ip, None)
    lifetime = SESSION_LONG if remember else SESSION_SHORT
    response = RedirectResponse(target, status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        SESSION_COOKIE, _session_token(int(time.time()) + lifetime),
        max_age=lifetime if remember else None, httponly=True, samesite="lax", secure=secure,
    )
    logger.info("Anmeldung von %s.", ip)
    return response


@app.post("/abmelden")
async def logout():
    response = RedirectResponse("/anmelden", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


templates.env.globals["login_required"] = lambda: bool(settings.app_password)
templates.env.globals["notify_channels"] = lambda: notify.channels(settings)


def _require_setup_mode() -> None:
    if not settings.setup_mode:
        raise HTTPException(status_code=404, detail=_("Die Einrichtung ist bereits abgeschlossen."))


class SetupImapRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=500)
    folder: str = Field(default="INBOX", max_length=200)


class SetupAiRequest(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    language: str = Field(default="de", max_length=5)
    calendars: list[dict] = Field(default_factory=list, max_length=30)


class SetupCaldavRequest(BaseModel):
    url: str = Field(default="https://caldav.icloud.com", max_length=500)
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=500)


class SetupFinishRequest(BaseModel):
    language: str = Field(default="auto", max_length=5)
    timezone: str = Field(default="Europe/Berlin", max_length=64)
    imap_host: str = Field(min_length=1, max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_username: str = Field(min_length=1, max_length=320)
    imap_password: str = Field(min_length=1, max_length=500)
    imap_folder: str = Field(default="INBOX", max_length=200)
    ai_base_url: str = Field(min_length=8, max_length=500)
    ai_api_key: str = Field(default="", max_length=500)
    ai_model: str = Field(min_length=1, max_length=200)
    caldav_url: str = Field(default="https://caldav.icloud.com", max_length=500)
    caldav_username: str = Field(default="", max_length=320)
    caldav_password: str = Field(default="", max_length=500)
    app_password: str = Field(default="", max_length=200)
    targets: list[dict] = Field(default_factory=list, max_length=30)


def _setup_error(error: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


@app.get("/einrichtung", response_class=HTMLResponse)
async def setup_page(request: Request):
    if not settings.setup_mode:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="setup.html", context={
        "timezones": sorted(zone for zone in zoneinfo.available_timezones() if "/" in zone),
        "default_timezone": settings.caldav_timezone,
    })


@app.get("/api/setup/state")
async def setup_state():
    return {"setup_mode": settings.setup_mode, "version": version.VERSION}


@app.post("/api/setup/skip")
async def setup_skip():
    """Assistent überspringen: Die App startet ohne Postfach; .env später ergänzen."""
    _require_setup_mode()
    db.set_service_state(settings.database_path, **{SETUP_SKIPPED_KEY: datetime.now(timezone.utc).isoformat()})
    _reload_settings()
    _start_workers()
    _status_cache.clear()
    logger.info("Einrichtungsassistent übersprungen; das Postfach ist nicht eingerichtet.")
    return {"ok": True}


@app.post("/api/setup/resume")
async def setup_resume():
    """Übersprungenen Assistenten wieder öffnen (nur solange das Postfach fehlt)."""
    if settings.demo_mode or settings.mail_configured:
        raise HTTPException(status_code=404, detail=_("Die Einrichtung ist bereits abgeschlossen."))
    db.set_service_state(settings.database_path, **{SETUP_SKIPPED_KEY: ""})
    _reload_settings()
    return {"ok": True}


@app.post("/api/setup/imap")
async def setup_imap(request: SetupImapRequest):
    _require_setup_mode()
    try:
        folders = await asyncio.to_thread(setup_wizard.imap_test, request.host.strip(), request.port,
                                          request.username.strip(), request.password, request.folder.strip() or "INBOX")
    except setup_wizard.SetupError as error:
        raise _setup_error(error) from error
    return {"ok": True, "folders": folders}


@app.post("/api/setup/ai-models")
async def setup_ai_models(request: SetupAiRequest):
    _require_setup_mode()
    try:
        models = await asyncio.to_thread(setup_wizard.ai_models, request.base_url.strip(), request.api_key)
    except setup_wizard.SetupError as error:
        raise _setup_error(error) from error
    return {"ok": True, "models": models}


@app.post("/api/setup/ai-probe")
async def setup_ai_probe(request: SetupAiRequest):
    _require_setup_mode()
    calendars = [
        {"name": str(c.get("name"))[:100], "description": str(c.get("description") or "")[:500], "shared": bool(c.get("shared"))}
        for c in request.calendars if c.get("name")
    ]
    try:
        events = await asyncio.to_thread(setup_wizard.ai_probe, request.base_url.strip(), request.api_key,
                                         request.model.strip(), calendars, request.language)
    except setup_wizard.SetupError as error:
        raise _setup_error(error) from error
    subject, text = setup_wizard.sample_mail(request.language)
    return {"ok": True, "sample": text, "events": [
        {"title": e["title"], "when": formatting.event_range(e["start"], e.get("end"), settings.caldav_timezone),
         "person": e.get("person"), "confidence": round(float(e.get("confidence", 0)) * 100)}
        for e in events
    ]}


@app.post("/api/setup/caldav")
async def setup_caldav(request: SetupCaldavRequest):
    _require_setup_mode()
    try:
        calendars = await asyncio.to_thread(setup_wizard.caldav_test, request.url.strip(),
                                            request.username.strip(), request.password)
    except setup_wizard.SetupError as error:
        raise _setup_error(error) from error
    return {"ok": True, "calendars": calendars}


@app.post("/api/setup/finish")
async def setup_finish(request: SetupFinishRequest):
    _require_setup_mode()
    if request.app_password and len(request.app_password) < 8:
        raise HTTPException(status_code=422, detail=_("Das Passwort muss mindestens 8 Zeichen haben."))
    if not setup_wizard.valid_timezone(request.timezone):
        raise HTTPException(status_code=422, detail=_("Unbekannte Zeitzone, z. B. Europe/Berlin verwenden."))
    if request.language not in ("auto", "de", "en"):
        raise HTTPException(status_code=422, detail=_("Unbekannte Sprache"))
    targets = [
        {"name": str(t.get("name")).strip()[:100], "description": str(t.get("description") or "")[:500], "shared": bool(t.get("shared"))}
        for t in request.targets if str(t.get("name") or "").strip()
    ]
    values = setup_wizard.env_values(request.model_dump())
    try:
        text = setup_wizard.env_text(values)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=_("Steuerzeichen oder Zeilenumbrüche sind nicht erlaubt.")) from error
    if targets:
        db.save_target_calendars(settings.database_path, targets)
    state = await asyncio.to_thread(updates.updater_status, settings.updater_url)
    if state is not None:
        ok, message = await asyncio.to_thread(updates.configure, settings.updater_url, values)
        if ok:
            logger.info("Ersteinrichtung abgeschlossen; Updater schreibt .env und startet die App neu.")
            return {"mode": "updater"}
        logger.warning("Updater hat die Ersteinrichtung abgelehnt: %s", message)
        return {"mode": "manual", "env": text, "reason": message}
    return {"mode": "manual", "env": text, "reason": ""}


@app.get("/i18n/{language}.js")
async def translations_script(language: str):
    if language not in i18n.LANGUAGES:
        raise HTTPException(status_code=404, detail=_("Unbekannte Sprache"))
    body = "window.I18N = " + json.dumps(i18n.catalog(language), ensure_ascii=False) + ";\n"
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


class SuggestionUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start: str = Field(min_length=10, max_length=40)
    end: str | None = Field(default=None, max_length=40)
    location: str | None = Field(default=None, max_length=2000)
    person: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)
    # Nur ändern, wenn ausdrücklich mitgeschickt (model_fields_set).
    recurrence: dict | None = None


class ReminderRequest(BaseModel):
    reminder: str | None = Field(default=None, max_length=10)


class ExportRequest(BaseModel):
    calendar: str | None = Field(default=None, max_length=50)


class ChangeRequestLink(BaseModel):
    suggestion_id: int = Field(gt=0)


def _suggestion_label(suggestion_id: int, title: object) -> str:
    """Vorschlag für Logmeldungen: Nummer und Titel."""
    return f"{suggestion_id} „{title}“"


def _validate_event_time(value: str, field: str) -> datetime:
    try:
        if len(value) == 10:
            return datetime.combine(datetime.fromisoformat(value).date(), datetime.min.time())
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(
            status_code=422, detail=_("{field} muss ein gültiges ISO-Datum sein.", field=_(field))
        ) from error


def _normalize_suggestion_value(field: str, value: object) -> object:
    if field not in {"start", "end"} or value is None:
        return value
    raw_value = str(value).strip()
    try:
        if len(raw_value) == 10:
            return ("date", datetime.fromisoformat(raw_value).date())
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return raw_value
    parsed = parsed.replace(second=0, microsecond=0)
    if parsed.tzinfo is not None:
        return ("datetime", parsed.astimezone(timezone.utc))
    return ("datetime", parsed)


def _suggestion_day(item: dict[str, object], today: date) -> date | None:
    """Tag des (nächsten) Termins in lokaler Zeit."""
    if item.get("recurrence"):
        return formatting.next_occurrence(item, today, settings.caldav_timezone)
    parsed = formatting.parse_event_value(item["starts_at"], settings.caldav_timezone)
    return parsed[0] if parsed else None


def _suggestion_interval(item: dict[str, object], day: date) -> tuple[datetime, datetime] | None:
    """Zeitraum des Vorschlags an diesem Tag (None bei ganztägigen)."""
    tz = settings.caldav_timezone
    start = formatting.parse_event_value(item["starts_at"], tz)
    if not start or start[1] is None:
        return None
    end = formatting.parse_event_value(item.get("ends_at"), tz) if item.get("ends_at") else None
    duration = (
        (end[1] - start[1]) if end and end[1] else timedelta(minutes=settings.default_event_minutes)
    )
    begin = datetime.combine(day, start[1].time())
    return begin, begin + max(duration, timedelta(0))


def _day_views(suggestions: list[dict[str, object]]) -> dict[int, dict[str, object]]:
    """Je Vorschlag: iCloud-Termine am selben Tag, Überschneidungen, Dubletten."""
    today = _local_now().date()
    views: dict[int, dict[str, object]] = {}
    cache: dict[date, list[dict[str, object]]] = {}
    for item in suggestions:
        if item["icloud_exported_at"]:
            continue
        day = _suggestion_day(item, today)
        if day is None:
            continue
        try:
            if day not in cache:
                cache[day] = calendar_backup.events_on_day(
                    settings.calendar_store_path, day, settings.caldav_timezone
                )
        except Exception as error:  # Zusatzinfo, Seite nie blockieren
            logger.warning("Tagesvorschau aus iCloud nicht möglich: %s", error)
            return {}
        own_uid = caldav.event_uid(int(item["id"]))
        interval = _suggestion_interval(item, day)
        events, matches, overlaps = [], [], 0
        for event in cache[day]:
            if event["uid"] == own_uid:
                continue
            overlap = bool(
                interval
                and not event["all_day"]
                and event["start"] < interval[1]
                and (event["end"] > interval[0] or (event["end"] == event["start"] and event["start"] >= interval[0]))
            )
            same_calendar = event["calendar"] == item.get("person")
            entry = {**event, "overlap": overlap, "same_calendar": same_calendar}
            events.append(entry)
            if overlap:
                overlaps += 1
            if person_learning.title_similarity(str(item["title"]), str(event["summary"])) >= 0.6:
                matches.append(entry)
        views[int(item["id"])] = {
            "day": day,
            "events": events,
            "overlaps": overlaps,
            "overlaps_same_calendar": sum(1 for e in events if e["overlap"] and e["same_calendar"]),
            "matches": matches,
        }
    return views


def _when_label(item: dict[str, object]) -> str:
    """„morgen“, „in 3 Tagen“ … – bei Serien für den nächsten Termin."""
    today = _local_now().date()
    if item.get("recurrence"):
        day = formatting.next_occurrence(item, today, settings.caldav_timezone)
    else:
        parsed = formatting.parse_event_value(item["starts_at"], settings.caldav_timezone)
        day = parsed[0] if parsed else None
    return formatting.relative_day(day, today)


HIDE_HINT_MIN_REMOVED = 3


def _hide_hints(suggestions: list[dict[str, object]]) -> dict[int, dict[str, object]]:
    """Karten, deren Absender-Domain du bisher nur entfernt hast."""
    stats = db.sender_stats(settings.database_path)
    rules = db.list_ignored_senders(settings.database_path, "hide") + db.list_ignored_senders(
        settings.database_path, "show"
    )
    hints: dict[int, dict[str, object]] = {}
    for item in suggestions:
        sender = str(item.get("source_sender") or "")
        domain = db.sender_domain(sender)
        entry = stats.get(domain)
        if (
            not entry
            or entry["exported"]
            or entry["removed"] < HIDE_HINT_MIN_REMOVED
            or any(db.sender_matches(sender, rule["pattern"]) for rule in rules)
        ):
            continue
        hints[int(item["id"])] = {"domain": domain, "removed": entry["removed"]}
    return hints


def _suggestion_context() -> dict[str, object]:
    everything = db.list_suggestions(settings.database_path)
    suggestions = [item for item in everything if not item.get("hidden_reason")]
    hidden = [item for item in everything if item.get("hidden_reason")]
    return {
        "hidden_suggestions": hidden,
        "hide_hints": _hide_hints(suggestions),
        "reminder_options": {
            item["id"]: formatting.reminder_options(
                item.get("reminder"),
                len(str(item["starts_at"])) == 10,
                settings.reminder_minutes,
                settings.allday_reminder_hour,
            )
            for item in suggestions
        },
        "newsletter_reason": db.NEWSLETTER_REASON,
        "day_views": _day_views(suggestions),
        "calendar_colors": calendar_backup.calendar_colors(settings.calendar_store_path),
        "calendar_snapshot": next(
            iter(calendar_backup.list_snapshots(settings.calendar_store_path, limit=1)),
            None,
        ),
        "suggestions": suggestions,
        "suggestion_groups": formatting.group_suggestions(
            suggestions, _local_now().date(), settings.caldav_timezone
        ),
        "history": {
            item["id"]: db.list_suggestion_history(settings.database_path, item["id"])
            for item in suggestions
        },
        "suggestion_version": db.get_suggestion_version(settings.database_path),
        "people": _calendar_names(),
        "caldav_configured": _caldav_configured(),
        "when_labels": {item["id"]: _when_label(item) for item in suggestions},
        "next_occurrences": {
            item["id"]: formatting.next_occurrence(
                item, _local_now().date(), settings.caldav_timezone
            )
            for item in suggestions
            if item.get("recurrence")
        },
        "uncertain_threshold": settings.uncertain_threshold_percent / 100,
        "default_event_minutes": settings.default_event_minutes,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            **_suggestion_context(),
            "change_requests": db.list_change_requests(settings.database_path),
            "linkable_suggestions": [
                item
                for item in db.list_suggestions(
                    settings.database_path, include_dismissed=True
                )
                if not item["cancelled_at"]
            ],
            "skipped_messages": db.list_skipped_messages(settings.database_path),
            "status_items": _status_summary(),
            "poll_seconds": _poll_seconds(),
        },
    )


@app.post("/api/check")
async def check_mailbox():
    if settings.demo_mode or settings.setup_mode:
        return JSONResponse(
            status_code=200,
            content={"status": "success", "message": _("Demo-Modus: es ist kein Postfach angebunden.")},
        )
    if not settings.mail_configured:
        return JSONResponse(
            status_code=409,
            content={"status": "error", "message": _("Das Postfach ist noch nicht eingerichtet.")},
        )
    if _mail_check_lock.locked():
        return JSONResponse(
            status_code=409,
            content={"status": "running", "message": _("Eine Prüfung läuft bereits.")},
        )
    async with _mail_check_lock:
        succeeded = await _perform_with_followups("manuell")
    return JSONResponse(
        status_code=200 if succeeded else 502,
        content={
            "status": "success" if succeeded else "error",
            "message": (
                _("Postfachprüfung abgeschlossen.")
                if succeeded
                else _("Prüfung fehlgeschlagen. Details stehen im Protokoll.")
            ),
        },
    )


_status_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}  # je Sprache


def _status_summary() -> list[dict[str, str]]:
    global _status_cache
    if settings.demo_mode:
        return [{
            "key": "demo", "label": i18n.gettext("Demo-Modus"), "state": "ok",
            "text": i18n.gettext("Beispieldaten – Postfach, KI und iCloud sind nicht angebunden"),
        }]
    cached = _status_cache.get(i18n.current())
    if cached and time.monotonic() - cached[0] < 5:
        return cached[1]
    try:
        snapshot = next(
            iter(calendar_backup.list_snapshots(settings.calendar_store_path, limit=1)),
            None,
        )
        items = health.status_items(
            settings,
            service_state=db.get_service_state(settings.database_path),
            check_state=dict(_check_state),
            ai_state=_ai_state,
            idle_state=_idle_watcher.state if _idle_watcher else None,
            last_calendar_snapshot=snapshot,
            caldav_configured=_caldav_configured(),
        )
    except Exception as error:
        logger.warning("Statusanzeige konnte nicht ermittelt werden: %s", error)
        items = []
    _status_cache[i18n.current()] = (time.monotonic(), items)
    return items


@app.get("/api/version")
async def app_version():
    """Für Seiten im Browser: neu laden, sobald eine neue Version läuft."""
    return JSONResponse(
        content={"version": version.VERSION},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
async def health_check():
    """Für Docker: 503 nur bei Fehlern, die ein Neustart beheben kann."""
    problems = []
    try:
        await asyncio.to_thread(db.get_suggestion_version, settings.database_path)
    except Exception as error:
        problems.append(f"Datenbank: {type(error).__name__}")
    dead = [name for name, task in _workers.items() if task.done()]
    if dead:
        problems.append("Hintergrundprozess ausgefallen: " + ", ".join(dead))
    status_code = 503 if problems else 200
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error" if problems else "ok",
            "problems": problems,
            "version": version.VERSION,
            "checks": {item["key"]: item["state"] for item in _status_summary()},
        },
    )


@app.get("/api/activity")
async def activity(after: int = Query(default=0, ge=0), include_logs: bool = True):
    with _activity_lock:
        entries = (
            [entry for entry in _activity_entries if entry["id"] > after]
            if include_logs
            else []
        )
        status = dict(_check_state)
    return {
        "status": status,
        "health": _status_summary(),
        "logs": entries[-100:],
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.post("/api/caldav/discover")
async def discover_caldav():
    if not _caldav_configured():
        raise HTTPException(
            status_code=503,
            detail=_(
                "CalDAV ist noch nicht konfiguriert. CALDAV_USERNAME und "
                "CALDAV_APP_PASSWORD müssen auf dem Server gesetzt sein."
            ),
        )
    try:
        calendars = await asyncio.to_thread(
            caldav.discover_icloud_calendars,
            settings.caldav_url,
            settings.caldav_username,
            settings.caldav_app_password,
            settings.caldav_timeout_seconds,
        )
    except httpx.HTTPStatusError as error:
        logger.error("CalDAV-Erkennung fehlgeschlagen: HTTP %s.", error.response.status_code)
        raise HTTPException(
            status_code=502,
            detail=_(
                "iCloud hat die Kalenderabfrage abgelehnt. Bitte Account, "
                "anwendungsspezifisches Passwort und Freigaben prüfen."
            ),
        ) from error
    except httpx.TimeoutException as error:
        logger.error("CalDAV-Erkennung mit Timeout fehlgeschlagen.")
        raise HTTPException(
            status_code=504,
            detail=_("Die iCloud-Kalenderabfrage hat das Zeitlimit überschritten."),
        ) from error
    except httpx.RequestError as error:
        logger.error("CalDAV-Netzwerkfehler: %s.", type(error).__name__)
        raise HTTPException(
            status_code=502,
            detail=_("iCloud konnte nicht erreicht werden. Netzwerk prüfen."),
        ) from error
    except ValueError as error:
        logger.error("CalDAV-Antwort konnte nicht verarbeitet werden: %s.", error)
        raise HTTPException(
            status_code=502,
            detail=_("iCloud lieferte keine verwertbare Kalenderliste."),
        ) from error
    calendar_backup.remember_calendars(
        settings.calendar_store_path,
        [
            {"name": c["name"], "url": c["calendar_url"], "color": c.get("color")}
            for c in calendars
            if c.get("calendar_url")
        ],
    )
    targets = set(_calendar_names())
    return {
        "calendars": [
            {
                "name": calendar["name"],
                "write_access": calendar["write_access"],
                "target": calendar["name"] in targets,
            }
            for calendar in calendars
        ]
    }


@app.get("/suggestions-fragment", response_class=HTMLResponse)
async def suggestions_fragment(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="suggestions.html",
        context=_suggestion_context(),
    )


@app.get("/change-requests-fragment", response_class=HTMLResponse)
async def change_requests_fragment(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="change-requests.html",
        context={
            "change_requests": db.list_change_requests(settings.database_path),
            "linkable_suggestions": [
                item
                for item in db.list_suggestions(
                    settings.database_path, include_dismissed=True
                )
                if not item["cancelled_at"]
            ],
        },
    )


async def _update_suggestion(suggestion_id: int, update: SuggestionUpdate):
    existing = db.get_suggestion(settings.database_path, suggestion_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))

    person = update.person.strip() if update.person else None
    allowed = _calendar_names()
    if person != existing["person"] and person not in allowed:
        raise HTTPException(
            status_code=422,
            detail=_("Bitte einen der Zielkalender wählen: {names}.", names=", ".join(allowed)),
        )

    title = update.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail=_("Der Titel darf nicht leer sein."))

    start = update.start.strip()
    start_value = _validate_event_time(start, "Beginn")
    end = update.end.strip() if update.end else None
    if end:
        end_value = _validate_event_time(end, "Ende")
        if (start_value.tzinfo is None) != (end_value.tzinfo is None):
            raise HTTPException(
                status_code=422,
                detail=_("Beginn und Ende müssen dieselbe Zeitzonenart verwenden."),
            )
        if end_value < start_value:
            raise HTTPException(
                status_code=422, detail=_("Das Ende darf nicht vor dem Beginn liegen.")
            )

    rule = existing["recurrence"]
    if "recurrence" in update.model_fields_set:
        try:
            rule = recurrence.normalize(update.recurrence, start_value.date())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    elif rule:
        # Beginn verschoben: Regel muss weiterhin passen (z. B. Wochentag).
        try:
            rule = recurrence.normalize(rule, start_value.date())
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail=_("{error} Bitte die Wiederholung anpassen.", error=error),
            ) from error

    values = {
        "title": title,
        "start": start,
        "end": end,
        "location": update.location.strip() if update.location else None,
        "person": person,
        "notes": update.notes.strip() if update.notes else None,
        "recurrence": rule,
    }
    values["previous_person"] = existing["person"]
    existing_values = {
        "title": existing["title"],
        "start": existing["starts_at"],
        "end": existing["ends_at"],
        "location": existing["location"],
        "person": existing["person"],
        "notes": existing["notes"],
        "recurrence": existing["recurrence"],
    }
    changed = any(
        _normalize_suggestion_value(field, existing_values[field])
        != _normalize_suggestion_value(field, values[field])
        for field in existing_values
    )
    if changed:
        person_changed = values["person"] != existing_values["person"]
        if not db.update_suggestion(
            settings.database_path,
            suggestion_id,
            values,
            learn_person_correction=person_changed and values["person"] in _calendar_names(),
        ):
            raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
        if existing.get("reminder") and (len(str(existing["starts_at"])) == 10) != (
            len(str(values["start"])) == 10
        ):
            # Ganztägig ↔ mit Uhrzeit: eigene Erinnerung passt nicht mehr
            db.set_suggestion_reminder(settings.database_path, suggestion_id, None)
        db.add_suggestion_history(
            settings.database_path,
            suggestion_id,
            "edited",
            {
                "before": existing_values,
                "after": {
                    key: values[key]
                    for key in (
                        "title", "start", "end", "location", "person", "notes", "recurrence"
                    )
                },
            },
        )
        logger.info(
            "Terminvorschlag %s wurde bearbeitet.",
            _suggestion_label(suggestion_id, values["title"]),
        )
        if person_changed and values["person"] in _calendar_names():
            logger.info(
                "Manuelle Kalenderkorrektur für ähnliche zukünftige Termine gemerkt."
            )
    return {
        "status": "success",
        "changed": changed,
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.put("/api/suggestions/{suggestion_id}")
async def update_suggestion(suggestion_id: int, update: SuggestionUpdate):
    async with _suggestion_lock:
        return await _update_suggestion(suggestion_id, update)


async def _send_suggestion_to_icloud(suggestion_id: int, *, resend: bool):
    suggestion = db.get_suggestion(settings.database_path, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
    if suggestion["icloud_exported_at"] and not resend:
        return {
            "status": "already_exported",
            "calendar": suggestion["icloud_calendar"],
            "exported_at": suggestion["icloud_exported_at"],
        }
    if suggestion["dismissed"]:
        raise HTTPException(
            status_code=409, detail=_("Archivierte Vorschläge können nicht übertragen werden.")
        )
    if resend and not suggestion["icloud_exported_at"]:
        raise HTTPException(
            status_code=409,
            detail=_("Dieser Vorschlag wurde noch nicht nach iCloud übertragen."),
        )
    calendar_name = (
        suggestion["icloud_calendar"] if resend else suggestion["person"]
    )
    if not resend and suggestion["person"] not in _calendar_names():
        raise HTTPException(
            status_code=422,
            detail=_("Bitte zuerst einen gültigen Zielkalender auswählen."),
        )
    if not _caldav_configured():
        raise HTTPException(
            status_code=503,
            detail=_("Die iCloud-Verbindung ist nicht vollständig konfiguriert."),
        )

    try:
        result = await asyncio.to_thread(
            (
                caldav.resend_icloud_event
                if resend
                else caldav.create_icloud_event
            ),
            settings.caldav_url,
            settings.caldav_username,
            settings.caldav_app_password,
            settings.caldav_timeout_seconds,
            settings.caldav_timezone,
            calendar_name,
            suggestion,
            default_event_minutes=settings.default_event_minutes,
            reminder_minutes=settings.reminder_minutes,
            allday_reminder_hour=settings.allday_reminder_hour,
        )
    except httpx.HTTPStatusError as error:
        logger.error(
            "iCloud-%s für Vorschlag %s fehlgeschlagen: HTTP %s.",
            "Neuversand" if resend else "Export",
            _suggestion_label(suggestion_id, suggestion["title"]),
            error.response.status_code,
        )
        raise HTTPException(
            status_code=502,
            detail=_(
                "iCloud hat den Termin nicht angenommen. Bitte Verbindung und "
                "Schreibrechte prüfen; der Vorschlag bleibt erhalten."
            ),
        ) from error
    except httpx.TimeoutException as error:
        logger.error(
            "iCloud-%s für Vorschlag %s mit Timeout fehlgeschlagen.",
            "Neuversand" if resend else "Export",
            _suggestion_label(suggestion_id, suggestion["title"]),
        )
        raise HTTPException(
            status_code=504,
            detail=_("Die iCloud-Übertragung hat das Zeitlimit überschritten."),
        ) from error
    except httpx.RequestError as error:
        logger.error(
            "iCloud-Netzwerkfehler beim %s von Vorschlag %s: %s.",
            "Neuversand" if resend else "Export",
            _suggestion_label(suggestion_id, suggestion["title"]),
            type(error).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=_("iCloud konnte nicht erreicht werden. Der Vorschlag bleibt erhalten."),
        ) from error
    except ValueError as error:
        logger.warning(
            "iCloud-%s von Vorschlag %s nicht möglich: %s",
            "Neuversand" if resend else "Export",
            _suggestion_label(suggestion_id, suggestion["title"]),
            error,
        )
        raise HTTPException(status_code=422, detail=str(error)) from error

    if not resend:
        uid = caldav.event_uid(suggestion_id)
        db.mark_suggestion_exported(
            settings.database_path, suggestion_id, uid, str(calendar_name)
        )
    updated = db.get_suggestion(settings.database_path, suggestion_id)
    if updated is None:
        raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
    if not updated["icloud_exported_at"]:
        raise HTTPException(
            status_code=409,
            detail=_("Der Vorschlag hat sich während der Übertragung geändert. Bitte Status prüfen."),
        )
    if resend:
        db.add_suggestion_history(
            settings.database_path,
            suggestion_id,
            "resent",
            {"calendar": updated["icloud_calendar"]},
        )
    logger.info(
        "Vorschlag %s wurde nach iCloud %s (Ergebnis: %s).",
        _suggestion_label(suggestion_id, updated["title"]),
        "erneut gesendet" if resend else "übertragen",
        result,
    )
    return {
        "status": result,
        "calendar": updated["icloud_calendar"],
        "exported_at": updated["icloud_exported_at"],
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.post("/api/suggestions/{suggestion_id}/export")
async def export_suggestion(suggestion_id: int, request: ExportRequest | None = None):
    async with _suggestion_lock:
        calendar = request.calendar if request else None
        if calendar is not None:
            # Ein Klick: Kalender setzen (inkl. Lernen bei Korrektur) und übertragen.
            allowed = _calendar_names()
            if calendar not in allowed:
                raise HTTPException(
                    status_code=422,
                    detail=_("Unbekannter Zielkalender. Erlaubt: {names}.", names=", ".join(allowed)),
                )
            existing = db.get_suggestion(settings.database_path, suggestion_id)
            if existing is None:
                raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
            if existing["icloud_exported_at"]:
                return await _send_suggestion_to_icloud(suggestion_id, resend=False)
            if existing["person"] != calendar:
                await _update_suggestion(
                    suggestion_id,
                    SuggestionUpdate(
                        title=existing["title"],
                        start=existing["starts_at"],
                        end=existing["ends_at"],
                        location=existing["location"],
                        person=calendar,
                        notes=existing["notes"],
                    ),
                )
        return await _send_suggestion_to_icloud(suggestion_id, resend=False)


@app.post("/api/suggestions/{suggestion_id}/resend")
async def resend_suggestion(suggestion_id: int):
    async with _suggestion_lock:
        return await _send_suggestion_to_icloud(suggestion_id, resend=True)


@app.post("/api/suggestions/{suggestion_id}/dismiss")
async def dismiss_api(suggestion_id: int):
    async with _suggestion_lock:
        if not db.dismiss_suggestion(settings.database_path, suggestion_id):
            raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
    return {
        "status": "dismissed",
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


class DismissReasonRequest(BaseModel):
    reason: str = Field(max_length=20)


@app.post("/api/suggestions/{suggestion_id}/reason")
async def dismiss_reason_api(suggestion_id: int, request: DismissReasonRequest):
    if request.reason not in db.DISMISS_REASONS:
        raise HTTPException(status_code=422, detail=_("Unbekannte Auswahl."))
    async with _suggestion_lock:
        if not db.set_dismiss_reason(settings.database_path, suggestion_id, request.reason):
            raise HTTPException(status_code=409, detail=_("Der Vorschlag ist nicht mehr entfernt."))
    return {"status": "ok"}


class IgnoreSenderRequest(BaseModel):
    scope: str = Field(default="address", max_length=20)


class SenderPatternRequest(BaseModel):
    pattern: str = Field(max_length=254)


@app.post("/api/suggestions/{suggestion_id}/ignore-sender")
async def ignore_sender_api(suggestion_id: int, request: IgnoreSenderRequest):
    async with _suggestion_lock:
        item = db.get_suggestion(settings.database_path, suggestion_id)
        if item is None:
            raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
        sender = str(item["source_sender"] or "")
        if request.scope == "domain":
            pattern = db.sender_domain(sender)
        elif request.scope == "address":
            pattern = db.sender_address(sender)
        else:
            raise HTTPException(status_code=422, detail=_("Unbekannte Auswahl."))
        try:
            pattern = db.normalize_sender_pattern(pattern)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail=_("Für diesen Vorschlag ist kein Absender bekannt.")
            ) from error
        # Erst entfernen, dann Regel anlegen: dieser Vorschlag soll im Archiv
        # landen, nicht zusätzlich als ausgeblendet markiert werden.
        dismissed = bool(item["dismissed"]) is False and db.dismiss_suggestion(
            settings.database_path, suggestion_id
        )
        pattern, created, hidden = db.add_ignored_sender(
            settings.database_path, pattern, sender, str(item["source_subject"] or "")
        )
    logger.info(
        "Absender %s wird künftig ausgeblendet (aus Vorschlag %d „%s“); "
        "%d weitere(r) offene(r) Vorschlag/Vorschläge ausgeblendet.",
        pattern,
        suggestion_id,
        item["title"],
        len(hidden),
    )
    return {
        "status": "ignored",
        "pattern": pattern,
        "created": created,
        "dismissed": dismissed,
        "hidden": len(hidden),
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.post("/api/ignored-senders/remove")
async def remove_ignored_sender_api(request: SenderPatternRequest):
    async with _suggestion_lock:
        removed, shown = db.remove_ignored_sender(
            settings.database_path, request.pattern.strip().casefold()
        )
    if removed:
        logger.info(
            "Absender %s wird wieder zugelassen; %d Vorschlag/Vorschläge "
            "wieder eingeblendet.",
            request.pattern,
            len(shown),
        )
    return {
        "status": "removed" if removed else "missing",
        "shown": len(shown),
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.post("/api/sender-rules/trust")
async def trust_sender_api(request: SenderPatternRequest):
    async with _suggestion_lock:
        try:
            pattern, created, shown = db.add_trusted_sender(
                settings.database_path, request.pattern
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    if created:
        logger.info(
            "Absender %s wird immer angezeigt (kein Ausblende-Hinweis, kein "
            "Newsletter-Filter); %d Vorschlag/Vorschläge eingeblendet.",
            pattern,
            len(shown),
        )
    return {
        "status": "trusted",
        "pattern": pattern,
        "created": created,
        "shown": len(shown),
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


class ReanalysisRequest(BaseModel):
    source_key: str = Field(max_length=1000)


_reanalysis_state: dict[str, dict[str, object]] = {}
_background_tasks: set[asyncio.Task] = set()


async def _run_reanalysis(source_key: str) -> None:
    state = _reanalysis_state[source_key]
    try:
        async with _mail_check_lock:  # nicht parallel zum Postfachabruf
            state["status"] = "running"
            message = await asyncio.to_thread(mail.fetch_for_reanalysis, settings, source_key)
            source = mail.message_source(message)
            logger.info("Erneute Auswertung gestartet: „%s“ von %s.", source["subject"], source["sender"])
            events = await asyncio.to_thread(
                mail.analyze_message, settings, message, source, "Erneute Auswertung"
            )
        async with _suggestion_lock:
            result = db.add_reanalysis_events(settings.database_path, source_key, events, source)
        added = len(result["added"])
        logger.info(
            "Erneute Auswertung von „%s“ abgeschlossen: %d neu, %d schon vorhanden, "
            "%d Änderung(en)/Absage(n) nicht übernommen.",
            source["subject"], added, result["known"], result["changes"],
        )
        parts = [
            _("{count} neue(r) Vorschlag/Vorschläge", count=added) if added else _("keine neuen Vorschläge")
        ]
        if result["known"]:
            parts.append(_("{count} schon vorhanden", count=result["known"]))
        if result["changes"]:
            parts.append(_("{count} Änderung(en)/Absage(n) nicht übernommen", count=result["changes"]))
        state.update(status="done", message=f"„{source['subject']}“: {'; '.join(parts)}.", added=added)
    except mail.ReanalysisError as error:
        logger.warning("Erneute Auswertung nicht möglich: %s", error)
        state.update(status="error", message=str(error))
    except Exception as error:
        logger.error("Erneute Auswertung fehlgeschlagen (%s: %s).", type(error).__name__, error)
        state.update(
            status="error",
            message=_("Auswertung fehlgeschlagen ({error}). Details im Protokoll.", error=type(error).__name__),
        )


@app.post("/api/messages/reanalyze", status_code=202)
async def reanalyze_api(request: ReanalysisRequest):
    source_key = request.source_key
    if settings.demo_mode:
        raise HTTPException(status_code=409, detail=_("Im Demo-Modus gibt es kein Postfach zum erneuten Auswerten."))
    if not db.source_known(settings.database_path, source_key):
        raise HTTPException(status_code=404, detail=_("Mail nicht gefunden."))
    current = _reanalysis_state.get(source_key)
    if current and current["status"] in ("queued", "running"):
        return {"status": current["status"]}
    _reanalysis_state[source_key] = {"status": "queued", "message": _("")}
    task = asyncio.create_task(_run_reanalysis(source_key))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "queued", "waiting": _mail_check_lock.locked()}


@app.get("/api/messages/reanalyze")
async def reanalyze_status(source_key: str = Query(max_length=1000)):
    state = _reanalysis_state.get(source_key)
    if state is None:
        raise HTTPException(status_code=404, detail=_("Keine Auswertung gestartet."))
    return {**state, "suggestion_version": db.get_suggestion_version(settings.database_path)}


@app.post("/api/suggestions/{suggestion_id}/reminder")
async def reminder_api(suggestion_id: int, request: ReminderRequest):
    value = (request.reminder or "").strip() or None
    if value not in (None, "none"):
        try:
            minutes = int(value)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=_("Ungültige Erinnerung.")) from error
        if not -1440 < minutes <= 4 * 10080:
            raise HTTPException(status_code=422, detail=_("Erinnerung liegt außerhalb des erlaubten Bereichs."))
        value = str(minutes)
    async with _suggestion_lock:
        item = db.get_suggestion(settings.database_path, suggestion_id)
        if item is None or not db.set_suggestion_reminder(settings.database_path, suggestion_id, value):
            raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
    all_day = len(str(item["starts_at"])) == 10
    label = (
        _("Standard") if value is None else _("keine") if value == "none"
        else formatting.reminder_label(int(value), all_day)
    )
    logger.info("Erinnerung für %s: %s.", _suggestion_label(suggestion_id, item["title"]), label)
    return {
        "status": "saved",
        "reminder": value,
        "label": label,
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.post("/api/suggestions/{suggestion_id}/unhide")
async def unhide_api(suggestion_id: int):
    async with _suggestion_lock:
        if not db.unhide_suggestion(settings.database_path, suggestion_id):
            raise HTTPException(
                status_code=409, detail=_("Der Vorschlag ist nicht ausgeblendet.")
            )
    logger.info("Ausgeblendeten Vorschlag %d wieder eingeblendet.", suggestion_id)
    return {
        "status": "shown",
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.post("/api/suggestions/{suggestion_id}/undo-dismiss")
async def undo_dismiss_api(suggestion_id: int):
    async with _suggestion_lock:
        if not db.restore_suggestion(
            settings.database_path, suggestion_id, keep_active=False
        ):
            raise HTTPException(
                status_code=409,
                detail=_("Der Vorschlag kann nicht mehr zurückgeholt werden."),
            )
    return {
        "status": "restored",
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.post("/api/change-requests/{request_id}/reopen")
async def reopen_change_request(request_id: int):
    async with _suggestion_lock:
        request_data = db.get_change_request(settings.database_path, request_id)
        if request_data is None or not db.reopen_change_request(
            settings.database_path, request_id
        ):
            raise HTTPException(
                status_code=409,
                detail=_("Die Ablehnung kann nicht mehr rückgängig gemacht werden."),
            )
        if request_data["related_suggestion_id"] is not None:
            db.add_suggestion_history(
                settings.database_path,
                request_data["related_suggestion_id"],
                "email_change_reopened",
                {"request_id": request_id},
            )
    return {
        "status": "pending",
        "suggestion_version": db.get_suggestion_version(settings.database_path),
    }


@app.get("/hilfe", response_class=HTMLResponse)
async def help_page(request: Request):
    name = "help_en.html" if i18n.current() == "en" else "help.html"
    return templates.TemplateResponse(request=request, name=name, context={})


MAINTENANCE_DAYS = 14


def _file_size(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in path.parent.glob(path.name + "*")
        if candidate.is_file()
    ) if path.parent.exists() else 0


def _maintenance_context() -> dict[str, object]:
    zone = ZoneInfo(settings.caldav_timezone)
    today = datetime.now(zone).date()
    first_day = today - timedelta(days=MAINTENANCE_DAYS - 1)
    since = datetime.combine(first_day - timedelta(days=30), datetime.min.time(), zone)
    rows = db.maintenance_rows(settings.database_path, since.astimezone(timezone.utc).isoformat())

    def local_day(value: str) -> date | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(zone).date()

    days = []
    for offset in range(MAINTENANCE_DAYS):
        day = today - timedelta(days=offset)
        runs = [r for r in rows["ai_runs"] if local_day(r["at"]) == day]
        ok_runs = [r["seconds"] for r in runs if r["ok"]]
        days.append({
            "day": day,
            "mails": sum(1 for r in rows["mails"] if local_day(r["at"]) == day),
            "suggestions": sum(1 for r in rows["suggestions"] if local_day(r["at"]) == day),
            "exports": sum(1 for r in rows["exports"] if local_day(r["at"]) == day),
            "ai_runs": len(runs),
            "ai_failed": sum(1 for r in runs if not r["ok"]),
            "ai_avg": round(sum(ok_runs) / len(ok_runs)) if ok_runs else None,
        })

    month_start = today - timedelta(days=29)
    month_runs = [r for r in rows["ai_runs"] if (local_day(r["at"]) or date.min) >= month_start]
    durations = sorted(r["seconds"] for r in month_runs if r["ok"])
    failed = sum(1 for r in month_runs if not r["ok"])
    ai = {
        "runs": len(month_runs),
        "failed": failed,
        "failure_rate": round(100 * failed / len(month_runs)) if month_runs else 0,
        "avg": round(sum(durations) / len(durations)) if durations else None,
        "median": round(durations[len(durations) // 2]) if durations else None,
        "max": round(durations[-1]) if durations else None,
        "models": sorted({r["model"] for r in month_runs if r["model"]}),
        "recorded_since": min((r["at"] for r in rows["ai_runs"]), default=None),
    }
    backups = backup.list_backups(settings.backup_dir)
    calendar_store = calendar_backup.storage_info(settings.calendar_store_path)
    storage = [
        ("Datenbank", _file_size(Path(settings.database_path)), None),
        ("Datenbank-Sicherungen", sum(int(b.get("size_bytes") or 0) for b in backups),
         i18n.ngettext("{count} Datei", "{count} Dateien", len(backups))),
        ("Kalender-Sicherung", calendar_store["bytes"],
         i18n.gettext("{count} Terminstände", count=calendar_store["versions"])),
    ]
    try:
        usage = shutil.disk_usage(settings.backup_dir if Path(settings.backup_dir).exists() else "/")
        disk = {"free": usage.free, "total": usage.total, "percent_free": round(100 * usage.free / usage.total)}
    except OSError:
        disk = None
    return {
        "days": days,
        "ai": ai,
        "storage": storage,
        "disk": disk,
        "inventory": db.inventory(settings.database_path),
        "month_failures": sum(1 for r in rows["failures"] if (local_day(r["at"]) or date.min) >= month_start),
    }


@app.get("/protokoll/wartung", response_class=HTMLResponse)
async def maintenance_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="maintenance.html", context=_maintenance_context()
    )


STAT_SEGMENTS = (  # feste Reihenfolge = feste Farbe
    ("exported", "Übernommen"),
    ("rejected", "Abgelehnt"),
    ("hidden", "Ausgeblendet"),
    ("open", "Offen"),
    ("other", "Verfallen oder abgesagt"),
)
REASON_LABELS = {
    "kein_termin": "Kein Termin",
    "nicht_relevant": "Nicht für uns",
    "doppelt": "Doppelt",
    "falsch": "Angaben falsch",
}


@app.get("/protokoll/statistik", response_class=HTMLResponse)
async def statistics_page(request: Request):
    today = datetime.now(ZoneInfo(settings.caldav_timezone)).date()
    first = date(today.year - 1, today.month, 1)  # 12 Monate inkl. laufendem
    data = await asyncio.to_thread(db.statistics, settings.database_path, first.isoformat())
    by_month = {row["month"]: row for row in data["months"]}
    months = []
    cursor = date(today.year, today.month, 1)
    while cursor > first:
        key = f"{cursor:%Y-%m}"
        row = by_month.get(key, {"total": 0, "exported": 0, "rejected": 0, "hidden": 0, "open": 0, "other": 0})
        months.append({**row, "label": formatting.month_label(cursor)})
        cursor = date(cursor.year - (cursor.month == 1), (cursor.month - 2) % 12 + 1, 1)
    # Monate vor dem ersten Vorschlag weglassen
    while months and not months[-1]["total"]:
        months.pop()
    peak = max((row["total"] for row in months), default=0)
    totals = data["totals"]
    decided = totals["exported"] + totals["rejected"] + totals["hidden"]
    reasons_total = sum(data["reasons"].values())
    return templates.TemplateResponse(request=request, name="statistics.html", context={
        "months": months,
        "peak": peak,
        "totals": totals,
        "rate": round(100 * totals["exported"] / decided) if decided else None,
        "segments": STAT_SEGMENTS,
        "reasons": [
            {"label": label, "count": data["reasons"].get(key, 0),
             "share": round(100 * data["reasons"].get(key, 0) / reasons_total) if reasons_total else 0}
            for key, label in REASON_LABELS.items()
        ],
        "reasons_total": reasons_total,
        "without_reason": max(0, totals["rejected"] - reasons_total),
    })


@app.get("/protokoll", response_class=HTMLResponse)
async def protocol_page(request: Request):
    return templates.TemplateResponse(request=request, name="protocol.html", context={})


@app.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss(suggestion_id: int):
    async with _suggestion_lock:
        if not db.dismiss_suggestion(settings.database_path, suggestion_id):
            raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
    return RedirectResponse("/", status_code=303)


def _render_rules_page(
    request: Request, *, error: str | None = None, value: str = "", status_code: int = 200
):
    rules = db.list_ignored_senders(settings.database_path)
    hidden_counts = Counter(
        item["hidden_reason"]
        for item in db.list_suggestions(settings.database_path)
        if item.get("hidden_reason")
    )
    trusted = db.list_ignored_senders(settings.database_path, "show")
    auto_rules = db.list_auto_senders(settings.database_path)
    sender_rows = []
    for domain, entry in sorted(
        db.sender_stats(settings.database_path).items(),
        key=lambda pair: (-pair[1]["total"], pair[0]),
    )[:50]:
        probe = f"x@{domain}"
        rule = next((r for r in rules if db.sender_matches(probe, r["pattern"])), None)
        trust = next((r for r in trusted if db.sender_matches(probe, r["pattern"])), None)
        sender_rows.append(
            {
                "domain": domain,
                **entry,
                "rate": round(100 * entry["exported"] / entry["total"]) if entry["total"] else 0,
                "rule": "hide" if rule else ("show" if trust else None),
                "auto": next((r for r in auto_rules if db.sender_matches(probe, r["pattern"])), None),
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="corrections.html",
        status_code=status_code,
        context={
            "corrections": db.list_all_person_corrections(settings.database_path),
            "ignored_senders": rules,
            "trusted_senders": trusted,
            "auto_rules": auto_rules,
            "auto_thresholds": db.AUTO_THRESHOLDS,
            "caldav_configured": _caldav_configured(),
            "sender_rows": sender_rows,
            "hidden_counts": hidden_counts,
            "sender_error": error,
            "sender_value": value,
        },
    )


@app.get("/korrekturen", response_class=HTMLResponse)
async def corrections(request: Request):
    return _render_rules_page(request)


@app.post("/regeln/absender", response_class=HTMLResponse)
async def add_ignored_sender_form(request: Request):
    form = await _read_form(request)
    try:
        async with _suggestion_lock:
            pattern, created, hidden = db.add_ignored_sender(
                settings.database_path, form.get("pattern", "")
            )
    except ValueError as error:
        return _render_rules_page(
            request, error=str(error), value=form.get("pattern", ""), status_code=422
        )
    if created:
        logger.info(
            "Absender %s von Hand ausgeblendet; %d offene(r) Vorschlag/Vorschläge "
            "ausgeblendet.",
            pattern,
            len(hidden),
        )
    return RedirectResponse("/korrekturen#absender", status_code=303)


@app.post("/regeln/automatisch")
async def add_auto_sender_form(request: Request):
    form = await _read_form(request)
    try:
        threshold = int(form.get("schwelle", "95"))
        pattern = db.set_auto_sender(settings.database_path, form.get("pattern", ""), threshold)
    except ValueError as error:
        return _render_rules_page(request, error=str(error), status_code=422)
    logger.info("Auto-Übernahme für %s ab %d %% Sicherheit eingerichtet.", pattern, threshold)
    return RedirectResponse("/korrekturen#automatisch", status_code=303)


@app.post("/regeln/automatisch/loeschen")
async def remove_auto_sender_form(request: Request):
    form = await _read_form(request)
    if db.remove_auto_sender(settings.database_path, form.get("pattern", "")):
        logger.info("Auto-Übernahme für %s beendet.", form.get("pattern", ""))
    return RedirectResponse("/korrekturen#automatisch", status_code=303)


@app.post("/regeln/absender/loeschen")
async def remove_ignored_sender_form(request: Request):
    form = await _read_form(request)
    pattern = form.get("pattern", "")
    async with _suggestion_lock:
        removed, shown = db.remove_ignored_sender(settings.database_path, pattern)
    if removed:
        logger.info(
            "Absender %s wird wieder zugelassen; %d Vorschlag/Vorschläge "
            "wieder eingeblendet.",
            pattern,
            len(shown),
        )
    return RedirectResponse("/korrekturen#absender", status_code=303)


@app.post("/person-corrections/{suggestion_id}/delete")
async def delete_person_correction(suggestion_id: int):
    if not db.delete_person_correction(settings.database_path, suggestion_id):
        raise HTTPException(status_code=404, detail=_("Korrektur nicht gefunden"))
    db.add_suggestion_history(
        settings.database_path,
        suggestion_id,
        "learning_rule_deleted",
    )
    return RedirectResponse("/korrekturen", status_code=303)


@app.post("/api/change-requests/{request_id}/reject")
async def reject_change_request(request_id: int):
    async with _suggestion_lock:
        request_data = db.get_change_request(settings.database_path, request_id)
        if request_data is None or request_data["status"] != "pending":
            raise HTTPException(
                status_code=404, detail=_("Änderungsvorschlag nicht gefunden")
            )
        if not db.resolve_change_request(
            settings.database_path, request_id, "rejected"
        ):
            raise HTTPException(
                status_code=409,
                detail=_("Änderungsvorschlag wurde bereits bearbeitet"),
            )
        if request_data["related_suggestion_id"] is not None:
            db.add_suggestion_history(
                settings.database_path,
                request_data["related_suggestion_id"],
                "email_change_rejected",
                {"request_id": request_id, "action": request_data["action"]},
            )
        return {"status": "rejected"}


@app.post("/api/change-requests/{request_id}/link")
async def link_change_request_endpoint(
    request_id: int, link: ChangeRequestLink
):
    async with _suggestion_lock:
        request_data = db.get_change_request(settings.database_path, request_id)
        if request_data is None or request_data["status"] != "pending":
            raise HTTPException(
                status_code=404, detail=_("Änderungsvorschlag nicht gefunden")
            )
        if db.get_suggestion(settings.database_path, link.suggestion_id) is None:
            raise HTTPException(status_code=404, detail=_("Zieltermin nicht gefunden"))
        if not db.link_change_request(
            settings.database_path, request_id, link.suggestion_id
        ):
            raise HTTPException(
                status_code=409,
                detail=_("Änderungsvorschlag oder Zieltermin wurde zwischenzeitlich geändert."),
            )
        db.add_suggestion_history(
            settings.database_path,
            link.suggestion_id,
            "email_change_linked",
            {"request_id": request_id, "action": request_data["action"]},
        )
        return {
            "status": "linked",
            "suggestion_version": db.get_suggestion_version(settings.database_path),
        }


async def _cancel_single_occurrence(
    target: dict[str, object], request_data: dict[str, object]
) -> None:
    """Folge-Mail sagt einen einzelnen Serientermin ab: als Ausnahme eintragen."""
    event = request_data["event_data"]
    try:
        day = date.fromisoformat(str(event.get("start", ""))[:10])
    except ValueError as error:
        raise HTTPException(
            status_code=422, detail=_("Die Absage enthält kein gültiges Datum.")
        ) from error
    parsed = formatting.parse_event_value(target["starts_at"], settings.caldav_timezone)
    rule = target["recurrence"]
    if parsed is None or not recurrence.is_occurrence(rule, parsed[0], day):
        raise HTTPException(
            status_code=409,
            detail=_(
                "Am {date} findet laut Serie „{title}“ kein Termin statt.",
                date=formatting.event_time(day.isoformat(), settings.caldav_timezone),
                title=target["title"],
            ),
        )
    exported = target["icloud_exported_at"] and not target["icloud_deleted_at"]
    if exported and not _caldav_configured():
        raise HTTPException(
            status_code=503,
            detail=_("iCloud-Zugangsdaten fehlen; die Absage wurde nicht übernommen."),
        )
    new_rule = {**rule, "exdates": sorted(set(rule["exdates"]) | {day.isoformat()})}
    db.update_suggestion(
        settings.database_path,
        int(target["id"]),
        {
            "title": target["title"],
            "start": target["starts_at"],
            "end": target["ends_at"],
            "location": target["location"],
            "person": target["person"],
            "notes": target["notes"],
            "previous_person": target["person"],
            "recurrence": new_rule,
        },
    )
    db.add_suggestion_history(
        settings.database_path,
        int(target["id"]),
        "occurrence_cancelled",
        {"date": day.isoformat()},
    )
    logger.info(
        "Einzeltermin am %s aus Serie %s ausgenommen.",
        day.isoformat(),
        _suggestion_label(int(target["id"]), target["title"]),
    )
    if not exported:
        return
    updated = db.get_suggestion(settings.database_path, int(target["id"]))
    try:
        await asyncio.to_thread(
            partial(
                caldav.resend_icloud_event,
                settings.caldav_url,
                settings.caldav_username,
                settings.caldav_app_password,
                settings.caldav_timeout_seconds,
                settings.caldav_timezone,
                str(target["icloud_calendar"]),
                updated,
                default_event_minutes=settings.default_event_minutes,
                reminder_minutes=settings.reminder_minutes,
                allday_reminder_hour=settings.allday_reminder_hour,
            )
        )
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(
            status_code=502,
            detail=_(
                "Die Ausnahme ist gespeichert, aber iCloud hat die Aktualisierung "
                "nicht angenommen. Bitte später über „Erneut an iCloud senden“ "
                "übertragen."
            ),
        ) from error


@app.post("/api/change-requests/{request_id}/apply")
async def apply_change_request(request_id: int):
    async with _suggestion_lock:
        request_data = db.get_change_request(settings.database_path, request_id)
        if request_data is None or request_data["status"] != "pending":
            raise HTTPException(
                status_code=404, detail=_("Änderungsvorschlag nicht gefunden")
            )
        target_id = request_data["related_suggestion_id"]
        if target_id is None:
            raise HTTPException(
                status_code=409,
                detail=_(
                    "Die Folge-Mail lässt sich keinem bestehenden Vorschlag "
                    "eindeutig zuordnen und kann deshalb nicht übernommen werden."
                ),
            )
        target = db.get_suggestion(settings.database_path, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail=_("Zieltermin nicht gefunden"))
        if target["cancelled_at"]:
            raise HTTPException(status_code=409, detail=_("Der Zieltermin ist bereits abgesagt."))

        if request_data["action"] == "update":
            event = request_data["event_data"]
            if target["recurrence"] and not event.get("recurrence"):
                raise HTTPException(
                    status_code=409,
                    detail=_(
                        "Die Folge-Mail ändert offenbar nur einen einzelnen Termin der "
                        "Serie „{title}“. Das wird nicht automatisch "
                        "übernommen – bitte direkt in der Kalender-App anpassen und "
                        "den Änderungsvorschlag danach ablehnen.",
                        title=target["title"],
                    ),
                )
            values = {
                "title": event["title"].strip(),
                "start": event["start"].strip(),
                "end": event.get("end"),
                "location": event.get("location") or target["location"],
                "person": target["person"],
                "notes": event.get("notes") or target["notes"],
                "previous_person": target["person"],
            }
            start_value = _validate_event_time(values["start"], "Beginn")
            if event.get("recurrence"):
                try:
                    values["recurrence"] = recurrence.normalize(
                        event["recurrence"], start_value.date(), fix_weekday=True
                    )
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
            if values["end"]:
                end_value = _validate_event_time(values["end"], "Ende")
                if (start_value.tzinfo is None) != (end_value.tzinfo is None):
                    raise HTTPException(
                        status_code=422,
                        detail=_("Beginn und Ende müssen dieselbe Zeitzonenart verwenden."),
                    )
                if end_value < start_value:
                    raise HTTPException(
                        status_code=422,
                        detail=_("Das vorgeschlagene Ende liegt vor dem Beginn."),
                    )
            if target["icloud_exported_at"] and not target["icloud_deleted_at"]:
                if not _caldav_configured():
                    raise HTTPException(
                        status_code=503,
                        detail=_("iCloud-Zugangsdaten fehlen; die Änderung wurde nicht übernommen."),
                    )
            before = {
                "title": target["title"],
                "start": target["starts_at"],
                "end": target["ends_at"],
                "location": target["location"],
                "person": target["person"],
                "notes": target["notes"],
                "recurrence": target["recurrence"],
            }
            changed_values = {
                "title": values["title"],
                "start": values["start"],
                "end": values["end"],
                "location": values["location"],
                "person": values["person"],
                "notes": values["notes"],
                "recurrence": values.get("recurrence", target["recurrence"]),
            }
            changed = any(
                _normalize_suggestion_value(field, before[field])
                != _normalize_suggestion_value(field, changed_values[field])
                for field in before
            )
            if changed:
                if not db.update_suggestion(
                    settings.database_path, target_id, values
                ):
                    raise HTTPException(status_code=404, detail=_("Zieltermin nicht gefunden"))
                db.add_suggestion_history(
                    settings.database_path,
                    target_id,
                    "updated_from_email",
                    {"before": before, "after": changed_values},
                )
            updated_target = db.get_suggestion(settings.database_path, target_id)
            if (
                target["icloud_exported_at"]
                and not target["icloud_deleted_at"]
                and updated_target is not None
            ):
                try:
                    await asyncio.to_thread(
                        caldav.resend_icloud_event,
                        settings.caldav_url,
                        settings.caldav_username,
                        settings.caldav_app_password,
                        settings.caldav_timeout_seconds,
                        settings.caldav_timezone,
                        str(target["icloud_calendar"]),
                        updated_target,
                        default_event_minutes=settings.default_event_minutes,
                        reminder_minutes=settings.reminder_minutes,
                        allday_reminder_hour=settings.allday_reminder_hour,
                    )
                except httpx.HTTPStatusError as error:
                    raise HTTPException(
                        status_code=502,
                        detail=_("iCloud hat die bestätigte Änderung nicht angenommen."),
                    ) from error
                except httpx.TimeoutException as error:
                    raise HTTPException(
                        status_code=504,
                        detail=_("Die iCloud-Aktualisierung hat das Zeitlimit überschritten."),
                    ) from error
                except httpx.RequestError as error:
                    raise HTTPException(
                        status_code=502,
                        detail=_("iCloud konnte nicht erreicht werden; die Änderung bleibt zur Prüfung vorgemerkt."),
                    ) from error
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
        elif target["recurrence"] and not request_data["event_data"].get("recurrence"):
            await _cancel_single_occurrence(target, request_data)
        else:
            icloud_deleted = False
            if target["icloud_exported_at"] and not target["icloud_deleted_at"]:
                if not _caldav_configured():
                    raise HTTPException(
                        status_code=503,
                        detail=_("iCloud-Zugangsdaten fehlen; der Termin wurde nicht gelöscht."),
                    )
                try:
                    await asyncio.to_thread(
                        caldav.delete_icloud_event,
                        settings.caldav_url,
                        settings.caldav_username,
                        settings.caldav_app_password,
                        settings.caldav_timeout_seconds,
                        str(target["icloud_calendar"]),
                        target,
                    )
                    icloud_deleted = True
                except httpx.HTTPStatusError as error:
                    raise HTTPException(
                        status_code=502,
                        detail=_("iCloud hat die bestätigte Absage nicht angenommen."),
                    ) from error
                except httpx.TimeoutException as error:
                    raise HTTPException(
                        status_code=504,
                        detail=_("Das Löschen in iCloud hat das Zeitlimit überschritten."),
                    ) from error
                except httpx.RequestError as error:
                    raise HTTPException(
                        status_code=502,
                        detail=_("iCloud konnte nicht erreicht werden; der Termin bleibt bestehen."),
                    ) from error
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
            if not db.mark_suggestion_cancelled(
                settings.database_path, target_id, icloud_deleted=icloud_deleted
            ):
                raise HTTPException(status_code=409, detail=_("Der Zieltermin wurde bereits abgesagt."))

        if not db.resolve_change_request(settings.database_path, request_id, "applied"):
            raise HTTPException(status_code=409, detail=_("Änderungsvorschlag wurde bereits bearbeitet"))
        db.add_suggestion_history(
            settings.database_path,
            target_id,
            "email_change_accepted",
            {"request_id": request_id, "action": request_data["action"]},
        )
        return {
            "status": "applied",
            "suggestion_version": db.get_suggestion_version(settings.database_path),
        }


ARCHIVE_STATUS = {
    "uebertragen": "Übertragen",
    "entfernt": "Entfernt",
    "automatisch": "Automatisch archiviert",
    "abgesagt": "Abgesagt",
}
ARCHIVE_PAGE_SIZE = 25


def _archive_status(item: dict[str, object], event: dict[str, str] | None) -> str:
    if item["cancelled_at"]:
        return "abgesagt"
    if item["icloud_exported_at"]:
        return "uebertragen"
    if event and event["action"] == "auto_archived":
        return "automatisch"
    return "entfernt"


@app.get("/archiv", response_class=HTMLResponse)
async def archive(
    request: Request,
    q: str = Query(default="", max_length=200),
    kalender: str = Query(default="", max_length=50),
    status: str = Query(default="", max_length=20),
    seite: int = Query(default=1, ge=1, le=10000),
):
    events = db.archive_events(settings.database_path)
    archived = []
    for item in db.list_suggestions(settings.database_path, include_dismissed=True):
        if not item["dismissed"]:
            continue
        event = events.get(int(item["id"]))
        item["archive_status"] = _archive_status(item, event)
        item["archived_at"] = (event or {}).get("at") or item["created_at"]
        item["archive_calendar"] = item["icloud_calendar"] or item["person"] or ""
        archived.append(item)

    counts = {key: 0 for key in ARCHIVE_STATUS}
    query = q.strip().casefold()
    matching = []
    for item in archived:
        if kalender and item["archive_calendar"] != kalender:
            continue
        haystack = " ".join(
            str(item.get(field) or "")
            for field in ("title", "location", "notes", "source_subject", "source_quote")
        ).casefold()
        if query and query not in haystack:
            continue
        counts[item["archive_status"]] += 1
        if status and item["archive_status"] != status:
            continue
        matching.append(item)
    matching.sort(key=lambda item: (str(item["archived_at"]), int(item["id"])), reverse=True)

    pages = max(1, -(-len(matching) // ARCHIVE_PAGE_SIZE))
    page = min(seite, pages)
    shown = matching[(page - 1) * ARCHIVE_PAGE_SIZE : page * ARCHIVE_PAGE_SIZE]
    filters = {"q": q, "kalender": kalender, "status": status}

    def page_url(number: int) -> str:
        params = {key: value for key, value in filters.items() if value}
        if number > 1:
            params["seite"] = str(number)
        return "/archiv" + (f"?{urlencode(params)}" if params else "")

    def status_url(value: str) -> str:
        params = {key: val for key, val in {**filters, "status": value}.items() if val}
        return "/archiv" + (f"?{urlencode(params)}" if params else "")

    return templates.TemplateResponse(
        request=request,
        name="archive.html",
        context={
            "suggestions": shown,
            "total_archived": len(archived),
            "total_matching": len(matching),
            "history": {
                item["id"]: db.list_suggestion_history(settings.database_path, item["id"])
                for item in shown
            },
            "filters": filters,
            "status_labels": ARCHIVE_STATUS,
            "counts": counts,
            "people": _calendar_names(),
            "page": page,
            "pages": pages,
            "page_url": page_url,
            "status_url": status_url,
            "calendar_colors": calendar_backup.calendar_colors(settings.calendar_store_path),
        },
    )


@app.post("/suggestions/{suggestion_id}/restore")
async def restore(suggestion_id: int):
    if not db.restore_suggestion(settings.database_path, suggestion_id):
        raise HTTPException(status_code=404, detail=_("Vorschlag nicht gefunden"))
    return RedirectResponse("/archiv", status_code=303)


BACKUP_GROUPS = {"Backup", "Kalender-Sicherung"}


def _parameter_fields(
    *,
    include_backup: bool,
    form: dict[str, str] | None,
    errors: dict[str, str] | None,
) -> dict[str, list[dict[str, object]]]:
    stored = db.get_app_settings(settings.database_path)
    groups: dict[str, list[dict[str, object]]] = {}
    for parameter in settings_store.PARAMETERS:
        if (parameter.group in BACKUP_GROUPS) != include_backup:
            continue
        groups.setdefault(parameter.group, []).append(
            {
                "parameter": parameter,
                "value": (form or {}).get(
                    parameter.key,
                    settings_store.display_value(parameter, settings),
                ),
                "env_value": settings_store.display_value(parameter, _env_settings),
                "overridden": parameter.key in stored,
                "error": (errors or {}).get(parameter.key),
            }
        )
    return groups


_MESSAGES = {
    "1": "Einstellungen gespeichert.",
    "kalender": "Zielkalender gespeichert.",
    "reset": "Alle Einstellungen auf die Werte aus .env zurückgesetzt.",
    "backup": "Sicherung erstellt.",
    "deleted": "Sicherung gelöscht.",
    "calendar": "Kalendersicherung abgeschlossen.",
    "calendars": "Auswahl der zu sichernden Kalender gespeichert.",
    "restored": (
        "Sicherung wiederhergestellt. Der vorherige Stand wurde zuvor "
        "als neue Sicherung abgelegt."
    ),
}


def _target_calendar_rows() -> list[dict[str, object]]:
    """Alle bekannten Kalender mit ihrer Zielkalender-Einstellung, Ziele zuerst."""
    targets = db.list_target_calendars(settings.database_path)
    known = calendar_backup.known_calendars(settings.calendar_store_path)
    rows = [{**item, "target": True} for item in targets]
    rows += [
        {"name": name, "description": "", "shared": False, "target": False}
        for name in known
        if name not in {item["name"] for item in targets}
    ]
    return rows


@app.post("/einstellungen/zielkalender")
async def save_target_calendars(request: Request):
    form = await _read_form(request)
    chosen = []
    index = 0
    while f"cal-{index}-name" in form:
        name = form[f"cal-{index}-name"].strip()
        if name and form.get(f"cal-{index}-target"):
            try:
                position = int(form.get(f"cal-{index}-position") or index + 1)
            except ValueError:
                position = index + 1
            chosen.append({
                "name": name[:100],
                "description": form.get(f"cal-{index}-description", "")[:500],
                "shared": bool(form.get(f"cal-{index}-shared")),
                "position": position,
            })
        index += 1
    chosen.sort(key=lambda item: (item["position"], item["name"].casefold()))
    async with _suggestion_lock:
        db.save_target_calendars(settings.database_path, chosen)
    logger.info(
        "Zielkalender gespeichert: %s.",
        ", ".join(item["name"] for item in chosen) or "keine",
    )
    return RedirectResponse("/einstellungen?saved=kalender#zielkalender", status_code=303)


def _render_settings_page(
    request: Request,
    *,
    form: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    message: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        status_code=status_code,
        context={
            "groups": _parameter_fields(include_backup=False, form=form, errors=errors),
            "target_rows": _target_calendar_rows(),
            "calendar_colors": calendar_backup.calendar_colors(settings.calendar_store_path),
            "errors": errors or {},
            "message": message,
            "fixed": [
                ("IMAP-Server", f"{settings.imap_host}:{settings.imap_port}"),
                ("IMAP-Benutzer", settings.imap_username),
                ("KI-Server", settings.ai_base_url),
                ("iCloud-Server", settings.caldav_url),
                ("iCloud-Benutzer", settings.caldav_username or "nicht gesetzt"),
                (
                    "iCloud-Passwort",
                    "gesetzt" if settings.caldav_app_password else "nicht gesetzt",
                ),
                (
                    "Zugangsschutz",
                    "aktiv (APP_PASSWORD)" if settings.app_password else "aus – APP_PASSWORD in .env setzen",
                ),
            ],
        },
    )


class UpdateRequest(BaseModel):
    version: str = Field(max_length=20)
    restore_data: bool = False


def _update_context() -> dict[str, object]:
    current = updates.parse_version(version.VERSION)
    available = None if settings.demo_mode or not settings.update_check else updates.releases(settings.update_repository)
    state = None if settings.demo_mode else updates.updater_status(settings.updater_url)
    newer = [r for r in (available or []) if current and r["key"] > current]
    older = [r for r in (available or []) if current and r["key"] < current]
    last_backup = (state or {}).get("backup") if (state or {}).get("state") in ("done",) else None
    return {
        "current": version.VERSION,
        "available": available,
        "newer": newer,
        "older": older,
        "latest": newer[0] if newer else None,
        "updater": state,
        "update_repository": settings.update_repository,
        "update_check": settings.update_check and not settings.demo_mode,
        "restore_candidate": last_backup,
    }


@app.get("/sicherungen/update-fragment", response_class=HTMLResponse)
async def update_fragment(request: Request):
    context = await asyncio.to_thread(_update_context)
    return templates.TemplateResponse(request=request, name="_update.html", context=context)


@app.get("/api/update/status")
async def update_status():
    state = None if settings.demo_mode else await asyncio.to_thread(updates.updater_status, settings.updater_url)
    return {"current": version.VERSION, "updater": state}


@app.post("/api/update", status_code=202)
async def start_update(request: UpdateRequest):
    if settings.demo_mode:
        raise HTTPException(status_code=409, detail=_("Im Demo-Modus gibt es keine Updates."))
    target = updates.parse_version(request.version)
    available = await asyncio.to_thread(updates.releases, settings.update_repository)
    if not target or not available or request.version not in {r["version"] for r in available}:
        raise HTTPException(status_code=422, detail=_("Diese Version ist nicht verfügbar."))
    if request.version == version.VERSION:
        raise HTTPException(status_code=409, detail=_("Diese Version läuft bereits."))
    state = await asyncio.to_thread(updates.updater_status, settings.updater_url)
    if state is None:
        raise HTTPException(status_code=503, detail=_("Der Updater ist nicht eingerichtet. Anleitung siehe unten."))
    if state.get("state") == "running":
        raise HTTPException(status_code=409, detail=_("Es läuft bereits ein Update."))
    restore = None
    if request.restore_data:
        restore = state.get("backup") if state.get("state") == "done" else None
        if not restore:
            raise HTTPException(status_code=409, detail=_("Es gibt keine Sicherung von vor dem letzten Update."))
    async with _mail_check_lock:  # nicht mitten in einem Postfachabruf
        database_backup = await _run_backup(prune=False)
        await asyncio.to_thread(updates.backup_calendar_store, settings.calendar_store_path, settings.backup_dir)
    ok, message = await asyncio.to_thread(updates.start_update, settings.updater_url, {
        "version": request.version,
        "previous": version.VERSION,
        "backup": database_backup.name,
        "restore_backup": restore,
    })
    if not ok:
        raise HTTPException(status_code=502, detail=_("Der Updater hat abgelehnt: {reason}", reason=message))
    logger.info(
        "Update von %s auf %s gestartet (Sicherung %s%s).",
        version.VERSION, request.version, database_backup.name,
        f", Datenbank wird auf {restore} zurückgesetzt" if restore else "",
    )
    return {"status": "started", "backup": database_backup.name}


def _render_backup_page(
    request: Request,
    *,
    form: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    message: str | None = None,
    problem: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request=request,
        name="backups.html",
        status_code=status_code,
        context={
            "groups": _parameter_fields(include_backup=True, form=form, errors=errors),
            "errors": errors or {},
            "message": message,
            "problem": problem,
            "backups": backup.list_backups(settings.backup_dir),
            "caldav_configured": _caldav_configured(),
            "calendar_snapshots": calendar_backup.list_snapshots(
                settings.calendar_store_path, limit=48
            ),
            "calendar_storage": calendar_backup.storage_info(
                settings.calendar_store_path
            ),
            "known_calendars": calendar_backup.known_calendars(
                settings.calendar_store_path
            ),
            "excluded_calendars": _excluded_calendars(),
            "calendar_backup_running": _calendar_backup_lock.locked(),
        },
    )


async def _read_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    return {key: values[-1] for key, values in parse_qs(body).items()}


def _store_settings(values: dict[str, object]) -> None:
    changed = {
        key: value for key, value in values.items() if getattr(settings, key) != value
    }
    if not changed:
        return
    db.save_app_settings(settings.database_path, changed)
    _reload_settings()
    logger.info(
        "Einstellungen geändert: %s.",
        ", ".join(settings_store.PARAMETERS_BY_KEY[key].label for key in changed),
    )


@app.get("/einstellungen", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str | None = None):
    return _render_settings_page(request, message=_(_MESSAGES[saved]) if saved in _MESSAGES else None)


@app.post("/einstellungen", response_class=HTMLResponse)
async def save_settings(request: Request):
    form = await _read_form(request)
    form = {
        key: value
        for key, value in form.items()
        if key in settings_store.PARAMETERS_BY_KEY
        and settings_store.PARAMETERS_BY_KEY[key].group not in BACKUP_GROUPS
    }
    values, errors = settings_store.parse_form(form)
    if errors:
        return _render_settings_page(
            request, form=form, errors=errors, status_code=422
        )
    _store_settings(values)
    return RedirectResponse("/einstellungen?saved=1", status_code=303)


@app.post("/einstellungen/zuruecksetzen")
async def reset_settings():
    db.reset_app_settings(settings.database_path)
    _reload_settings()
    logger.info("Einstellungen auf die Werte aus .env zurückgesetzt.")
    return RedirectResponse("/einstellungen?saved=reset", status_code=303)


@app.get("/sicherungen", response_class=HTMLResponse)
async def backups_page(request: Request, saved: str | None = None):
    return _render_backup_page(request, message=_(_MESSAGES[saved]) if saved in _MESSAGES else None)


@app.post("/sicherungen/einstellungen", response_class=HTMLResponse)
async def save_backup_settings(request: Request):
    form = await _read_form(request)
    form = {
        key: value
        for key, value in form.items()
        if key in settings_store.PARAMETERS_BY_KEY
        and settings_store.PARAMETERS_BY_KEY[key].group in BACKUP_GROUPS
    }
    values, errors = settings_store.parse_form(form)
    if errors:
        return _render_backup_page(request, form=form, errors=errors, status_code=422)
    _store_settings(values)
    return RedirectResponse("/sicherungen?saved=1", status_code=303)


@app.post("/sicherungen/erstellen", response_class=HTMLResponse)
async def backup_now(request: Request):
    try:
        await _run_backup()
    except Exception as error:
        logger.error("Datenbanksicherung fehlgeschlagen: %s", error)
        return _render_backup_page(
            request,
            problem=_("Sicherung fehlgeschlagen. Details stehen im Live-Log."),
            status_code=500,
        )
    return RedirectResponse("/sicherungen?saved=backup", status_code=303)


@app.get("/sicherungen/{name}/download")
async def download_backup(name: str):
    try:
        path = backup.backup_path(settings.backup_dir, name)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=_("Sicherung nicht gefunden")) from error
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)


@app.post("/sicherungen/{name}/loeschen")
async def delete_backup(name: str):
    try:
        await asyncio.to_thread(backup.delete_backup, settings.backup_dir, name)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=_("Sicherung nicht gefunden")) from error
    return RedirectResponse("/sicherungen?saved=deleted", status_code=303)


@app.post("/sicherungen/{name}/wiederherstellen", response_class=HTMLResponse)
async def restore_backup(request: Request, name: str):
    try:
        backup.backup_path(settings.backup_dir, name)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=_("Sicherung nicht gefunden")) from error
    if _mail_check_lock.locked():
        return _render_backup_page(
            request,
            problem=_(
                "Gerade läuft eine Postfachprüfung. Bitte warten, bis sie "
                "abgeschlossen ist, und dann erneut versuchen."
            ),
            status_code=409,
        )
    # Während der Wiederherstellung keine Mailprüfung und keine Bearbeitung.
    async with _mail_check_lock, _suggestion_lock:
        try:
            # Aktuellen Stand vorher sichern – ohne Aufräumen, damit die
            # wiederherzustellende Sicherung nicht gelöscht wird.
            await _run_backup(prune=False)
            await asyncio.to_thread(
                backup.restore_backup, settings.database_path, settings.backup_dir, name
            )
            db.initialize(settings.database_path)  # ältere Sicherungen nachrüsten
        except Exception as error:
            logger.error("Wiederherstellung fehlgeschlagen: %s", error)
            return _render_backup_page(
                request,
                problem=_("Wiederherstellung fehlgeschlagen. Details stehen im Live-Log."),
                status_code=500,
            )
        _reload_settings()
    return RedirectResponse("/sicherungen?saved=restored", status_code=303)


@app.post("/sicherungen/kalender/abgleichen", response_class=HTMLResponse)
async def calendar_backup_now(request: Request):
    if not _caldav_configured():
        return _render_backup_page(
            request, problem=_("iCloud-Zugangsdaten fehlen in .env."), status_code=503
        )
    if _calendar_backup_lock.locked():
        return _render_backup_page(
            request,
            problem=_("Eine Kalendersicherung läuft bereits. Bitte kurz warten."),
            status_code=409,
        )
    try:
        await _run_calendar_backup()
    except Exception as error:
        logger.error("Kalendersicherung fehlgeschlagen: %s", type(error).__name__)
        return _render_backup_page(
            request,
            problem=_("Kalendersicherung fehlgeschlagen. Details stehen im Live-Log."),
            status_code=502,
        )
    return RedirectResponse("/sicherungen?saved=calendar", status_code=303)


@app.post("/sicherungen/kalender/auswahl")
async def save_calendar_selection(request: Request):
    body = (await request.body()).decode("utf-8", errors="replace")
    chosen = set(parse_qs(body).get("calendar", []))
    known = set(calendar_backup.known_calendars(settings.calendar_store_path))
    excluded = sorted(known - chosen)
    db.save_app_settings(
        settings.database_path,
        {EXCLUDED_CALENDARS_KEY: json.dumps(excluded, ensure_ascii=False)},
    )
    logger.info(
        "Auswahl der Kalendersicherung gespeichert; ausgenommen: %s.",
        ", ".join(excluded) or "keine",
    )
    return RedirectResponse("/sicherungen?saved=calendars", status_code=303)


BACKUP_PAGE_SIZE = 100
BACKUP_STATUSES = ("gelöscht", "geändert", "neu", "alle")
HISTORY_GROUPINGS = ("tag", "woche", "monat")


def _history_groups(points: list[dict[str, Any]], grouping: str) -> list[dict[str, Any]]:
    """Änderungspunkte (neueste zuerst) nach Tag, Kalenderwoche oder Monat bündeln.

    Ein Klick auf einen Zeitraum zeigt den Stand vor dessen erster Änderung;
    die erste Sicherung überhaupt steht für sich (es gibt keinen Stand davor).
    """
    tz = ZoneInfo(settings.caldav_timezone)
    groups: list[dict[str, Any]] = []
    for point in points:
        if point["before_id"] is None:
            groups.append({"key": "erste", "first": True, "label": formatting.timestamp(point["taken_at"], settings.caldav_timezone),
                           "before_id": None, "deleted": 0, "changed": 0, "added": 0, "runs": 1})
            continue
        day = datetime.fromisoformat(str(point["taken_at"]).replace("Z", "+00:00")).astimezone(tz).date()
        if grouping == "tag":
            key, label = day.isoformat(), formatting.event_time(day.isoformat(), settings.caldav_timezone)
            period = _("am {date}", date=label)
        elif grouping == "monat":
            key, label = f"{day:%Y-%m}", formatting.month_label(day)
            period = _("im {month}", month=label)
        else:
            monday = day - timedelta(days=day.weekday())
            key, label = monday.isoformat(), formatting.week_label(monday)
            period = _("in {week}", week=label)
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "first": False, "label": label, "period": period,
                           "deleted": 0, "changed": 0, "added": 0, "runs": 0})
        group = groups[-1]
        group["deleted"] += point["deleted"]
        group["changed"] += point["changed"]
        group["added"] += point["added"]
        group["runs"] += 1
        group["before_id"] = point["before_id"]  # Punkte sind neueste zuerst → zuletzt = älteste
    return groups
_BACKUP_FILTERS = ("stand", "q", "kalender", "status", "zeitraum")


def _backup_groups(items: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Termine nach Monat und Tag gruppieren; laufende Serien zuerst für sich."""
    tz = settings.caldav_timezone
    colors = calendar_backup.calendar_colors(settings.calendar_store_path)
    groups: list[dict[str, Any]] = []
    for item in items:
        parsed = formatting.parse_event_value(item["dtstart"], tz)
        day = parsed[0] if parsed else None
        item = {
            **item,
            "color": colors.get(item["calendar"]),
            "time": formatting.clock_text(item["dtstart"], tz),
            "when": formatting.event_range(item["dtstart"], item.get("dtend"), tz),
        }
        if item["status"] == "geändert":
            item["current_when"] = formatting.event_range(item["current_dtstart"], item.get("current_dtend"), tz)
            item["diff"] = {
                "summary": (item["summary"] or "") != (item["current_summary"] or ""),
                "when": item["when"] != item["current_when"],
                "location": (item["location"] or "") != (item["current_location"] or ""),
            }
        if item["upcoming"] and item["recurring"] and day is not None and day < today:
            key, label, past = "serien", _("Laufende Serien"), False
            day_key, day_text = "serien", ""
            weekday = formatting.day_label(day).split(" ")[0]
            item["time"] = weekday if item["time"] == _("ganztägig") else f"{weekday} {item['time']}"
            item["series_since"] = formatting.event_time(day.isoformat(), tz)
        elif day is None:
            key, label, past = "ohne-datum", _("Ohne Datum"), not item["upcoming"]
            day_key, day_text = "ohne-datum", ""
        else:
            past = not item["upcoming"]
            key = f"{'v' if past else 'k'}{day:%Y-%m}"
            label = formatting.month_label(day)
            day_key, day_text = f"{key}-{day:%d}", formatting.day_label(day)
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "label": label, "past": past, "days": []})
        days = groups[-1]["days"]
        if not days or days[-1]["key"] != day_key:
            days.append({"key": day_key, "label": day_text, "today": day == today, "entries": []})
        days[-1]["entries"].append(item)
    return groups


@app.get("/sicherungen/kalender", response_class=HTMLResponse)
async def calendar_backup_search(
    request: Request,
    stand: int | None = None,
    q: str = Query(default="", max_length=200),
    kalender: str = Query(default="", max_length=200),
    status: str = Query(default="", max_length=20),
    zeitraum: str = Query(default="kommend", max_length=20),
    seite: int = Query(default=1, ge=1, le=1000),
    teil: str = Query(default="", max_length=10),
    verlauf: str = Query(default="", max_length=10),
    nur_aenderungen: str | None = None,
    restored: str | None = None,
    problem: str | None = None,
):
    store = settings.calendar_store_path
    snapshots = calendar_backup.list_snapshots(store)
    newest = snapshots[0] if snapshots else None
    selected = next((item for item in snapshots if item["id"] == stand), newest)
    is_newest = selected is None or newest is None or selected["id"] == newest["id"]
    period = zeitraum if zeitraum in calendar_backup.PERIODS else "kommend"
    today = datetime.now(ZoneInfo(settings.caldav_timezone)).date()
    filters = {"text": q, "calendar": kalender, "period": period, "today": today}
    counts = (
        await asyncio.to_thread(calendar_backup.count_by_status, store, int(selected["id"]), **filters)
        if selected else {"gelöscht": 0, "geändert": 0, "unverändert": 0, "alle": 0, "neu": 0}
    )
    if is_newest:
        chosen = "alle"  # im neuesten Stand ist nichts gelöscht oder geändert
    elif status in BACKUP_STATUSES:
        chosen = status
    else:  # Standard: dorthin, wo es etwas zu sehen gibt
        chosen = next((key for key in ("gelöscht", "geändert", "neu") if counts[key]), "alle")
    rows = (
        await asyncio.to_thread(
            calendar_backup.search, store, int(selected["id"]),
            status="" if chosen == "alle" else chosen,
            limit=BACKUP_PAGE_SIZE + 1, offset=(seite - 1) * BACKUP_PAGE_SIZE, **filters,
        )
        if selected else []
    )
    has_more = len(rows) > BACKUP_PAGE_SIZE
    state = {
        "stand": "" if is_newest or not selected else str(selected["id"]),
        "q": q.strip(), "kalender": kalender,
        "status": "" if is_newest or status not in BACKUP_STATUSES else status,
        "zeitraum": "" if period == "kommend" else period,
    }

    def link(**changes: Any) -> str:
        merged = {**state, **{key: "" if value is None else str(value) for key, value in changes.items()}}
        query = urlencode({key: value for key, value in merged.items() if value})
        return "/sicherungen/kalender" + (f"?{query}" if query else "")

    points = calendar_backup.change_points(snapshots)
    grouping = verlauf if verlauf in HISTORY_GROUPINGS else request.cookies.get("verlauf", "")
    grouping = grouping if grouping in HISTORY_GROUPINGS else "woche"
    history = _history_groups(points, grouping)
    selected_point = next(
        (group for group in history if selected and group["before_id"] == selected["id"]), None
    )
    name = {"liste": "_calendar_backup_results.html", "mehr": "_calendar_backup_more.html"}.get(
        teil, "calendar-backup.html"
    )
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "snapshots": snapshots,
            "selected": selected,
            "is_newest": is_newest,
            "selected_point": selected_point,
            "points": points,
            "history": history,
            "grouping": grouping,
            "counts": counts,
            "chosen": chosen,
            "period": period,
            "groups": _backup_groups(rows[:BACKUP_PAGE_SIZE], today),
            "has_more": has_more,
            "next_page": seite + 1,
            "page": seite,
            "link": link,
            "q": q,
            "kalender": kalender,
            "calendars": calendar_backup.known_calendars(store),
            "calendar_colors": calendar_backup.calendar_colors(store),
            "message": (
                _("„{title}“ wurde in iCloud wiederhergestellt.", title=restored) if restored else None
            ),
            "problem": problem,
            "caldav_configured": _caldav_configured(),
        },
    )
    if verlauf in HISTORY_GROUPINGS:  # Wahl pro Gerät merken
        response.set_cookie("verlauf", verlauf, max_age=31536000, samesite="lax")
    return response


@app.post("/api/benachrichtigungen/test")
async def notification_test():
    if not notify.channels(settings):
        raise HTTPException(status_code=409, detail=_("Kein Kanal eingerichtet. Zugangsdaten in .env eintragen."))
    results = await asyncio.to_thread(lambda: _in_language(lambda: notify.send(
        settings, _("Test vom Kalender-Assistenten"),
        _("Wenn du das liest, kommen Benachrichtigungen an."),
    )))
    return {"results": results}


@app.post("/sicherungen/warnung/ausblenden")
async def dismiss_mass_alert(request: Request):
    db.set_service_state(settings.database_path, **{MASS_DELETE_KEY: ""})
    target = urlsplit(request.headers.get("referer") or "/")
    path = target.path if target.path.startswith("/") and not target.path.startswith("//") else "/"
    return RedirectResponse(path, status_code=303)


@app.post("/sicherungen/kalender/eintrag/{presence_id}/wiederherstellen")
async def restore_calendar_event(request: Request, presence_id: int):
    version = calendar_backup.get_version(settings.calendar_store_path, presence_id)
    if version is None:
        raise HTTPException(status_code=404, detail=_("Gesicherter Termin nicht gefunden"))
    # Zurück zur Suche mit denselben Filtern; alte Meldungen entfernen.
    referer = urlsplit(request.headers.get("referer") or "")
    filters = {
        key: values[-1]
        for key, values in parse_qs(referer.query).items()
        if key in set(_BACKUP_FILTERS)
    }

    def with_param(key: str, value: str) -> str:
        return "/sicherungen/kalender?" + urlencode({**filters, key: value})

    if not _caldav_configured():
        return RedirectResponse(
            with_param("problem", "iCloud-Zugangsdaten fehlen in .env."), status_code=303
        )

    def restore() -> str:
        calendars = caldav.discover_icloud_calendars(
            settings.caldav_url,
            settings.caldav_username,
            settings.caldav_app_password,
            settings.caldav_timeout_seconds,
        )
        matches = [c for c in calendars if c["name"] == version["calendar"]]
        if len(matches) != 1:
            raise ValueError(
                f"Der Kalender „{version['calendar']}“ wurde in iCloud nicht "
                "eindeutig gefunden."
            )
        filename = version["href"].rstrip("/").rsplit("/", 1)[-1]
        with caldav.open_client(
            settings.caldav_username,
            settings.caldav_app_password,
            settings.caldav_timeout_seconds,
        ) as client:
            return caldav.restore_event_resource(
                client, str(matches[0]["calendar_url"]), filename, version["ics"]
            )

    try:
        result = await asyncio.to_thread(restore)
    except ValueError as error:
        return RedirectResponse(with_param("problem", str(error)), status_code=303)
    except httpx.HTTPStatusError as error:
        logger.error(
            "Wiederherstellung von „%s“ fehlgeschlagen: HTTP %s.",
            version["summary"],
            error.response.status_code,
        )
        detail = (
            "iCloud hat den Termin abgelehnt. Möglicherweise existiert er inzwischen "
            "in einem anderen Kalender (gleiche Termin-ID)."
            if error.response.status_code in (403, 409, 412)
            else f"iCloud hat den Termin abgelehnt (HTTP {error.response.status_code})."
        )
        return RedirectResponse(with_param("problem", detail), status_code=303)
    except httpx.RequestError:
        return RedirectResponse(
            with_param("problem", "iCloud ist nicht erreichbar."), status_code=303
        )
    logger.info(
        "Termin „%s“ im Kalender „%s“ aus der Sicherung wiederhergestellt (%s).",
        version["summary"],
        version["calendar"],
        "neu angelegt" if result == "created" else "überschrieben",
    )
    # Neuen Stand gleich mitsichern, damit Suche und Status aktuell sind.
    if not _calendar_backup_lock.locked():
        asyncio.create_task(_run_calendar_backup_quietly())
    return RedirectResponse(with_param("restored", version["summary"]), status_code=303)


async def _run_calendar_backup_quietly() -> None:
    try:
        await _run_calendar_backup()
    except Exception as error:
        logger.error("Kalendersicherung fehlgeschlagen: %s", type(error).__name__)

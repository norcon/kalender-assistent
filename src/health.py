"""Statusanzeige und Healthcheck."""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import formatting
from .i18n import _
from .config import Settings


def check_ai_server(settings: Settings) -> dict[str, Any]:
    """Kurze Erreichbarkeitsprüfung des KI-Servers über /models."""
    checked_at = datetime.now(timezone.utc).isoformat()
    headers = {"Authorization": f"Bearer {settings.ai_api_key}"} if settings.ai_api_key else {}
    try:
        response = httpx.get(
            f"{settings.ai_base_url}/models", headers=headers, timeout=10
        )
    except httpx.HTTPError as error:
        return {"reachable": False, "model_found": None, "error": type(error).__name__,
                "checked_at": checked_at}
    model_found = None
    if response.status_code == 200:
        try:
            ids = {item.get("id") for item in response.json().get("data", [])}
            model_found = settings.ai_model in ids if ids else None
        except (ValueError, AttributeError):
            model_found = None
    return {
        "reachable": response.status_code < 500,
        "model_found": model_found,
        "error": None if response.status_code < 500 else f"HTTP {response.status_code}",
        "checked_at": checked_at,
    }


def _age(value: str | None, now: datetime) -> timedelta | None:
    if not value:
        return None
    try:
        return now - datetime.fromisoformat(value)
    except ValueError:
        return None


def idle_active(idle_state: dict[str, Any] | None) -> bool:
    return (idle_state or {}).get("status") == "active"


def effective_poll_seconds(settings: Any, idle_state: dict[str, Any] | None) -> int:
    """Abrufintervall: seltener, solange IMAP IDLE neue Mails sofort meldet."""
    if idle_active(idle_state):
        return int(getattr(settings, "imap_idle_poll_seconds", settings.imap_poll_seconds))
    return int(settings.imap_poll_seconds)


def status_items(
    settings: Settings,
    *,
    service_state: dict[str, str],
    check_state: dict[str, Any],
    ai_state: dict[str, Any] | None,
    idle_state: dict[str, Any] | None,
    last_calendar_snapshot: dict[str, Any] | None,
    caldav_configured: bool,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Statuspunkte für die Startseite: state ist ok, warn, error oder off."""
    now = now or datetime.now(timezone.utc)
    items: list[dict[str, str]] = []

    # Postfach
    success = service_state.get("mail_last_success")
    failure = service_state.get("mail_last_error")
    if check_state.get("status") == "running":
        mail = ("ok", _("Abruf läuft …"))
    elif failure and (not success or failure > success):
        mail = ("error", _("Letzter Abruf fehlgeschlagen ({age})", age=formatting.relative_time(failure, now)))
    elif success:
        age = _age(success, now)
        stale = age is not None and age > timedelta(
            seconds=3 * effective_poll_seconds(settings, idle_state) + 300
        )
        mail = ("warn" if stale else "ok", _("Letzter Abruf {age}", age=formatting.relative_time(success, now)))
    else:
        mail = ("warn", _("Noch kein erfolgreicher Abruf"))
    items.append({"key": "mail", "label": _("Postfach"), "state": mail[0], "text": mail[1]})

    # IMAP IDLE
    status = (idle_state or {}).get("status")
    minutes = max(1, round(settings.imap_poll_seconds / 60))
    if status == "active":
        idle_minutes = max(1, round(effective_poll_seconds(settings, idle_state) / 60))
        idle = ("ok", _("Neue E-Mails werden sofort erkannt – zur Sicherheit Abruf alle {minutes} Min.", minutes=idle_minutes))
    elif status == "unsupported":
        idle = ("off", _("Vom Server nicht unterstützt – Abruf alle {minutes} Min.", minutes=minutes))
    elif status in ("connecting", "starting"):
        idle = ("warn", _("Verbindet …"))
    elif status is None or status == "stopped":
        idle = ("off", _("Aus – Abruf alle {minutes} Min.", minutes=minutes))
    else:
        idle = ("warn", _("Unterbrochen – Abruf alle {minutes} Min., neuer Versuch läuft", minutes=minutes))
    items.append({"key": "idle", "label": _("Sofort-Erkennung"), "state": idle[0], "text": idle[1]})

    # KI-Server
    if not ai_state:
        ai = ("warn", _("Noch nicht geprüft"))
    elif not ai_state["reachable"]:
        ai = ("error", _("Nicht erreichbar ({error})", error=ai_state.get("error")))
    elif ai_state.get("model_found") is False:
        ai = ("warn", _("Erreichbar, Modell „{model}“ nicht gefunden", model=settings.ai_model))
    else:
        ai = ("ok", _("Erreichbar, Modell „{model}“ vorhanden", model=settings.ai_model) if ai_state.get("model_found") else _("Erreichbar"))
    items.append({"key": "ai", "label": _("KI-Server"), "state": ai[0], "text": ai[1]})

    # iCloud-Sicherung
    if not caldav_configured:
        cloud = ("off", _("iCloud nicht konfiguriert"))
    elif not last_calendar_snapshot:
        cloud = ("warn", _("Noch keine Kalendersicherung"))
    else:
        taken = str(last_calendar_snapshot["taken_at"])
        age = _age(taken, now)
        stale = age is not None and age > timedelta(minutes=2 * settings.calendar_backup_minutes + 10)
        partial = last_calendar_snapshot.get("status") == "partial"
        cloud = (
            "warn" if stale or partial else "ok",
            _("Kalender gesichert {age}", age=formatting.relative_time(taken, now))
            + (" " + _("(unvollständig)") if partial else ""),
        )
    items.append({"key": "icloud", "label": "iCloud", "state": cloud[0], "text": cloud[1]})
    return items

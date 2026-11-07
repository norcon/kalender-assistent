"""Benachrichtigungen über ntfy, Pushover, Home Assistant und E-Mail.

Zugangsdaten stehen nur in .env (wie alle anderen Zugangsdaten). Jeder
eingerichtete Kanal wird genutzt. Fehler beim Senden werden protokolliert,
stören aber nie den eigentlichen Ablauf.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings

logger = logging.getLogger(__name__)
TIMEOUT = 15


def channels(settings: Settings) -> list[str]:
    """Eingerichtete Kanäle (Reihenfolge = Anzeige)."""
    found = []
    if settings.ntfy_url:
        found.append("ntfy")
    if settings.pushover_token and settings.pushover_user:
        found.append("Pushover")
    if settings.homeassistant_url and settings.homeassistant_token and settings.homeassistant_service:
        found.append("Home Assistant")
    if settings.smtp_host and settings.notify_email_to:
        found.append("E-Mail")
    return found


def link(settings: Settings, path: str = "/") -> str | None:
    base = settings.app_url.rstrip("/")
    return f"{base}{path}" if base else None


def _ntfy(settings: Settings, title: str, message: str, url: str | None, urgent: bool) -> None:
    parts = urlsplit(settings.ntfy_url.rstrip("/"))
    topic = parts.path.rsplit("/", 1)[-1]
    if not topic:
        raise ValueError("NTFY_URL braucht ein Thema, z. B. https://ntfy.sh/mein-thema")
    server = urlunsplit((parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0], "", ""))
    payload: dict[str, Any] = {
        "topic": topic, "title": title, "message": message,
        "priority": 4 if urgent else 3, "tags": ["warning" if urgent else "calendar"],
    }
    if url:
        payload["click"] = url
    headers = {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
    httpx.post(server or f"{parts.scheme}://{parts.netloc}", json=payload, headers=headers,
               timeout=TIMEOUT).raise_for_status()


def _pushover(settings: Settings, title: str, message: str, url: str | None, urgent: bool) -> None:
    data = {"token": settings.pushover_token, "user": settings.pushover_user,
            "title": title, "message": message, "priority": 1 if urgent else 0}
    if url:
        data["url"] = url
    httpx.post("https://api.pushover.net/1/messages.json", data=data, timeout=TIMEOUT).raise_for_status()


def _homeassistant(settings: Settings, title: str, message: str, url: str | None, urgent: bool) -> None:
    service = settings.homeassistant_service.removeprefix("notify.").strip()
    data: dict[str, Any] = {"title": title, "message": message}
    if url:
        data["data"] = {"url": url, "clickAction": url}  # iOS bzw. Android
    httpx.post(
        f"{settings.homeassistant_url.rstrip('/')}/api/services/notify/{service}",
        json=data, headers={"Authorization": f"Bearer {settings.homeassistant_token}"},
        timeout=TIMEOUT,
    ).raise_for_status()


def _email(settings: Settings, title: str, message: str, url: str | None, urgent: bool) -> None:
    mail = EmailMessage()
    mail["Subject"] = title
    mail["From"] = settings.smtp_from or settings.smtp_username
    mail["To"] = settings.notify_email_to
    if urgent:
        mail["X-Priority"] = "2"
    mail.set_content(message + (f"\n\n{url}" if url else ""))
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT, context=context)
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(mail)


_SENDERS = {"ntfy": _ntfy, "Pushover": _pushover, "Home Assistant": _homeassistant, "E-Mail": _email}


def send(settings: Settings, title: str, message: str, *, path: str = "/", urgent: bool = False) -> dict[str, str]:
    """An alle eingerichteten Kanäle senden; Ergebnis je Kanal ("ok" oder Fehler)."""
    results: dict[str, str] = {}
    url = link(settings, path)
    for name in channels(settings):
        try:
            _SENDERS[name](settings, title, message, url, urgent)
            results[name] = "ok"
        except Exception as error:  # noqa: BLE001 – ein Kanal darf die anderen nicht stören
            detail = f"{type(error).__name__}"
            if isinstance(error, httpx.HTTPStatusError):
                detail += f" {error.response.status_code}"
            results[name] = detail
            logger.warning("Benachrichtigung über %s fehlgeschlagen: %s", name, detail)
    if results:
        logger.info("Benachrichtigung „%s“ gesendet (%s).", title,
                    ", ".join(f"{name}: {result}" for name, result in results.items()))
    return results

"""IMAP IDLE: Der Mailserver meldet neue Nachrichten sofort.

Läuft in einem eigenen Thread mit einer dauerhaften, nur lesenden Verbindung.
Meldet der Server neue Nachrichten (EXISTS), wird ein Abruf ausgelöst. Der
regelmäßige Abruf bleibt als Rückfallebene bestehen, falls IDLE nicht
verfügbar ist oder die Verbindung abreißt.
"""

import imaplib
import logging
import select
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .config import Settings

logger = logging.getLogger(__name__)

RENEW_SECONDS = 25 * 60  # RFC 2177: IDLE spätestens nach 29 Minuten erneuern
WAKE_SECONDS = 30  # so oft auf Beenden/Einstellungsänderungen prüfen
RETRY_SECONDS = 60


class IdleWatcher:
    def __init__(
        self,
        get_settings: Callable[[], Settings],
        on_new_mail: Callable[[], None],
        stop_event: threading.Event | None = None,
        label: str = "",
    ) -> None:
        self._get_settings = get_settings
        self._prefix = f"Postfach „{label}“: " if label else ""
        self._on_new_mail = on_new_mail
        self.stop_event = stop_event or threading.Event()
        self.state: dict[str, str | None] = {
            "status": "starting",
            "since": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }

    def _set_state(self, status: str, error: str | None = None) -> None:
        if self.state["status"] != status or self.state["error"] != error:
            self.state = {
                "status": status,
                "since": datetime.now(timezone.utc).isoformat(),
                "error": error,
            }

    def run(self) -> None:
        while not self.stop_event.is_set():
            settings = self._get_settings()
            try:
                supported = self._session(settings)
                if not supported:
                    self._set_state("unsupported")
                    logger.info(
                        self._prefix + "Der Mailserver unterstützt kein IMAP IDLE; neue E-Mails "
                        "werden über den regelmäßigen Abruf erkannt."
                    )
                    return
            except Exception as error:
                if self.stop_event.is_set():
                    break
                self._set_state("error", type(error).__name__)
                logger.warning(
                    self._prefix + "IMAP-IDLE-Verbindung unterbrochen (%s); neuer Versuch in %d s.",
                    type(error).__name__,
                    RETRY_SECONDS,
                )
                self.stop_event.wait(RETRY_SECONDS)
        self._set_state("stopped")

    def _session(self, settings: Settings) -> bool:
        """Eine Verbindung; kehrt bei Einstellungsänderung oder Beenden zurück."""
        self._set_state("connecting")
        context = ssl.create_default_context()
        with imaplib.IMAP4_SSL(
            settings.imap_host, settings.imap_port, ssl_context=context, timeout=30
        ) as client:
            client.login(settings.imap_username, settings.imap_password)
            if "IDLE" not in client.capabilities:
                return False
            status, _ = client.select(settings.imap_folder, readonly=True)
            if status != "OK":
                raise RuntimeError("IMAP-Ordner konnte nicht geöffnet werden")
            self._set_state("active")
            logger.info(self._prefix + "IMAP IDLE aktiv; neue E-Mails werden sofort erkannt.")
            while not self.stop_event.is_set():
                if self._settings_changed(settings):
                    logger.info("IMAP-Einstellungen geändert; IDLE verbindet neu.")
                    return True
                if self._idle_once(client):
                    logger.info(self._prefix + "Neue E-Mail vom Server gemeldet.")
                    self._on_new_mail()
        return True

    def _settings_changed(self, previous: Settings) -> bool:
        current = self._get_settings()
        return any(
            getattr(current, key) != getattr(previous, key)
            for key in ("imap_host", "imap_port", "imap_username", "imap_folder")
        )

    def _idle_once(self, client: imaplib.IMAP4_SSL) -> bool:
        """Ein IDLE-Durchgang; True, wenn neue Nachrichten gemeldet wurden."""
        tag = client._new_tag().decode("ascii")
        client.send(f"{tag} IDLE\r\n".encode("ascii"))
        response = client.readline()
        if not response.startswith(b"+"):
            raise RuntimeError("Server hat IDLE nicht bestätigt")
        started = time.monotonic()
        new_mail = False
        while (
            not new_mail
            and not self.stop_event.is_set()
            and time.monotonic() - started < RENEW_SECONDS
        ):
            sock = client.sock
            ready = sock.pending() if hasattr(sock, "pending") else 0
            if not ready:
                readable, _, _ = select.select([sock], [], [], WAKE_SECONDS)
                if not readable:
                    continue
            line = client.readline()
            if not line:
                raise ConnectionError("Verbindung vom Server geschlossen")
            if line.startswith(b"* BYE"):
                raise ConnectionError("Server hat die Verbindung beendet")
            if line.startswith(b"*") and (b"EXISTS" in line or b"RECENT" in line):
                new_mail = True
        client.send(b"DONE\r\n")
        while True:
            line = client.readline()
            if not line:
                raise ConnectionError("Verbindung vom Server geschlossen")
            if line.startswith(tag.encode("ascii")):
                if b" OK" not in line:
                    raise RuntimeError("IDLE wurde vom Server abgelehnt")
                break
            if line.startswith(b"*") and b"EXISTS" in line:
                new_mail = True
        return new_mail

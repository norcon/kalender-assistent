import email
import email.header
import email.policy
import email.utils
import html
import imaplib
import io
import logging
import math
import warnings
import mimetypes
import re
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import Message
from html.parser import HTMLParser
from typing import Any

import httpx
import pymupdf as fitz
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from .i18n import _
from . import ai, db, ics_import, person_learning
from .config import Settings

logger = logging.getLogger(__name__)
_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
# JPEG und PNG versteht jeder Vision-Server; alles andere wird umgewandelt.
_PASSTHROUGH_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png"}
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")
_MAX_DECODE_PIXELS = 40_000_000  # Schutz vor „Dekompressionsbomben“
_MAX_VISION_BYTES = 8 * 1024 * 1024
_MAX_VISION_IMAGES = 10
_MAX_VISION_PIXELS = 2_500_000


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    pieces = email.header.decode_header(value)
    decoded = []
    for part, charset in pieces:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                logger.warning("Unbekannte Zeichenkodierung im E-Mail-Header: %s", charset)
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded).strip()


def _decode_bytes(payload: bytes, charset: str) -> str:
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        logger.warning("Unbekannte Zeichenkodierung im E-Mail-Inhalt: %s", charset)
        return payload.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return html.unescape("".join(parser.parts))


def _vision_image(data: bytes) -> tuple[str, bytes] | None:
    """Bild so aufbereiten, dass jeder Vision-Server es annimmt.

    Maßgeblich ist der echte Inhalt, nicht der Typ im Mail-Header. JPEG und PNG
    gehen unverändert durch; WebP, GIF, BMP und TIFF werden zu PNG (mit
    Transparenz) oder JPEG und bei Bedarf verkleinert. None = nicht lesbar.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > _MAX_DECODE_PIXELS:
                    return None
                if image.format in _PASSTHROUGH_FORMATS:
                    return _PASSTHROUGH_FORMATS[image.format], data
                image.seek(0)  # bei Animationen das erste Bild
                image.load()
                pixels = image.width * image.height
                if pixels > _MAX_VISION_PIXELS:
                    scale = math.sqrt(_MAX_VISION_PIXELS / pixels)
                    image.thumbnail((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
                transparent = image.mode in ("RGBA", "LA", "PA") or (
                    image.mode == "P" and "transparency" in image.info
                )
                output = io.BytesIO()
                if transparent:
                    image.convert("RGBA").save(output, "PNG", optimize=True)
                    return "image/png", output.getvalue()
                image.convert("RGB").save(output, "JPEG", quality=85)
                return "image/jpeg", output.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError, ValueError, EOFError):
        return None


def _attachment_content(part: Message) -> tuple[str, list[dict[str, Any]]]:
    payload = part.get_payload(decode=True) or b""
    content_type = part.get_content_type().lower()
    filename = _decode_header(part.get_filename()).lower()

    if content_type == "application/pdf" or filename.endswith(".pdf"):
        try:
            reader = PdfReader(io.BytesIO(payload))
            page_text = [page.extract_text() or "" for page in reader.pages]
            images = []
            if any(not text.strip() for text in page_text):
                with fitz.open(stream=payload, filetype="pdf") as document:
                    for index, text in enumerate(page_text):
                        if text.strip():
                            continue
                        if index >= _MAX_VISION_IMAGES:
                            logger.warning(
                                "PDF-Anhang %s hat mehr als %s Seiten ohne Text; "
                                "übrige Seiten werden nicht an die Vision-KI gesendet.",
                                filename or "(ohne Dateiname)",
                                _MAX_VISION_IMAGES,
                            )
                            break
                        page = document[index]
                        page_rect = page.rect
                        page_area = max(1.0, page_rect.width * page_rect.height)
                        scale = min(1.5, math.sqrt(_MAX_VISION_PIXELS / page_area))
                        pixmap = page.get_pixmap(
                            matrix=fitz.Matrix(scale, scale), alpha=False
                        )
                        images.append(
                            {
                                "name": f"{filename or 'PDF'} – Seite {index + 1}",
                                "mime_type": "image/jpeg",
                                "data": pixmap.tobytes("jpg", jpg_quality=78),
                            }
                        )
            return "\n".join(page_text), images
        except Exception:
            logger.exception("PDF-Anhang konnte nicht als Text gelesen werden")
            return "", []
    guessed_type = mimetypes.guess_type(filename)[0] if filename else None
    image_type = (
        content_type
        if content_type in _SUPPORTED_IMAGE_TYPES
        else guessed_type or content_type
    )
    if image_type.startswith("image/") or filename.endswith(_IMAGE_EXTENSIONS):
        prepared = _vision_image(payload)
        if prepared is None:
            logger.warning(
                "Bildanhang %s (%s) lässt sich nicht lesen und wird nicht an die "
                "Vision-KI gesendet; unterstützt sind JPEG, PNG, WebP, GIF, BMP und TIFF.",
                filename or "(ohne Dateiname)",
                image_type,
            )
            return "", []
        mime_type, data = prepared
        return "", [
            {
                "name": filename or "Bildanhang",
                "mime_type": mime_type,
                "data": data,
            }
        ]
    if _is_calendar_part(part):
        return "", []  # wird direkt gelesen, siehe _calendar_parts
    if (
        content_type in {"text/plain", "text/html"}
        or filename.endswith((".txt", ".csv", ".html", ".htm"))
    ):
        charset = part.get_content_charset() or "utf-8"
        text = _decode_bytes(payload, charset)
        return (_html_to_text(text) if content_type == "text/html" else text), []
    return "", []


def _is_calendar_part(part: Message) -> bool:
    content_type = part.get_content_type().lower()
    filename = _decode_header(part.get_filename()).lower()
    return content_type in {"text/calendar", "application/ics"} or filename.endswith(".ics")


def _calendar_parts(message: Message) -> list[tuple[str, str]]:
    """Kalendereinladungen als (Name, Text) – Anhänge und eingebettete Teile."""
    parts = []
    seen: set[str] = set()
    for part in message.walk() if message.is_multipart() else [message]:
        if part.is_multipart() or not _is_calendar_part(part):
            continue
        payload = part.get_payload(decode=True) or b""
        text = _decode_bytes(payload, part.get_content_charset() or "utf-8")
        if "BEGIN:VEVENT" not in text.upper() or text in seen:
            continue
        seen.add(text)
        parts.append((_decode_header(part.get_filename()) or "Kalendereinladung", text))
    return parts


def _message_content(message: Message) -> tuple[str, list[dict[str, Any]]]:
    plain_parts = []
    html_parts = []
    attachment_parts = []
    images: list[dict[str, Any]] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = part.get_content_disposition()
            if disposition == "attachment" or part.get_filename():
                extracted, attached_images = _attachment_content(part)
                if extracted:
                    attachment_name = _decode_header(part.get_filename()) or "Datei"
                    attachment_parts.append((attachment_name, extracted))
                images.extend(attached_images)
                continue
            content_type = part.get_content_type().lower()
            if content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = _decode_bytes(payload, charset)
            (plain_parts if content_type == "text/plain" else html_parts).append(text)
    else:
        payload = message.get_payload(decode=True) or b""
        charset = message.get_content_charset() or "utf-8"
        content = _decode_bytes(payload, charset)
        (html_parts if message.get_content_type() == "text/html" else plain_parts).append(
            content
        )

    body = "\n".join(plain_parts) or "\n".join(_html_to_text(x) for x in html_parts)
    segments = [f"Nachrichtentext:\n{body}"] if body.strip() else []
    segments.extend(
        f"Anhang {name}:\n{text}"
        for name, text in attachment_parts
    )
    included_images = []
    total_bytes = 0
    for image in images:
        if len(included_images) >= _MAX_VISION_IMAGES:
            logger.warning(
                "Weitere Bildanhänge werden ausgelassen (Limit %s).",
                _MAX_VISION_IMAGES,
            )
            break
        if total_bytes + len(image["data"]) > _MAX_VISION_BYTES:
            logger.warning(
                "Bildanhang %s wird wegen des Vision-Datenlimits ausgelassen.",
                image["name"],
            )
            continue
        included_images.append(image)
        total_bytes += len(image["data"])
    return "\n\n".join(segments)[:50000], included_images


def _message_text(message: Message) -> str:
    return _message_content(message)[0]


def _received_at(message: Message) -> str:
    try:
        parsed = email.utils.parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError, OverflowError):
        logger.warning("Ungültiger Date-Header; verwende aktuelle Zeit")
        parsed = None
    if parsed is None:
        return datetime.now(timezone.utc).isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _uid_validity(client: imaplib.IMAP4_SSL) -> str:
    response = client.response("UIDVALIDITY")
    if response and response[1]:
        for item in response[1]:
            if item:
                match = re.search(rb"\d+", item)
                if match:
                    return match.group().decode("ascii")
    raise RuntimeError("IMAP-Server lieferte keine UIDVALIDITY")


def _message_size(data: list[Any]) -> int | None:
    for item in data:
        response = item[0] if isinstance(item, tuple) and item else item
        if isinstance(response, bytes):
            match = re.search(rb"RFC822\.SIZE\s+(\d+)", response, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def _fetch_raw_message(
    client: imaplib.IMAP4_SSL, uid: int, max_bytes: int
) -> bytes | None:
    status, data = client.uid("fetch", str(uid), "(RFC822.SIZE)")
    if status != "OK":
        raise RuntimeError(f"IMAP konnte Größe von UID {uid} nicht lesen")
    size = _message_size(data)
    if size is None:
        raise RuntimeError(f"IMAP lieferte keine Größe für UID {uid}")
    if size > max_bytes:
        logger.warning("Überspringe E-Mail UID %s mit %s Bytes (Limit %s)", uid, size, max_bytes)
        return None

    status, data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError(f"IMAP konnte E-Mail UID {uid} nicht abrufen")
    for item in data:
        if isinstance(item, tuple) and len(item) > 1:
            return item[1]
    raise RuntimeError(f"IMAP lieferte keinen Inhalt für UID {uid}")


def _search_uids(
    client: imaplib.IMAP4_SSL, last_uid: int, initial_days: int
) -> list[int]:
    if last_uid == 0:
        since = (datetime.now() - timedelta(days=initial_days)).strftime("%d-%b-%Y")
        status, data = client.uid("search", None, "SINCE", since)
    else:
        status, data = client.uid("search", None, "UID", f"{last_uid + 1}:*")
    if status != "OK":
        raise RuntimeError("IMAP-Suche nach neuen E-Mails fehlgeschlagen")
    raw_uids = data[0].split() if data and data[0] else []
    return sorted(
        {
            int(uid)
            for uid in raw_uids
            if uid.isdigit() and (last_uid == 0 or int(uid) > last_uid)
        }
    )


MAX_AI_ATTEMPTS = 3
# HTTP-Status, die auf ein Problem mit genau dieser Anfrage hindeuten. 401/403/404,
# 429 und 5xx deuten dagegen auf Konfiguration oder Serverzustand hin.
_MESSAGE_SPECIFIC_HTTP_STATUS = {400, 413, 415, 422}


def _is_message_specific_error(error: Exception) -> bool:
    """Fehler, die bei jeder Wiederholung derselben Mail erneut auftreten würden."""
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _MESSAGE_SPECIFIC_HTTP_STATUS
    if isinstance(error, httpx.HTTPError):
        return False
    # ValueError umfasst auch ungültiges JSON in der KI-Antwort.
    return isinstance(error, (ValueError, KeyError, IndexError, TypeError))


def _error_summary(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return f"{type(error).__name__}: {error}"


def is_bulk_mail(message: Message) -> bool:
    """Massenmail (Newsletter, Werbung): Abmelde-Kennzeichen im Mail-Kopf."""
    return bool(message.get("List-Unsubscribe"))


def _mark_newsletter(
    settings: Settings, message: Message, source: dict[str, str], events: list[dict[str, Any]]
) -> None:
    threshold = getattr(settings, "newsletter_min_confidence_percent", 95) / 100
    if not threshold or not is_bulk_mail(message):
        return
    uncertain = [event for event in events if float(event.get("confidence", 0)) < threshold]
    if not uncertain or db.sender_is_trusted(settings.database_path, source["sender"]):
        return
    for event in uncertain:
        event["hidden_reason"] = db.NEWSLETTER_REASON
    logger.info(
        "E-Mail „%s“ von %s ist ein Newsletter; %d Vorschlag/Vorschläge unter "
        "%d %% Sicherheit landen unter „Ausgeblendet“.",
        source["subject"],
        source["sender"],
        len(uncertain),
        round(threshold * 100),
    )


def message_source(message: Message) -> dict[str, str]:
    references = " ".join(
        _decode_header(value)
        for header in ("In-Reply-To", "References")
        for value in message.get_all(header, [])
    )
    return {
        "subject": _decode_header(message.get("Subject")) or "(ohne Betreff)",
        "sender": _decode_header(message.get("From")),
        "received_at": _received_at(message),
        "message_id": _decode_header(message.get("Message-ID")),
        "references": references,
    }


def _target_calendars(settings: Settings) -> list[dict[str, Any]]:
    try:
        return db.list_target_calendars(settings.database_path)
    except Exception as error:
        logger.warning("Zielkalender konnten nicht gelesen werden: %s", error)
        return []


def _record_ai_run(settings: Settings, seconds: float, *, ok: bool) -> None:
    try:
        db.record_ai_run(settings.database_path, seconds, ok, settings.ai_model)
    except Exception as error:  # Kennzahl, darf die Auswertung nie stören
        logger.debug("KI-Laufzeit konnte nicht gespeichert werden: %s", error)


def analyze_message(
    settings: Settings,
    message: Message,
    source: dict[str, str],
    label: str,
    *,
    use_ai: bool = True,
) -> list[dict[str, Any]]:
    """Termine einer Mail: Kalendereinladungen direkt, Rest per KI.

    Danach gelernte Kalenderkorrekturen, Newsletter-Filter und Absender-Regeln.
    Fehler der KI werden an den Aufrufer weitergereicht.
    """
    content, images = _message_content(message)
    ics_events: list[dict[str, Any]] = []
    calendar_parts = _calendar_parts(message)
    for part_name, part_text in calendar_parts:
        try:
            ics_events.extend(
                ics_import.parse_invitation(part_text, settings.caldav_timezone, part_name)
            )
        except Exception as error:
            logger.warning(
                "Kalendereinladung %s konnte nicht gelesen werden (%s).",
                part_name,
                type(error).__name__,
            )
    if ics_events:
        logger.info(
            "%s (Betreff: %s): %d Termin(e) direkt aus der Kalendereinladung übernommen.",
            label,
            source["subject"],
            len(ics_events),
        )
    elif calendar_parts:
        # Nicht lesbar: der KI als Text mitgeben
        content = "\n\n".join(
            [content] + [f"Anhang {name}:\n{text}" for name, text in calendar_parts]
        )[:50000]

    events: list[dict[str, Any]] = []
    if use_ai and (content.strip() or images):
        logger.info(
            "Starte LLM-Auswertung für %s (Modell %s): %s",
            label,
            settings.ai_model,
            source["subject"],
        )
        started = time.monotonic()
        try:
            events = ai.extract_events(
                settings.ai_base_url,
                settings.ai_api_key,
                settings.ai_model,
                settings.ai_timeout_seconds,
                source["subject"],
                source["sender"],
                source["received_at"],
                content,
                images=images,
                calendars=_target_calendars(settings),
                rejections=db.recent_rejections(settings.database_path),
            )
        except Exception:
            _record_ai_run(settings, time.monotonic() - started, ok=False)
            raise
        _record_ai_run(settings, time.monotonic() - started, ok=True)
        logger.info(
            "LLM-Auswertung für %s (Betreff: %s) abgeschlossen nach %d s; "
            "%d Terminvorschlag/-vorschläge erkannt.",
            label,
            source["subject"],
            round(time.monotonic() - started),
            len(events),
        )
    if ics_events:
        events, dropped = ics_import.drop_duplicates_of(events, ics_events)
        if dropped:
            logger.info(
                "%d KI-Fund(e) verworfen, weil die Kalendereinladung denselben "
                "Termin enthält.",
                dropped,
            )
        events = ics_events + events
    if not events:
        return events
    for event in events:
        event["person_source"] = "ai" if event.get("person") else "unassigned"
    corrections = db.list_person_corrections(
        settings.database_path, person_learning.sender_key(source["sender"])
    )
    events, learned_count = person_learning.apply_person_corrections(
        events, corrections, source["sender"]
    )
    if learned_count:
        logger.info(
            "Manuelle Kalenderkorrektur auf %d ähnlichen Terminvorschlag/-vorschläge "
            "angewendet.",
            learned_count,
        )
    _mark_newsletter(settings, message, source, events)
    rule = db.matching_ignore_rule(settings.database_path, source["sender"])
    if rule:
        for event in events:
            event["hidden_reason"] = rule
        logger.info(
            "Absender %s ist ausgeblendet (Regel: %s); %d Vorschlag/Vorschläge aus "
            "„%s“ landen unter „Ausgeblendet“.",
            source["sender"],
            rule,
            len(events),
            source["subject"],
        )
    return events


class ReanalysisError(Exception):
    """Verständliche Meldung, warum eine Mail nicht erneut ausgewertet werden kann."""


def fetch_for_reanalysis(settings: Settings, source_key: str) -> Message:
    """Eine bereits verarbeitete Mail lesend aus dem Postfach holen."""
    try:
        mailbox_key, uid_validity, uid_text = source_key.rsplit("/", 2)
        uid = int(uid_text)
    except ValueError as error:
        raise ReanalysisError(_("Unbekannte Mail-Kennung.")) from error
    prefix = f"{settings.imap_username}@{settings.imap_host}/"
    if not mailbox_key.startswith(prefix):
        raise ReanalysisError(_("Die Mail stammt aus einem anderen Postfach."))
    folder = mailbox_key[len(prefix):]
    context = ssl.create_default_context()
    with imaplib.IMAP4_SSL(
        settings.imap_host, settings.imap_port, ssl_context=context, timeout=30
    ) as client:
        client.login(settings.imap_username, settings.imap_password)
        status, _response = client.select(folder, readonly=True)
        if status != "OK":
            raise ReanalysisError(_("Der IMAP-Ordner „{folder}“ ist nicht mehr vorhanden.", folder=folder))
        if _uid_validity(client) != uid_validity:
            raise ReanalysisError(_(
                "Der Ordner wurde auf dem Mailserver neu aufgebaut; die Mail lässt "
                "sich nicht mehr eindeutig finden."
            ))
        try:
            raw = _fetch_raw_message(client, uid, settings.imap_max_message_bytes)
        except RuntimeError:
            raw = None  # UID existiert nicht mehr
    if raw is None:
        raise ReanalysisError(
            _("Die Mail ist nicht mehr im Postfach (gelöscht oder verschoben) oder zu groß.")
        )
    return email.message_from_bytes(raw, policy=email.policy.default)


def poll_mailbox(settings: Settings) -> None:
    logger.info("Verbinde mit dem IMAP-Postfach.")
    mailbox_key = f"{settings.imap_username}@{settings.imap_host}/{settings.imap_folder}"
    context = ssl.create_default_context()
    with imaplib.IMAP4_SSL(
        settings.imap_host, settings.imap_port, ssl_context=context, timeout=30
    ) as client:
        client.login(settings.imap_username, settings.imap_password)
        status, _response = client.select(settings.imap_folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP-Ordner {settings.imap_folder!r} konnte nicht geöffnet werden")

        uid_validity = _uid_validity(client)
        stored_validity, last_uid = db.get_mailbox_state(
            settings.database_path, mailbox_key
        )
        if stored_validity != uid_validity:
            db.reset_mailbox_state(settings.database_path, mailbox_key, uid_validity)
            last_uid = 0

        uids = _search_uids(
            client, last_uid, settings.imap_initial_lookback_days
        )[:25]
        logger.info("%d E-Mail(s) für diesen Durchlauf gefunden.", len(uids))
        for index, uid in enumerate(uids, start=1):
            logger.info("Lade E-Mail %d/%d (UID %d).", index, len(uids), uid)
            source_key = f"{mailbox_key}/{uid_validity}/{uid}"
            raw = _fetch_raw_message(client, uid, settings.imap_max_message_bytes)
            events: list[dict[str, Any]] = []
            source = {
                "subject": "(ohne Betreff)",
                "sender": "",
                "received_at": "",
                "message_id": "",
                "references": "",
            }
            if raw is not None:
                message = email.message_from_bytes(raw, policy=email.policy.default)
                source = message_source(message)
                logger.info(
                    "E-Mail %d/%d (UID %d): Betreff: %s",
                    index,
                    len(uids),
                    uid,
                    source["subject"],
                )
                try:
                    events = analyze_message(
                        settings, message, source, f"E-Mail {index}/{len(uids)} (UID {uid})"
                    )
                except Exception as error:
                    if isinstance(error, ValueError):
                        logger.error(
                            "KI-Auswertung für E-Mail UID %d (Betreff: %s) "
                            "fehlgeschlagen: %s",
                            uid,
                            source["subject"],
                            error,
                        )
                    else:
                        logger.error(
                            "LLM-Auswertung für E-Mail %d/%d (UID %d, Betreff: %s) "
                            "fehlgeschlagen (%s).",
                            index,
                            len(uids),
                            uid,
                            source["subject"],
                            type(error).__name__,
                        )
                    if not _is_message_specific_error(error):
                        raise
                    attempts = db.record_message_failure(
                        settings.database_path,
                        source_key,
                        _error_summary(error),
                        source,
                    )
                    if attempts < MAX_AI_ATTEMPTS:
                        logger.warning(
                            "E-Mail UID %d (Betreff: %s): Versuch %d von %d "
                            "fehlgeschlagen; neuer Versuch beim nächsten Abruf.",
                            uid,
                            source["subject"],
                            attempts,
                            MAX_AI_ATTEMPTS,
                        )
                        raise
                    logger.warning(
                        "E-Mail UID %d (Betreff: %s) wird nach %d "
                        "fehlgeschlagenen KI-Auswertungen übersprungen.",
                        uid,
                        source["subject"],
                        attempts,
                    )
                    db.mark_message_skipped(settings.database_path, source_key)
                    # Termine aus Kalendereinladungen gehen trotzdem nicht verloren.
                    events = analyze_message(
                        settings, message, source, f"E-Mail UID {uid}", use_ai=False
                    )
            db.save_processed_message(
                settings.database_path,
                mailbox_key,
                uid_validity,
                uid,
                source_key,
                events,
                source,
            )
            logger.info(
                "E-Mail verarbeitet (UID %d, Betreff: %s); "
                "%d Terminvorschlag/-vorschläge erkannt.",
                uid,
                source["subject"],
                len(events),
            )

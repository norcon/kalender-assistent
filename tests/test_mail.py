import os
import tempfile
import unittest
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import io
import pymupdf
from PIL import Image

from src import db, mail
from src.mail import (
    _message_content,
    _message_size,
    _message_text,
    _received_at,
    _search_uids,
    poll_mailbox,
)


def _image_bytes(format_name, mode="RGB", size=(40, 30)):
    output = io.BytesIO()
    Image.new(mode, size, (200, 80, 40, 128)[: len(mode)]).save(output, format_name)
    return output.getvalue()


class MessageTextTests(unittest.TestCase):
    def test_uid_search_ignores_server_returning_last_uid_for_open_ended_range(self):
        class FakeImapClient:
            def __init__(self):
                self.args = None

            def uid(self, *args):
                self.args = args
                return "OK", [b"29699 29700 29701 29700"]

        client = FakeImapClient()
        self.assertEqual(_search_uids(client, 29699, 14), [29700, 29701])
        self.assertEqual(client.args, ("search", None, "UID", "29700:*"))

    def test_initial_uid_search_deduplicates_server_results(self):
        class FakeImapClient:
            def uid(self, *args):
                return "OK", [b"21 19 21 20"]

        self.assertEqual(_search_uids(FakeImapClient(), 0, 14), [19, 20, 21])

    def test_reads_size_from_bytes_or_tuple_imap_response(self):
        self.assertEqual(_message_size([b"42 (RFC822.SIZE 1234)"]), 1234)
        self.assertEqual(_message_size([(b"42 (RFC822.SIZE 5678)", b"")]), 5678)
        self.assertIsNone(_message_size([b"42 (UID 42)"]))

    def test_extracts_plain_body_and_calendar_attachment(self):
        message = EmailMessage()
        message.set_content("Bitte am Freitag um 15 Uhr zum Termin kommen.")
        message.add_alternative("<p>HTML-Kopie</p>", subtype="html")
        message.add_attachment(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Elternabend\nEND:VEVENT\nEND:VCALENDAR\n",
            subtype="calendar",
            filename="termin.ics",
        )

        extracted = _message_text(message)

        self.assertIn("Bitte am Freitag um 15 Uhr", extracted)
        # Einladungen werden direkt gelesen (ics_import), nicht an die KI geschickt.
        self.assertNotIn("SUMMARY:Elternabend", extracted)
        self.assertEqual([name for name, _ in mail._calendar_parts(message)], ["termin.ics"])
        self.assertNotIn("HTML-Kopie", extracted)

    def test_extracts_image_attachment_for_vision_model(self):
        png = _image_bytes("PNG")
        message = EmailMessage()
        message.set_content("Bitte Einladung beachten.")
        message.add_attachment(
            png,
            maintype="image",
            subtype="png",
            filename="einladung.png",
        )

        text, images = _message_content(message)

        self.assertIn("Bitte Einladung beachten.", text)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["name"], "einladung.png")
        self.assertEqual(images[0]["mime_type"], "image/png")
        self.assertEqual(images[0]["data"], png)  # PNG geht unverändert durch

    def test_converts_webp_gif_and_mislabelled_images(self):
        cases = [
            ("album-thumbnail.webp", "webp", _image_bytes("WEBP"), "image/jpeg"),
            ("logo.webp", "webp", _image_bytes("WEBP", mode="RGBA"), "image/png"),
            ("animiert.gif", "gif", _image_bytes("GIF"), "image/jpeg"),
            ("falsch.jpg", "jpeg", _image_bytes("WEBP"), "image/jpeg"),  # Header lügt
        ]
        for name, subtype, data, expected in cases:
            with self.subTest(name):
                message = EmailMessage()
                message.set_content("Text")
                message.add_attachment(data, maintype="image", subtype=subtype, filename=name)
                images = _message_content(message)[1]
                self.assertEqual(images[0]["mime_type"], expected)
                magic = b"\x89PNG" if expected == "image/png" else b"\xff\xd8"
                self.assertTrue(images[0]["data"].startswith(magic))

    def test_large_converted_image_is_downscaled(self):
        message = EmailMessage()
        message.set_content("Text")
        message.add_attachment(_image_bytes("WEBP", size=(3000, 2000)), maintype="image", subtype="webp", filename="gross.webp")
        data = _message_content(message)[1][0]["data"]
        with Image.open(io.BytesIO(data)) as image:
            self.assertLessEqual(image.width * image.height, mail._MAX_VISION_PIXELS)

    def test_unreadable_image_is_skipped_not_fatal(self):
        message = EmailMessage()
        message.set_content("Bitte lesen.")
        message.add_attachment(b"kein bild", maintype="image", subtype="webp", filename="kaputt.webp")
        message.add_attachment(b"<svg/>", maintype="image", subtype="svg+xml", filename="icon.svg")
        with self.assertLogs("src.mail", "WARNING"):
            text, images = _message_content(message)
        self.assertEqual(images, [])
        self.assertIn("Bitte lesen.", text)

    def test_renders_textless_pdf_page_for_vision_model(self):
        document = pymupdf.open()
        document.new_page(width=200, height=200)
        pdf_data = document.tobytes()
        document.close()
        message = EmailMessage()
        message.set_content("Bitte Einladung beachten.")
        message.add_attachment(
            pdf_data,
            maintype="application",
            subtype="pdf",
            filename="einladung.pdf",
        )

        text, images = _message_content(message)

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["name"], "einladung.pdf – Seite 1")
        self.assertEqual(images[0]["mime_type"], "image/jpeg")
        self.assertTrue(images[0]["data"].startswith(b"\xff\xd8"))
        self.assertEqual(
            text.strip(), "Nachrichtentext:\nBitte Einladung beachten."
        )

    def test_invalid_date_header_does_not_block_message(self):
        message = EmailMessage()
        message["Date"] = "not a valid date"
        self.assertTrue(_received_at(message))

    def test_mail_check_logs_subject_and_llm_progress(self):
        message = EmailMessage()
        message["Subject"] = "Elternabend am Donnerstag"
        message.set_content("Vertraulicher E-Mail-Inhalt")

        class FakeImapClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def login(self, *_args):
                return "OK", []

            def select(self, *_args, **_kwargs):
                return "OK", []

            def response(self, _name):
                return "UIDVALIDITY", [b"123"]

            def uid(self, *_args):
                return "OK", [b"42"]

        settings = SimpleNamespace(
            imap_username="user",
            imap_password="password",
            imap_host="imap.example.org",
            imap_folder="INBOX",
            imap_port=993,
            database_path=":memory:",
            imap_initial_lookback_days=14,
            imap_max_message_bytes=1024,
            ai_base_url="http://llm.example.org/v1",
            ai_api_key="",
            ai_model="test-model",
            ai_timeout_seconds=30,
        )

        with (
            self.assertLogs("src.mail", level="INFO") as captured,
            patch.object(mail.imaplib, "IMAP4_SSL", return_value=FakeImapClient()),
            patch.object(mail, "_fetch_raw_message", return_value=message.as_bytes()),
            patch.object(mail, "_message_content", return_value=("Text", [])),
            patch.object(mail.db, "get_mailbox_state", return_value=("123", 0)),
            patch.object(mail.db, "list_person_corrections", return_value=[]),
            patch.object(mail.db, "save_processed_message"),
            patch.object(mail.ai, "extract_events", return_value=[]),
            patch.object(
                mail.person_learning, "apply_person_corrections", return_value=([], 0)
            ),
        ):
            poll_mailbox(settings)

        output = "\n".join(captured.output)
        self.assertIn("Betreff: Elternabend am Donnerstag", output)
        self.assertIn("Starte LLM-Auswertung", output)
        self.assertIn("test-model", output)
        self.assertIn("LLM-Auswertung", output)
        self.assertNotIn("Vertraulicher E-Mail-Inhalt", output)


class IgnoredSenderMailTests(unittest.TestCase):
    def _fake_client(self):
        class FakeImapClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def login(self, *_args):
                return "OK", []

            def select(self, *_args, **_kwargs):
                return "OK", []

            def response(self, _name):
                return "UIDVALIDITY", [b"123"]

            def uid(self, *_args):
                return "OK", [b"42"]

        return FakeImapClient()

    def _fake_settings(self):
        return SimpleNamespace(
            imap_username="user", imap_password="password", imap_host="imap.example.org",
            imap_folder="INBOX", imap_port=993, database_path=":memory:",
            imap_initial_lookback_days=14, imap_max_message_bytes=1024 * 1024,
            ai_base_url="http://llm.example.org/v1", ai_api_key="", ai_model="test-model",
            ai_timeout_seconds=30, caldav_timezone="Europe/Berlin",
            newsletter_min_confidence_percent=95,
        )

    def test_events_from_ignored_sender_are_saved_hidden(self):
        message = EmailMessage()
        message["Subject"] = "Ihr Paket kommt heute"
        message["From"] = "Versand <versand@amazon.de>"
        message.set_content("Zustellung heute")

        class FakeImapClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def login(self, *_args):
                return "OK", []

            def select(self, *_args, **_kwargs):
                return "OK", []

            def response(self, _name):
                return "UIDVALIDITY", [b"123"]

            def uid(self, *_args):
                return "OK", [b"42"]

        settings = SimpleNamespace(
            imap_username="user", imap_password="password", imap_host="imap.example.org",
            imap_folder="INBOX", imap_port=993, database_path=":memory:",
            imap_initial_lookback_days=14, imap_max_message_bytes=1024,
            ai_base_url="http://llm.example.org/v1", ai_api_key="", ai_model="test-model",
            ai_timeout_seconds=30,
        )
        events = [{"title": "Paket", "start": "2099-01-01", "confidence": 1.0}]
        with (
            self.assertLogs("src.mail", level="INFO") as captured,
            patch.object(mail.imaplib, "IMAP4_SSL", return_value=FakeImapClient()),
            patch.object(mail, "_fetch_raw_message", return_value=message.as_bytes()),
            patch.object(mail, "_message_content", return_value=("Text", [])),
            patch.object(mail.db, "get_mailbox_state", return_value=("123", 0)),
            patch.object(mail.db, "list_person_corrections", return_value=[]),
            patch.object(mail.db, "matching_ignore_rule", return_value="amazon.de"),
            patch.object(mail.db, "save_processed_message") as save,
            patch.object(mail.ai, "extract_events", return_value=events),
        ):
            poll_mailbox(settings)

        saved_events = save.call_args.args[5]
        self.assertEqual(saved_events[0]["hidden_reason"], "amazon.de")
        self.assertIn("Regel: amazon.de", "\n".join(captured.output))


class InvitationPollTests(IgnoredSenderMailTests):
    def test_poll_combines_invitation_and_ai_without_duplicates(self):
        message = EmailMessage()
        message["Subject"] = "Einladung Elternabend"
        message["From"] = "Schule <info@schule.example>"
        message.set_content("Elternabend am 20.11. um 19:30, außerdem Schulfest am 28.11.")
        message.add_attachment(
            b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:e1\nSUMMARY:Elternabend\nDTSTART;TZID=Europe/Berlin:20261120T193000\nEND:VEVENT\nEND:VCALENDAR\n",
            maintype="text", subtype="calendar", filename="einladung.ics",
        )
        client = self._fake_client()
        settings = self._fake_settings()
        ai_events = [
            {"title": "Elternabend", "start": "2026-11-20T19:30", "confidence": 0.9},
            {"title": "Schulfest", "start": "2026-11-28", "confidence": 0.9},
        ]
        with (
            self.assertLogs("src.mail", level="INFO") as captured,
            patch.object(mail.imaplib, "IMAP4_SSL", return_value=client),
            patch.object(mail, "_fetch_raw_message", return_value=message.as_bytes()),
            patch.object(mail.db, "get_mailbox_state", return_value=("123", 0)),
            patch.object(mail.db, "list_person_corrections", return_value=[]),
            patch.object(mail.db, "matching_ignore_rule", return_value=None),
            patch.object(mail.db, "save_processed_message") as save,
            patch.object(mail.ai, "extract_events", return_value=ai_events) as extract,
            patch.object(mail.db, "record_ai_run") as record,
        ):
            poll_mailbox(settings)
        self.assertNotIn("BEGIN:VEVENT", extract.call_args.args[7])
        self.assertEqual(record.call_args.args[2:], (True, "test-model"))
        saved = save.call_args.args[5]
        self.assertEqual([(e["title"], e.get("quote_source")) for e in saved], [("Elternabend", "ics"), ("Schulfest", None)])
        self.assertIn("direkt aus der Kalendereinladung", "\n".join(captured.output))


class ReanalysisFetchTests(IgnoredSenderMailTests):
    def test_fetch_checks_mailbox_validity_and_presence(self):
        settings = self._fake_settings()
        message = EmailMessage()
        message["Subject"] = "Noch einmal"
        message.set_content("Text")
        with patch.object(mail.imaplib, "IMAP4_SSL", return_value=self._fake_client()):
            with self.assertRaisesRegex(mail.ReanalysisError, "anderen Postfach"):
                mail.fetch_for_reanalysis(settings, "other@imap.example.org/INBOX/123/42")
            with self.assertRaisesRegex(mail.ReanalysisError, "neu aufgebaut"):
                mail.fetch_for_reanalysis(settings, "user@imap.example.org/INBOX/999/42")
            with patch.object(mail, "_fetch_raw_message", side_effect=RuntimeError("keine Größe")):
                with self.assertRaisesRegex(mail.ReanalysisError, "nicht mehr im Postfach"):
                    mail.fetch_for_reanalysis(settings, "user@imap.example.org/INBOX/123/42")
            with patch.object(mail, "_fetch_raw_message", return_value=message.as_bytes()) as fetch:
                result = mail.fetch_for_reanalysis(settings, "user@imap.example.org/Familie/Schule/123/42")
        self.assertEqual(result["Subject"], "Noch einmal")
        self.assertEqual(fetch.call_args.args[1], 42)


class CalendarPartTests(unittest.TestCase):
    def test_invitation_is_read_separately_and_not_sent_to_ai(self):
        message = EmailMessage()
        message["Subject"] = "Einladung"
        message.set_content("Hallo, anbei die Einladung.")
        message.add_alternative("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Inline\nDTSTART:20261120T100000\nEND:VEVENT\nEND:VCALENDAR\n", subtype="calendar")
        message.add_attachment(b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:2\nSUMMARY:Anhang\nDTSTART:20261121T100000\nEND:VEVENT\nEND:VCALENDAR\n", maintype="application", subtype="octet-stream", filename="termin.ics")
        parts = mail._calendar_parts(message)
        self.assertEqual([name for name, _ in parts], ["Kalendereinladung", "termin.ics"])
        content, _ = mail._message_content(message)
        self.assertIn("anbei die Einladung", content)
        self.assertNotIn("BEGIN:VEVENT", content)


class NewsletterTests(unittest.TestCase):
    def _settings(self, percent=95):
        return SimpleNamespace(database_path=":memory:", newsletter_min_confidence_percent=percent)

    def _message(self, bulk=True):
        message = EmailMessage()
        if bulk:
            message["List-Unsubscribe"] = "<mailto:abmelden@shop.de>"
        return message

    def test_uncertain_events_from_bulk_mail_are_hidden(self):
        events = [{"title": "Rabatt", "confidence": 0.8}, {"title": "Messe", "confidence": 0.97}]
        source = {"subject": "Angebote", "sender": "news@shop.de"}
        with patch.object(mail.db, "sender_is_trusted", return_value=False):
            mail._mark_newsletter(self._settings(), self._message(), source, events)
        self.assertEqual(events[0].get("hidden_reason"), "newsletter")
        self.assertNotIn("hidden_reason", events[1])

    def test_trusted_sender_normal_mail_and_disabled_filter_are_untouched(self):
        source = {"subject": "Angebote", "sender": "news@shop.de"}
        for settings, message, trusted in (
            (self._settings(), self._message(), True),
            (self._settings(), self._message(bulk=False), False),
            (self._settings(0), self._message(), False),
        ):
            events = [{"title": "Rabatt", "confidence": 0.5}]
            with patch.object(mail.db, "sender_is_trusted", return_value=trusted):
                mail._mark_newsletter(settings, message, source, events)
            self.assertNotIn("hidden_reason", events[0])


class PoisonMessageTests(unittest.TestCase):
    class FakeImapClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def login(self, *_args):
            return "OK", []

        def select(self, *_args, **_kwargs):
            return "OK", []

        def response(self, _name):
            return "UIDVALIDITY", [b"123"]

        def uid(self, *_args):
            return "OK", [b"42 43"]

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = SimpleNamespace(
            imap_username="user",
            imap_password="password",
            imap_host="imap.example.org",
            imap_folder="INBOX",
            imap_port=993,
            database_path=os.path.join(self.directory.name, "test.sqlite3"),
            imap_initial_lookback_days=14,
            imap_max_message_bytes=1024 * 1024,
            ai_base_url="http://llm.example.org/v1",
            ai_api_key="",
            ai_model="test-model",
            ai_timeout_seconds=30,
        )
        db.initialize(self.settings.database_path)

    def _poll(self, extract_events):
        def fetch(_client, uid, _max_bytes):
            message = EmailMessage()
            message["Subject"] = "Kaputte Mail" if uid == 42 else "Gute Mail"
            message.set_content("Text")
            return message.as_bytes()

        with (
            patch.object(mail.imaplib, "IMAP4_SSL", return_value=self.FakeImapClient()),
            patch.object(mail, "_fetch_raw_message", side_effect=fetch),
            patch.object(mail.ai, "extract_events", side_effect=extract_events),
            self.assertLogs("src.mail", level="INFO"),
        ):
            mail.poll_mailbox(self.settings)

    def _last_uid(self):
        return db.get_mailbox_state(
            self.settings.database_path, "user@imap.example.org/INBOX"
        )[1]

    def test_message_is_skipped_after_repeated_message_specific_failures(self):
        def fail_for_broken_mail(*args, **_kwargs):
            if args[4] == "Kaputte Mail":
                raise ValueError("ungültige KI-Antwort")
            return []

        for _ in range(mail.MAX_AI_ATTEMPTS - 1):
            with self.assertRaises(ValueError):
                self._poll(fail_for_broken_mail)
            self.assertEqual(self._last_uid(), 0)

        # Dritter Fehlschlag: Mail 42 wird übersprungen, Mail 43 verarbeitet.
        self._poll(fail_for_broken_mail)
        self.assertEqual(self._last_uid(), 43)
        skipped = db.list_skipped_messages(self.settings.database_path)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["source_subject"], "Kaputte Mail")
        self.assertEqual(skipped[0]["attempts"], mail.MAX_AI_ATTEMPTS)

    def test_infrastructure_errors_never_skip_messages(self):
        request = httpx.Request("POST", "http://llm.example.org/v1/chat/completions")

        for error in (
            httpx.ConnectError("down", request=request),
            httpx.ReadTimeout("slow", request=request),
            httpx.HTTPStatusError(
                "busy", request=request, response=httpx.Response(503, request=request)
            ),
        ):
            for _ in range(mail.MAX_AI_ATTEMPTS + 1):
                with self.assertRaises(type(error)):
                    self._poll(error)
        self.assertEqual(self._last_uid(), 0)
        self.assertEqual(db.list_skipped_messages(self.settings.database_path), [])


if __name__ == "__main__":
    unittest.main()

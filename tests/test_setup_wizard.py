import importlib.util
import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from src import setup_wizard
from src.config import Settings

_spec = importlib.util.spec_from_file_location("updater_for_setup", Path(__file__).resolve().parent.parent / "updater" / "updater.py")
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)


class EnvTests(unittest.TestCase):
    VALUES = ["abc", "pa$word", "it's \\ back$slash \"q\" #h", "a b ", "0.17.0", "http://host.docker.internal:1234/v1", ""]

    def test_quoting_matches_updater_and_round_trips(self):
        for value in self.VALUES:
            quoted = setup_wizard.quote_env_value(value)
            self.assertEqual(quoted, updater.quote_env_value(value), value)
            self.assertEqual(updater._unquote(quoted), value, value)
        with self.assertRaises(ValueError):
            setup_wizard.quote_env_value("zwei\nzeilen")

    def test_env_values_and_text(self):
        values = setup_wizard.env_values({
            "imap_host": " imap.example.org ", "imap_username": "a@b.de", "imap_password": " geheim$ ",
            "ai_base_url": "http://ki:1234/v1", "ai_model": "modell", "timezone": "Europe/Berlin", "language": "en",
        })
        self.assertEqual(values["IMAP_HOST"], "imap.example.org")
        self.assertEqual(values["IMAP_PASSWORD"], " geheim$ ")  # Passwörter unverändert
        self.assertEqual(values["IMAP_FOLDER"], "INBOX")
        self.assertEqual(values["CALDAV_URL"], "https://caldav.icloud.com")
        text = setup_wizard.env_text(values)
        self.assertIn("IMAP_PASSWORD=' geheim$ '\n", text)
        self.assertNotIn("CALDAV_USERNAME=", text)  # leere Werte entfallen
        self.assertEqual(set(values) - updater.SETUP_KEYS, set())
        self.assertNotIn("CONNECTIONS_EDIT_WITHOUT_PASSWORD", values)  # nur wenn gewählt
        base = {"imap_host": "h", "imap_username": "u", "imap_password": "p", "edit_without_password": True}
        allowed = setup_wizard.env_values(base)
        self.assertEqual(allowed["CONNECTIONS_EDIT_WITHOUT_PASSWORD"], "1")
        self.assertEqual(set(allowed) - updater.SETUP_KEYS, set())
        # mit Passwort überflüssig
        self.assertNotIn("CONNECTIONS_EDIT_WITHOUT_PASSWORD", setup_wizard.env_values({**base, "app_password": "12345678"}))
        with patch.dict(os.environ, {"CONNECTIONS_EDIT_WITHOUT_PASSWORD": "1"}):
            self.assertTrue(Settings.from_env().connections_without_password)

    def test_setup_mode_without_credentials(self):
        with patch.dict(os.environ, {"IMAP_HOST": "", "IMAP_USERNAME": "", "IMAP_PASSWORD": "", "DEMO_MODE": "0"}):
            self.assertTrue(Settings.from_env().setup_mode)
        with patch.dict(os.environ, {"IMAP_HOST": "h", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p", "DEMO_MODE": "0"}):
            self.assertFalse(Settings.from_env().setup_mode)
        with patch.dict(os.environ, {"IMAP_HOST": "", "DEMO_MODE": "1"}):
            self.assertFalse(Settings.from_env().setup_mode)


class CheckTests(unittest.TestCase):
    def test_imap_test_lists_folders_and_reports_login_errors(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Schule"', b'(\\HasNoChildren) "." Archiv'])
        client.select.return_value = ("OK", [b"3"])
        with patch.object(setup_wizard.imaplib, "IMAP4_SSL", return_value=client):
            self.assertEqual(setup_wizard.imap_test("h", 993, "u", "p", "INBOX"), ["Archiv", "INBOX", "Schule"])
            client.select.return_value = ("NO", [])
            with self.assertRaises(setup_wizard.SetupError):
                setup_wizard.imap_test("h", 993, "u", "p", "Fehlt")
            client.login.side_effect = setup_wizard.imaplib.IMAP4.error("auth")
            with self.assertRaisesRegex(setup_wizard.SetupError, "Benutzer und Passwort"):
                setup_wizard.imap_test("h", 993, "u", "p", "INBOX")
        with patch.object(setup_wizard.imaplib, "IMAP4_SSL", side_effect=ConnectionRefusedError()):
            with self.assertRaisesRegex(setup_wizard.SetupError, "nicht erreichbar"):
                setup_wizard.imap_test("h", 993, "u", "p", "INBOX")

    def test_ai_models(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [{"id": "zeta"}, {"id": "Alpha-vision"}]}
        with patch.object(setup_wizard.httpx, "get", return_value=response) as get:
            self.assertEqual(setup_wizard.ai_models("http://ki:1234/v1/", "key"), ["Alpha-vision", "zeta"])
        self.assertEqual(get.call_args.args[0], "http://ki:1234/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer key"})
        response.json.return_value = {"data": []}
        with patch.object(setup_wizard.httpx, "get", return_value=response):
            with self.assertRaises(setup_wizard.SetupError):
                setup_wizard.ai_models("http://ki:1234/v1", "")

    def test_sample_mail_mentions_a_date_next_week(self):
        subject, text = setup_wizard.sample_mail("de", date(2026, 11, 7))
        self.assertIn("14.11.2026", text)
        self.assertIn("Samstag", text)
        self.assertIn("class 4b", setup_wizard.sample_mail("en", date(2026, 11, 7))[1])


class UpdaterConfigureTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.project = Path(self.directory.name)
        (self.project / ".env").write_text("IMAP_HOST=\nIMAP_USERNAME=\n# bleibt\nKALENDER_VERSION=0.17.1\n")
        (self.project / "backups").mkdir()
        self.calls = []
        self.updater = updater.Updater(self.project, run=lambda command, **_k: self.calls.append(command[2:]) or Mock(returncode=0), sleep=lambda _s: None, fetch=lambda _u: "{}")

    def tearDown(self):
        self.directory.cleanup()

    def wait(self):
        import time
        for _ in range(200):
            if self.updater.state.get("state") != "running":
                return
            time.sleep(0.01)

    def test_configure_writes_allowed_keys_once(self):
        ok, _message = updater.configure(self.updater, {"IMAP_HOST": "imap.example.org", "IMAP_USERNAME": "a@b.de", "IMAP_PASSWORD": "it's $geheim", "APP_LANGUAGE": "en"})
        self.assertTrue(ok)
        self.wait()
        env = (self.project / ".env").read_text()
        self.assertIn("# bleibt\nKALENDER_VERSION=0.17.1\n", env)
        self.assertIn('IMAP_PASSWORD="it\'s $$geheim"', env)
        self.assertEqual(updater.read_env_value(self.project / ".env", "IMAP_PASSWORD"), "it's $geheim")
        self.assertEqual(self.calls, [["up", "-d", "kalender-assistent"]])
        self.assertEqual(self.updater.state["state"], "done")
        self.assertTrue(updater.configured(self.project / ".env"))
        ok, message = updater.configure(self.updater, {"IMAP_HOST": "boese.example", "IMAP_USERNAME": "x", "IMAP_PASSWORD": "y"})
        self.assertFalse(ok)
        self.assertIn("bereits abgeschlossen", message)
        self.assertIn("imap.example.org", (self.project / ".env").read_text())

    def test_configure_rejects_unknown_keys_and_bad_values(self):
        for values in ({"IMAP_HOST": "h", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p", "KALENDER_VERSION": "9.9.9"},
                       {"IMAP_HOST": "h", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p\nX=1"},
                       {"IMAP_HOST": "h"}, {}):
            self.assertFalse(updater.configure(self.updater, values)[0], values)
        self.assertEqual(self.calls, [])


class ServiceNameTests(unittest.TestCase):
    def test_service_name_from_url_and_branding(self):
        from src import i18n
        from src.config import service_name_for
        self.assertEqual(service_name_for(""), "iCloud")
        self.assertEqual(service_name_for("https://caldav.icloud.com"), "iCloud")
        self.assertEqual(service_name_for("https://caldav.fastmail.com/dav/"), "Fastmail")
        self.assertEqual(service_name_for("https://dav.mailbox.org/"), "mailbox.org")
        self.assertEqual(service_name_for("https://cloud.example.org/remote.php/dav"), "cloud.example.org")
        try:
            i18n.set_service_name("Nextcloud")
            self.assertEqual(i18n.gettext("iCloud nicht konfiguriert"), "Nextcloud nicht konfiguriert")
            self.assertIn("Nextcloud", i18n.js_catalog("de")["iCloud nicht konfiguriert"])
            token = i18n.activate("en")
            try:
                self.assertNotIn("iCloud", i18n.gettext("iCloud nicht konfiguriert"))
            finally:
                i18n.reset(token)
        finally:
            i18n.set_service_name("iCloud")
        self.assertEqual(i18n.js_catalog("de"), {})

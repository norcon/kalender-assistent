"""Übernahme alter .env-Werte in die Datenbank (Einstellungen, Zugangsschutz)."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from src import access, db, settings_store


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class EnvSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "test.sqlite3")
        db.initialize(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def absorb(self, environ, targets_set=False):
        return settings_store.absorb_env(self.path, environ, targets_set=targets_set)

    def test_values_move_into_the_database_once(self):
        environ = {"IMAP_POLL_SECONDS": "600", "AI_MODEL": "modell-a", "IMAP_FOLDER": "INBOX",
                   "APP_LANGUAGE": "en", "LOG_LEVEL": "INFO", "UPDATE_CHECK": "0",
                   "APP_DATABASE_PATH": "/data/calendar-assistant.sqlite3", "TARGET_CALENDARS": "Alex,Sam",
                   "DEMO_MODE": "0"}
        absorbed, removable = self.absorb(environ)
        stored = db.get_app_settings(self.path)
        self.assertEqual(stored["imap_poll_seconds"], "600")
        self.assertEqual(stored["ai_model"], "modell-a")
        self.assertEqual(stored["language"], "en")
        self.assertEqual(stored["update_check"], "0")
        self.assertNotIn("imap_folder", stored)  # Standardwert: nichts zu speichern
        self.assertNotIn("log_level", stored)
        self.assertEqual(sorted(absorbed), ["ai_model", "imap_poll_seconds", "language", "update_check"])
        # Alles darf aus .env verschwinden – außer TARGET_CALENDARS, solange keine Zielkalender gespeichert sind
        self.assertEqual(set(removable), set(environ) - {"TARGET_CALENDARS"})
        self.assertEqual(removable["AI_MODEL"], digest("modell-a"))
        self.assertIn("TARGET_CALENDARS", self.absorb(environ, targets_set=True)[1])
        settings = settings_store.apply_overrides(settings_store.Settings.from_env(environ={}), stored)
        self.assertIs(settings.update_check, False)

    def test_old_env_value_never_overrides_a_change_in_the_interface(self):
        db.save_app_settings(self.path, {"ai_model": "aus-der-oberflaeche"})
        self.absorb({"AI_MODEL": "alt"})
        self.assertEqual(db.get_app_settings(self.path)["ai_model"], "aus-der-oberflaeche")
        self.absorb({"AI_MODEL": "alt"})  # unverändert → bleibt
        self.assertEqual(db.get_app_settings(self.path)["ai_model"], "aus-der-oberflaeche")
        self.absorb({"AI_MODEL": "neu-in-env"})  # bewusst geändert → gilt
        self.assertEqual(db.get_app_settings(self.path)["ai_model"], "neu-in-env")

    def test_invalid_value_stays_in_env(self):
        with self.assertLogs("src.settings_store", "WARNING"):
            absorbed, removable = self.absorb({"CALDAV_TIMEZONE": "Mond/Krater"})
        self.assertEqual((absorbed, removable), ([], {}))


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "test.sqlite3")
        db.initialize(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_password_hash(self):
        stored = access.hash_password("geheim-123")
        self.assertNotIn("geheim", stored)
        self.assertTrue(access.verify_password("geheim-123", stored))
        self.assertFalse(access.verify_password("geheim-124", stored))
        self.assertFalse(access.verify_password("geheim-123", ""))
        self.assertNotEqual(stored, access.hash_password("geheim-123"))  # mit Salz
        with self.assertRaises(ValueError):
            access.set_password(self.path, "kurz")
        access.set_password(self.path, "geheim-123")
        self.assertTrue(access.verify_password("geheim-123", access.load(self.path)))
        access.set_password(self.path, None)
        self.assertEqual(access.load(self.path), "")

    def test_env_values_are_taken_over_once(self):
        removable = access.absorb_env(self.path, {"APP_PASSWORD": "alt-pw", "CONNECTIONS_EDIT_WITHOUT_PASSWORD": "1"})
        self.assertEqual(set(removable), {"APP_PASSWORD", "CONNECTIONS_EDIT_WITHOUT_PASSWORD"})
        self.assertEqual(removable["APP_PASSWORD"], digest("alt-pw"))
        self.assertTrue(access.verify_password("alt-pw", access.load(self.path)))  # kurze alte Passwörter bleiben gültig
        # Änderung in der Oberfläche überlebt den alten Wert in .env
        access.set_password(self.path, "neues-passwort")
        access.absorb_env(self.path, {"APP_PASSWORD": "alt-pw"})
        self.assertTrue(access.verify_password("neues-passwort", access.load(self.path)))
        self.assertEqual(access.absorb_env(self.path, {"APP_PASSWORD": ""}), {"APP_PASSWORD": digest("")})

    def test_command_line(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        access.set_password(self.path, "geheim-123")
        with patch.dict("os.environ", {"APP_DATABASE_PATH": self.path}), redirect_stdout(io.StringIO()) as out:
            access.main(["passwort-weg"])
        self.assertEqual(access.load(self.path), "")
        self.assertIn("Passwortschutz: aus", out.getvalue())


if __name__ == "__main__":
    unittest.main()

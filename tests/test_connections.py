import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import connections, db
from src.config import Settings


class ConnectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "db.sqlite3")
        db.initialize(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_absorb_encrypt_and_keep_ui_changes(self):
        env = {"IMAP_HOST": "imap.example.org", "IMAP_PASSWORD": " geheim $ ", "AI_MODEL": "bleibt", "NTFY_URL": ""}
        with self.assertLogs("src.connections", "INFO"):
            absorbed, removable = connections.absorb_env(self.path, env)
        self.assertEqual(sorted(absorbed), ["IMAP_HOST", "IMAP_PASSWORD"])
        self.assertEqual(sorted(removable), ["IMAP_HOST", "IMAP_PASSWORD"])
        self.assertEqual(removable["IMAP_PASSWORD"], connections.value_hash(" geheim $ "))
        raw = db.get_service_state(self.path)[connections.STATE_KEY]
        self.assertNotIn("geheim", raw)  # verschlüsselt
        self.assertEqual(connections.values(connections.load(self.path)), {"IMAP_HOST": "imap.example.org", "IMAP_PASSWORD": " geheim $ "})
        self.assertEqual(oct(os.stat(connections.key_path(self.path)).st_mode & 0o777), "0o600")
        # Änderung in der Oberfläche; unveränderter alter .env-Wert überschreibt sie nicht
        connections.update(self.path, {"IMAP_HOST": "imap.neu.example"})
        absorbed, removable = connections.absorb_env(self.path, env)
        self.assertEqual(absorbed, [])
        self.assertEqual(connections.values(connections.load(self.path))["IMAP_HOST"], "imap.neu.example")
        self.assertIn("IMAP_HOST", removable)  # darf trotzdem aus .env weg
        # Neuer Wert in .env wird übernommen
        with self.assertLogs("src.connections", "INFO"):
            absorbed, _removable = connections.absorb_env(self.path, {**env, "IMAP_HOST": "imap.anders.example"})
        self.assertEqual(absorbed, ["IMAP_HOST"])
        self.assertEqual(connections.values(connections.load(self.path))["IMAP_HOST"], "imap.anders.example")

    def test_database_copy_without_key_is_useless(self):
        with self.assertLogs("src.connections", "INFO"):
            connections.absorb_env(self.path, {"IMAP_PASSWORD": "geheim"})
        copy_dir = tempfile.mkdtemp()
        try:
            copy = os.path.join(copy_dir, "db.sqlite3")
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(self.path + suffix):
                    shutil.copy(self.path + suffix, copy + suffix)
            with self.assertRaises(connections.StoreError):
                connections.load(copy)
        finally:
            shutil.rmtree(copy_dir)

    def test_key_from_environment(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"APP_SECRET_KEY": key}):
            connections.update(self.path, {"NTFY_URL": "https://ntfy.sh/x"})
            self.assertEqual(connections.values(connections.load(self.path)), {"NTFY_URL": "https://ntfy.sh/x"})
            with self.assertRaises(ValueError):
                connections.update(self.path, {"APP_PASSWORD": "nein"})
        self.assertFalse(connections.key_path(self.path).exists())
        with self.assertRaises(connections.StoreError):  # ohne den Schlüssel nicht lesbar
            connections.load(self.path)

    def test_settings_take_connection_values_only_from_store(self):
        with patch.dict(os.environ, {"IMAP_HOST": "aus-env", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p", "IMAP2_HOST": "zwei",
                                     "IMAP2_USERNAME": "u2", "IMAP2_PASSWORD": "p2", "AI_MODEL": "modell-aus-env", "DEMO_MODE": "0"}):
            from_env = Settings.from_env()
            stored = Settings.from_env({"IMAP_HOST": "aus-db", "IMAP_USERNAME": "u", "IMAP_PASSWORD": "p"})
        self.assertEqual((from_env.imap_host, len(from_env.mailboxes())), ("aus-env", 2))
        self.assertEqual((stored.imap_host, len(stored.mailboxes())), ("aus-db", 1))  # IMAP2 nur aus der Ablage
        self.assertEqual(stored.ai_model, "modell-aus-env")  # keine Verbindungsdaten → weiter aus .env
        self.assertFalse(stored.setup_mode)
        self.assertTrue(Settings.from_env({}).setup_mode)


if __name__ == "__main__":
    unittest.main()

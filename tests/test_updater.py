import importlib.util
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "updater" / "updater.py"
_spec = importlib.util.spec_from_file_location("updater_module", _PATH)
updater = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(updater)


class FakeDocker:
    """Merkt sich compose-Aufrufe; die App „läuft“ mit der festgelegten Version."""

    def __init__(self, project: Path, *, fail_pull=False, broken=()):
        self.project = project
        self.calls = []
        self.fail_pull = fail_pull
        self.broken = set(broken)
        self.running = None

    def run(self, command, **_kwargs):
        self.calls.append(command[2:])
        if command[2] == "pull" and self.fail_pull:
            return subprocess.CompletedProcess(command, 1, "", "manifest unknown")
        if command[2] == "up":
            self.running = updater.pinned_version(self.project)
        if command[2] == "stop":
            self.running = None
        return subprocess.CompletedProcess(command, 0, "", "")

    def fetch(self, url):
        if self.running is None or self.running in self.broken:
            raise OSError("down")
        return json.dumps({"version": self.running}) if url.endswith("/api/version") else "ok"


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.project = Path(self.directory.name)
        (self.project / ".env").write_text("IMAP_HOST=mail\n# Kommentar\nKALENDER_VERSION=0.16.1\n")
        (self.project / "compose.yaml").write_text("services: {}\n")
        (self.project / "data").mkdir()
        (self.project / "backups").mkdir()
        (self.project / "data" / "calendar-assistant.sqlite3").write_text("neu")
        (self.project / "data" / "calendar-assistant.sqlite3-wal").write_text("wal")
        (self.project / "backups" / "calendar-assistant-2026-11-06_200000.sqlite3").write_text("alt")

    def tearDown(self):
        self.directory.cleanup()

    def make(self, docker):
        return updater.Updater(self.project, run=docker.run, sleep=lambda _s: None, fetch=docker.fetch, health_timeout=0.05)

    def wait(self, instance):
        # Erst fertig, wenn der Hintergrund-Thread auch Protokoll und Statusdatei
        # geschrieben und die Sperre freigegeben hat (sonst räumt tearDown zu früh auf).
        if not instance._lock.acquire(timeout=5):
            self.fail("Updater wurde nicht fertig")
        instance._lock.release()
        self.assertNotEqual(instance.state.get("state"), "running")
        return instance.state

    def test_env_editing_keeps_other_lines(self):
        env = self.project / ".env"
        updater.set_env_value(env, "KALENDER_VERSION", "0.17.0")
        self.assertEqual(env.read_text(), "IMAP_HOST=mail\n# Kommentar\nKALENDER_VERSION=0.17.0\n")
        self.assertEqual(updater.read_env_value(env, "KALENDER_VERSION"), "0.17.0")
        updater.set_env_value(env, "KALENDER_VERSION", None)
        self.assertNotIn("KALENDER_VERSION", env.read_text())
        self.assertIsNone(updater.read_env_value(env, "KALENDER_VERSION"))

    def test_successful_update(self):
        docker = FakeDocker(self.project)
        instance = self.make(docker)
        ok, _message = instance.start({"version": "0.17.0", "previous": "0.16.1", "backup": "calendar-assistant-2026-11-06_200000.sqlite3"})
        self.assertTrue(ok)
        state = self.wait(instance)
        self.assertEqual(state["state"], "done", state)
        self.assertEqual(updater.read_env_value(self.project / ".env", "KALENDER_VERSION"), "0.17.0")
        self.assertEqual(docker.calls, [["pull", "kalender-assistent"], ["up", "-d", "kalender-assistent"]])
        saved = list((self.project / "backups").glob("vor-update-*/.env"))
        self.assertEqual(len(saved), 1)
        self.assertIn("KALENDER_VERSION=0.16.1", saved[0].read_text())
        self.assertEqual((self.project / "data" / "calendar-assistant.sqlite3").read_text(), "neu")
        persisted = json.loads((self.project / "backups" / "update-status.json").read_text())
        self.assertEqual(persisted["state"], "done")

    def test_broken_version_rolls_back_version_and_database(self):
        docker = FakeDocker(self.project, broken={"0.17.0"})
        instance = self.make(docker)
        instance.start({"version": "0.17.0", "previous": "0.16.1", "backup": "calendar-assistant-2026-11-06_200000.sqlite3"})
        state = self.wait(instance)
        self.assertEqual(state["state"], "rolled_back", state)
        self.assertEqual(updater.read_env_value(self.project / ".env", "KALENDER_VERSION"), "0.16.1")
        self.assertEqual((self.project / "data" / "calendar-assistant.sqlite3").read_text(), "alt")
        self.assertFalse((self.project / "data" / "calendar-assistant.sqlite3-wal").exists())
        self.assertEqual(docker.running, "0.16.1")

    def test_failed_pull_leaves_running_app_alone(self):
        docker = FakeDocker(self.project, fail_pull=True)
        instance = self.make(docker)
        instance.start({"version": "0.17.0", "previous": "0.16.1", "backup": "calendar-assistant-2026-11-06_200000.sqlite3"})
        state = self.wait(instance)
        self.assertEqual(state["state"], "failed")
        self.assertIn("nichts geändert", state["message"])
        self.assertEqual(docker.calls, [["pull", "kalender-assistent"]])
        self.assertEqual(updater.read_env_value(self.project / ".env", "KALENDER_VERSION"), "0.16.1")
        self.assertEqual((self.project / "data" / "calendar-assistant.sqlite3").read_text(), "neu")

    def test_rollback_with_database_restore(self):
        docker = FakeDocker(self.project)
        instance = self.make(docker)
        instance.start({"version": "0.15.0", "previous": "0.16.1", "backup": "calendar-assistant-2026-11-06_200000.sqlite3",
                        "restore_backup": "calendar-assistant-2026-11-06_200000.sqlite3"})
        state = self.wait(instance)
        self.assertEqual(state["state"], "done", state)
        self.assertEqual(docker.calls[:2], [["pull", "kalender-assistent"], ["stop", "kalender-assistent"]])
        self.assertEqual((self.project / "data" / "calendar-assistant.sqlite3").read_text(), "alt")

    def test_rollback_before_0_28_refused_after_migration(self):
        env = self.project / ".env"
        env.write_text("KALENDER_VERSION=0.28.1\nKALENDER_VERBINDUNGEN=app\n")
        docker = FakeDocker(self.project)
        ok, message = self.make(docker).start({"version": "0.27.0", "previous": "0.28.1"})
        self.assertFalse(ok)
        self.assertIn("0.28.0", message)
        self.assertEqual(docker.calls, [])
        instance = self.make(docker)
        self.assertTrue(instance.start({"version": "0.28.0", "previous": "0.28.1"})[0])
        self.assertEqual(self.wait(instance)["state"], "done")

    def test_database_restore_keeps_current_connections(self):
        import sqlite3
        current = self.project / "data" / "calendar-assistant.sqlite3"
        old = self.project / "backups" / "calendar-assistant-2026-11-06_200000.sqlite3"
        current.unlink()
        old.unlink()
        (self.project / "data" / "calendar-assistant.sqlite3-wal").unlink()
        for path, rows in ((current, [("connections_v1", "neu-verschluesselt"), ("other", "neu")]),
                           (old, [("other", "alt")])):
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE service_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.executemany("INSERT INTO service_state VALUES (?, ?)", rows)
            connection.close()
        self.make(FakeDocker(self.project))._restore_database(old.name)
        with sqlite3.connect(current) as connection:
            state = dict(connection.execute("SELECT key, value FROM service_state"))
        connection.close()
        self.assertEqual(state, {"connections_v1": "neu-verschluesselt", "other": "alt"})

    def _store_connections(self):
        import sqlite3
        database = self.project / "data" / "calendar-assistant.sqlite3"
        database.unlink()
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE service_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO service_state VALUES ('connections_v1', 'x')")

    def test_env_cleanup_only_matching_allowed_keys(self):
        import hashlib
        env = self.project / ".env"
        env.write_text("IMAP_HOST=mail\nIMAP_PASSWORD='ge heim'\nAI_MODEL=m\nAPP_PASSWORD=\nKALENDER_VERSION=0.16.1\nNTFY_URL=https://ntfy.sh/a\n")
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        instance = self.make(FakeDocker(self.project))
        self.assertFalse(updater.cleanup_env(instance, {"KALENDER_VERSION": digest("0.16.1")})[0])  # nicht erlaubt
        self.assertFalse(updater.cleanup_env(instance, {"PATH": digest("x")})[0])
        ok, _message, removed = updater.cleanup_env(instance, {
            "IMAP_HOST": digest("mail"), "IMAP_PASSWORD": digest("ge heim"), "NTFY_URL": digest("https://ntfy.sh/anders"),
            "AI_MODEL": digest("m"), "APP_PASSWORD": digest(""),
        })
        self.assertTrue(ok)
        self.assertEqual(sorted(removed), ["AI_MODEL", "APP_PASSWORD", "IMAP_HOST", "IMAP_PASSWORD"])
        text = env.read_text()
        self.assertNotIn("IMAP_PASSWORD", text)
        self.assertNotIn("APP_PASSWORD", text)  # leere Zeile ebenfalls
        self.assertIn("NTFY_URL=https://ntfy.sh/a", text)  # Prüfsumme passte nicht → bleibt
        self.assertNotIn("KALENDER_VERBINDUNGEN", text)  # Markierung wird nicht mehr gebraucht
        backups = list((self.project / "backups").glob("env-vor-aufraeumen-*/.env"))
        self.assertEqual(len(backups), 1)
        self.assertIn("IMAP_PASSWORD", backups[0].read_text())
        self.assertEqual(updater.cleanup_env(instance, {"IMAP_HOST": digest("mail")})[1], "Nichts zu entfernen.")
        # Ersteinrichtung gesperrt, sobald die App Zugangsdaten gespeichert hat
        self.assertFalse(updater.configured(env, instance.data_file))
        self._store_connections()
        self.assertTrue(updater.configured(env, instance.data_file))
        self.assertTrue(instance.migrated())

    LEGACY_COMPOSE = (
        "services:\n  kalender-assistent:\n"
        "    image: ghcr.io/norcon/kalender-assistent:${KALENDER_VERSION:-latest}\n"
        "    env_file:\n      - .env\n"
        "  updater:\n    image: ghcr.io/norcon/kalender-assistent-updater:${KALENDER_UPDATER_VERSION:-latest}\n"
    )

    def test_version_is_pinned_in_compose_yaml(self):
        compose = self.project / "compose.yaml"
        compose.write_text(self.LEGACY_COMPOSE)
        self.assertEqual(updater.pinned_version(self.project), "0.16.1")  # .env hat noch Vorrang
        docker = FakeDocker(self.project)
        instance = self.make(docker)
        self.assertTrue(instance.start({"version": "0.17.0", "previous": "0.16.1"})[0])
        self.assertEqual(self.wait(instance)["state"], "done")
        self.assertIn("kalender-assistent:${KALENDER_VERSION:-0.17.0}\n", compose.read_text())
        self.assertIn("updater:${KALENDER_UPDATER_VERSION:-latest}", compose.read_text())  # Updater-Zeile bleibt
        self.assertNotIn("KALENDER_VERSION", (self.project / ".env").read_text())
        self.assertEqual(docker.running, "0.17.0")
        # kaputte Version → zurück auf die bisherige, ebenfalls in compose.yaml
        docker.broken.add("0.18.0")
        instance.start({"version": "0.18.0", "previous": "0.17.0"})
        self.assertEqual(self.wait(instance)["state"], "rolled_back")
        self.assertEqual(updater.pinned_version(self.project), "0.17.0")

    def test_tidy_env_removes_the_file_when_nothing_is_left(self):
        compose = self.project / "compose.yaml"
        compose.write_text(self.LEGACY_COMPOSE)
        env = self.project / ".env"
        env.write_text("# Kommentar\nKALENDER_VERSION=0.29.0\nKALENDER_VERBINDUNGEN=app\n")
        instance = self.make(FakeDocker(self.project))
        updater.tidy_env(instance)
        self.assertIn("KALENDER_VERBINDUNGEN=app", env.read_text())  # ohne gespeicherte Verbindungen bleibt sie
        self.assertIn("${KALENDER_VERSION:-0.29.0}", compose.read_text())
        self._store_connections()
        steps = updater.tidy_env(instance)
        self.assertFalse(env.exists())
        self.assertIn(".env gelöscht (nichts mehr darin)", steps)
        text = compose.read_text()
        self.assertIn("    env_file:\n      - path: .env\n        required: false\n", text)
        self.assertEqual(updater.pinned_version(self.project), "0.29.0")
        self.assertTrue(list((self.project / "backups").glob("env-vor-aufraeumen-*/.env")))
        self.assertEqual(updater.tidy_env(instance), [])  # ohne .env nichts zu tun
        # .env neu anlegen (z. B. Version in eigener compose-Datei) geht weiterhin
        updater.set_env_value(env, "KALENDER_VERSION", "0.30.0")
        self.assertEqual(oct(env.stat().st_mode & 0o777), "0o600")

    def test_rejects_bad_input_and_parallel_updates(self):
        instance = self.make(FakeDocker(self.project))
        for request in ({"version": "latest"}, {"version": "1.2.3; rm -rf /"}, {"version": "1.2.3", "backup": "../.env"},
                        {"version": "1.2.3", "previous": "x"}):
            self.assertFalse(instance.start(request)[0], request)
        instance._lock.acquire()
        self.assertEqual(instance.start({"version": "1.2.3"}), (False, "Es läuft bereits ein Update."))
        instance._lock.release()

    def test_interrupted_run_is_reported_after_restart(self):
        (self.project / "backups" / "update-status.json").write_text(json.dumps({"state": "running", "to": "0.17.0"}))
        state = self.make(FakeDocker(self.project)).state
        self.assertEqual(state["state"], "failed")

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
    """Merkt sich compose-Aufrufe; die App „läuft“ mit der Version aus .env."""

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
            self.running = updater.read_env_value(self.project / ".env", "KALENDER_VERSION")
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

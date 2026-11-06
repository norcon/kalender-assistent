import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import backup, db


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = str(Path(self.directory.name) / "data.sqlite3")
        self.backup_dir = str(Path(self.directory.name) / "backups")
        db.initialize(self.database_path)
        db.save_app_settings(self.database_path, {"backup_hour": 4})

    def test_backup_is_complete_copy_of_database(self):
        with self.assertLogs("src.backup", level="INFO"):
            target = backup.create_backup(self.database_path, self.backup_dir, 14)
        connection = sqlite3.connect(target)
        try:
            value = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'backup_hour'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(value, "4")
        self.assertFalse(list(Path(self.backup_dir).glob("*.tmp")))

    def test_old_backups_are_pruned_but_newest_kept(self):
        start = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
        with self.assertLogs("src.backup", level="INFO"):
            for day in range(20):
                backup.create_backup(
                    self.database_path, self.backup_dir, 7, now=start + timedelta(days=day)
                )
        names = [item["name"] for item in backup.list_backups(self.backup_dir)]
        self.assertEqual(len(names), 8)
        self.assertEqual(names[0], "calendar-assistant-2026-10-20_030000.sqlite3")

        with self.assertLogs("src.backup", level="INFO"):
            backup.create_backup(
                self.database_path, self.backup_dir, 1,
                now=start + timedelta(days=400),
            )
        self.assertEqual(len(backup.list_backups(self.backup_dir)), 1)

    def test_two_backups_in_same_second_do_not_overwrite_each_other(self):
        moment = datetime(2026, 11, 4, 20, 0, 0, tzinfo=timezone.utc)
        with self.assertLogs("src.backup", level="INFO"):
            first = backup.create_backup(self.database_path, self.backup_dir, 14, now=moment)
            second = backup.create_backup(self.database_path, self.backup_dir, 14, now=moment)
        self.assertNotEqual(first, second)
        names = [item["name"] for item in backup.list_backups(self.backup_dir)]
        self.assertEqual(names, [second.name, first.name])
        self.assertEqual(backup.backup_path(self.backup_dir, second.name), second)

    def test_parallel_backups_in_same_second_do_not_collide(self):
        import threading
        moment = datetime(2026, 11, 4, 20, 0, 0, tzinfo=timezone.utc)
        results, errors = [], []

        def run():
            try:
                results.append(backup.create_backup(self.database_path, self.backup_dir, 14, now=moment))
            except Exception as error:  # pragma: no cover - soll nicht passieren
                errors.append(error)

        with self.assertLogs("src.backup", level="INFO"):
            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({path.name for path in results}), 4)
        self.assertFalse(list(Path(self.backup_dir).glob("*.tmp")))

    def test_backup_due_once_per_local_day_after_configured_hour(self):
        berlin = ZoneInfo("Europe/Berlin")
        before = datetime(2026, 11, 5, 2, 59, tzinfo=berlin)
        after = datetime(2026, 11, 5, 3, 0, tzinfo=berlin)
        self.assertFalse(backup.backup_due(self.backup_dir, 3, before))
        self.assertTrue(backup.backup_due(self.backup_dir, 3, after))

        with self.assertLogs("src.backup", level="INFO"):
            backup.create_backup(self.database_path, self.backup_dir, 14, now=after)
        self.assertFalse(
            backup.backup_due(self.backup_dir, 3, after + timedelta(hours=20))
        )
        self.assertTrue(
            backup.backup_due(self.backup_dir, 3, after + timedelta(days=1))
        )


if __name__ == "__main__":
    unittest.main()

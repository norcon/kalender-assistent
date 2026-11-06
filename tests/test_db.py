import tempfile
import unittest
from pathlib import Path

from src import db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(
            Path(self.temporary_directory.name) / "calendar.sqlite3"
        )
        db.initialize(self.database_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_saves_and_dismisses_suggestion(self):
        db.reset_mailbox_state(self.database_path, "mailbox", "11")
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            3,
            "mailbox/11/3",
            [
                {
                    "title": "Training",
                    "start": "2026-11-04T17:00:00",
                    "end": None,
                    "location": None,
                    "person": "Kind 1",
                    "notes": None,
                    "confidence": 0.9,
                }
            ],
            {
                "subject": "Training",
                "sender": "verein@example.org",
                "received_at": "2026-11-03T18:00:00+01:00",
            },
        )

        suggestions = db.list_suggestions(self.database_path)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["title"], "Training")
        self.assertTrue(db.dismiss_suggestion(self.database_path, suggestions[0]["id"]))
        self.assertEqual(db.list_suggestions(self.database_path), [])
        self.assertEqual(
            len(db.list_suggestions(self.database_path, include_dismissed=True)), 1
        )

    def test_processed_message_is_idempotent(self):
        db.reset_mailbox_state(self.database_path, "mailbox", "11")
        source = {
            "subject": "Betreff",
            "sender": "sender@example.org",
            "received_at": "2026-11-03T18:00:00+01:00",
        }
        event = {
            "title": "Termin",
            "start": "2026-11-04",
            "end": None,
            "location": None,
            "person": None,
            "notes": None,
            "confidence": 0.8,
        }
        for _ in range(2):
            db.save_processed_message(
                self.database_path, "mailbox", "11", 4, "mailbox/11/4", [event], source
            )
        self.assertEqual(len(db.list_suggestions(self.database_path)), 1)

    def test_records_successful_icloud_export_only_once(self):
        db.reset_mailbox_state(self.database_path, "mailbox", "11")
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            5,
            "mailbox/11/5",
            [
                {
                    "title": "Familientermin",
                    "start": "2026-11-05T16:00:00",
                    "end": None,
                    "location": None,
                    "person": "Familie",
                    "notes": None,
                    "confidence": 0.9,
                }
            ],
            {
                "subject": "Familientermin",
                "sender": "family@example.org",
                "received_at": "2026-11-03T18:00:00+01:00",
            },
        )
        suggestion = db.list_suggestions(self.database_path)[0]

        self.assertTrue(
            db.mark_suggestion_exported(
                self.database_path,
                suggestion["id"],
                f"family-calendar-assistant-{suggestion['id']}@local.invalid",
                "Familie",
            )
        )
        self.assertFalse(
            db.mark_suggestion_exported(
                self.database_path,
                suggestion["id"],
                "different-uid@local.invalid",
                "Mia",
            )
        )
        exported = db.get_suggestion(self.database_path, suggestion["id"])
        self.assertEqual(exported["icloud_calendar"], "Familie")
        self.assertIsNotNone(exported["icloud_exported_at"])
        self.assertEqual(exported["dismissed"], 1)
        self.assertEqual(db.list_suggestions(self.database_path), [])
        self.assertEqual(
            db.list_suggestions(self.database_path, include_dismissed=True)[0]["id"],
            suggestion["id"],
        )

    def test_saves_person_correction_only_when_requested(self):
        db.reset_mailbox_state(self.database_path, "mailbox", "11")
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            6,
            "mailbox/11/6",
            [
                {
                    "title": "Mia Fußballtraining",
                    "start": "2026-11-06T16:00",
                    "end": None,
                    "location": None,
                    "person": "Alex",
                    "notes": None,
                    "confidence": 0.9,
                }
            ],
            {
                "subject": "Trainingsplan",
                "sender": "Fußballverein <verein@example.org>",
                "received_at": "2026-11-03T18:00:00+01:00",
            },
        )
        suggestion = db.list_suggestions(self.database_path)[0]
        unchanged_values = {
            "title": suggestion["title"],
            "start": suggestion["starts_at"],
            "end": suggestion["ends_at"],
            "location": suggestion["location"],
            "person": "Alex",
            "notes": suggestion["notes"],
        }
        db.update_suggestion(
            self.database_path, suggestion["id"], unchanged_values
        )
        self.assertEqual(
            db.list_person_corrections(
                self.database_path, "verein@example.org"
            ),
            [],
        )

        corrected_values = dict(unchanged_values)
        corrected_values["person"] = "Mia"
        corrected_values["previous_person"] = "Alex"
        self.assertTrue(
            db.update_suggestion(
                self.database_path,
                suggestion["id"],
                corrected_values,
                learn_person_correction=True,
            )
        )
        corrections = db.list_person_corrections(
            self.database_path, "Verein <VEREIN@example.org>"
        )
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["event_title"], "Mia Fußballtraining")
        self.assertEqual(corrections[0]["corrected_person"], "Mia")

    def test_links_threaded_change_requests_without_creating_new_suggestions(self):
        source = {
            "subject": "Training",
            "sender": "verein@example.org",
            "received_at": "2026-11-03T18:00:00+01:00",
            "message_id": "<original@example.org>",
            "references": "",
        }
        event = {
            "title": "Mia Fußballtraining",
            "start": "2026-11-06T16:00",
            "end": "2026-11-06T17:00",
            "location": "Sporthalle",
            "person": "Mia",
            "notes": None,
            "confidence": 0.9,
        }
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            7,
            "mailbox/11/7",
            [event],
            source,
        )
        original = db.list_suggestions(self.database_path)[0]
        update_source = {
            **source,
            "subject": "Training verschoben",
            "message_id": "<update@example.org>",
            "references": "<original@example.org>",
        }
        updated_event = {
            **event,
            "start": "2026-11-06T17:00",
            "action": "update",
        }
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            8,
            "mailbox/11/8",
            [updated_event],
            update_source,
        )

        requests = db.list_change_requests(self.database_path)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["action"], "update")
        self.assertEqual(requests[0]["related_suggestion_id"], original["id"])
        self.assertEqual(len(db.list_suggestions(self.database_path)), 1)

    def test_unmatched_change_request_can_be_manually_linked(self):
        source = {
            "subject": "Training",
            "sender": "verein@example.org",
            "received_at": "2026-11-03T18:00:00+01:00",
            "message_id": "<original-unlinked@example.org>",
            "references": "",
        }
        event = {
            "title": "Training",
            "start": "2026-11-06T16:00",
            "end": None,
            "location": None,
            "person": "Mia",
            "notes": None,
            "confidence": 0.9,
        }
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            11,
            "mailbox/11/11",
            [event],
            source,
        )
        target = db.list_suggestions(self.database_path)[0]
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            12,
            "mailbox/11/12",
            [{**event, "action": "cancel"}],
            {
                **source,
                "subject": "Training abgesagt",
                "message_id": "<unlinked-cancel@example.org>",
            },
        )
        request = db.list_change_requests(self.database_path)[0]
        self.assertIsNone(request["related_suggestion_id"])

        self.assertTrue(
            db.link_change_request(
                self.database_path, request["id"], target["id"]
            )
        )
        self.assertEqual(
            db.get_change_request(self.database_path, request["id"])[
                "related_suggestion_id"
            ],
            target["id"],
        )

    def test_marks_similar_active_events_as_possible_duplicates(self):
        source = {
            "subject": "Turnier",
            "sender": "verein@example.org",
            "received_at": "2026-11-03T18:00:00+01:00",
        }
        event = {
            "title": "Mia Fußballturnier",
            "start": "2026-11-07T10:00",
            "end": None,
            "location": "Sportplatz",
            "person": "Mia",
            "notes": None,
            "confidence": 0.9,
        }
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            9,
            "mailbox/11/9",
            [event],
            source,
        )
        first = db.list_suggestions(self.database_path)[0]
        self.assertTrue(db.dismiss_suggestion(self.database_path, first["id"]))
        db.save_processed_message(
            self.database_path,
            "mailbox",
            "11",
            10,
            "mailbox/11/10",
            [{**event, "title": "Fußballturnier Mia"}],
            source,
        )

        suggestions = db.list_suggestions(self.database_path)
        duplicate = next(item for item in suggestions if item["id"] != first["id"])
        self.assertEqual(duplicate["possible_duplicate_of"], first["id"])
        self.assertEqual(duplicate["duplicate_title"], first["title"])


if __name__ == "__main__":
    unittest.main()


class IgnoredSenderTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temporary_directory.name) / "calendar.sqlite3")
        db.initialize(self.database_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _save(self, uid, sender, title, hidden_reason=None):
        event = {"title": title, "start": "2099-01-01T10:00", "confidence": 1.0}
        if hidden_reason:
            event["hidden_reason"] = hidden_reason
        db.save_processed_message(
            self.database_path, "mailbox", "1", uid, f"key-{uid}", [event],
            {"subject": f"Betreff {title}", "sender": sender, "received_at": "2026-11-06T10:00:00+01:00"},
        )
        return next(i for i in db.list_suggestions(self.database_path) if i["title"] == title)

    def test_address_domain_and_matching(self):
        sender = "Hermes <noreply@paketankuendigung.myhermes.de>"
        self.assertEqual(db.sender_address(sender), "noreply@paketankuendigung.myhermes.de")
        self.assertEqual(db.sender_domain(sender), "myhermes.de")
        self.assertEqual(db.sender_domain("a@mail.shop.co.uk"), "shop.co.uk")
        self.assertTrue(db.sender_matches(sender, "myhermes.de"))
        self.assertFalse(db.sender_matches("x@notmyhermes.de", "myhermes.de"))
        self.assertTrue(db.sender_matches("Versand <VERSAND@amazon.de>", "versand@amazon.de"))
        self.assertFalse(db.sender_matches("info@amazon.de", "versand@amazon.de"))
        self.assertFalse(db.sender_matches("", "amazon.de"))
        self.assertEqual(db.normalize_sender_pattern(" @Amazon.DE "), "amazon.de")
        self.assertEqual(db.normalize_sender_pattern("Shop <Info@Shop.de>"), "info@shop.de")
        for bad in ("", "amazon", "zwei worte.de"):
            with self.assertRaises(ValueError):
                db.normalize_sender_pattern(bad)

    def test_rule_hides_open_suggestions_and_removal_shows_them(self):
        parcel = self._save(1, "versand@amazon.de", "Paket kommt")
        school = self._save(2, "schule@example.org", "Elternabend")
        pattern, created, hidden = db.add_ignored_sender(self.database_path, "amazon.de", "versand@amazon.de", "Versendet")
        self.assertEqual((pattern, created, hidden), ("amazon.de", True, [parcel["id"]]))
        self.assertFalse(db.add_ignored_sender(self.database_path, "amazon.de")[1])
        self.assertEqual(db.matching_ignore_rule(self.database_path, "x@de.amazon.de"), "amazon.de")
        self.assertIsNone(db.matching_ignore_rule(self.database_path, "schule@example.org"))
        stored = db.get_suggestion(self.database_path, parcel["id"])
        self.assertEqual((stored["hidden_reason"], stored["dismissed"]), ("amazon.de", 0))
        self.assertIsNone(db.get_suggestion(self.database_path, school["id"])["hidden_reason"])
        self.assertIn("hidden", [e["action"] for e in db.list_suggestion_history(self.database_path, parcel["id"])])

        removed, shown = db.remove_ignored_sender(self.database_path, "amazon.de")
        self.assertEqual((removed, shown), (True, [parcel["id"]]))
        self.assertIsNone(db.get_suggestion(self.database_path, parcel["id"])["hidden_reason"])
        self.assertEqual(db.list_ignored_senders(self.database_path), [])

    def test_saved_hidden_suggestion_can_be_unhidden(self):
        item = self._save(3, "info@shop.de", "Rabatt", hidden_reason="shop.de")
        self.assertEqual(item["hidden_reason"], "shop.de")
        self.assertTrue(db.unhide_suggestion(self.database_path, item["id"]))
        self.assertFalse(db.unhide_suggestion(self.database_path, item["id"]))
        self.assertIsNone(db.get_suggestion(self.database_path, item["id"])["hidden_reason"])


class SenderTrustTests(IgnoredSenderTests):
    def test_trusted_rules_stats_and_newsletter_unhide(self):
        news = self._save(20, "News <news@verein-test.de>", "Sommerfest", hidden_reason=db.NEWSLETTER_REASON)
        self.assertFalse(db.sender_is_trusted(self.database_path, "news@verein-test.de"))
        pattern, created, shown = db.add_trusted_sender(self.database_path, "verein-test.de")
        self.assertEqual((pattern, created, shown), ("verein-test.de", True, [news["id"]]))
        self.assertTrue(db.sender_is_trusted(self.database_path, "x@verein-test.de"))
        self.assertIsNone(db.matching_ignore_rule(self.database_path, "x@verein-test.de"))
        self.assertEqual([r["pattern"] for r in db.list_ignored_senders(self.database_path, "show")], ["verein-test.de"])
        # Ausblenden überschreibt „immer anzeigen“, umgekehrt nicht
        self.assertTrue(db.add_ignored_sender(self.database_path, "verein-test.de")[1])
        self.assertEqual(db.list_ignored_senders(self.database_path, "show"), [])
        with self.assertRaises(ValueError):
            db.add_trusted_sender(self.database_path, "verein-test.de")

    def test_export_makes_sender_trusted_and_stats_count_manual_removals(self):
        exported = self._save(30, "bahn@deutschebahn.com", "Fahrt")
        db.mark_suggestion_exported(self.database_path, exported["id"], "uid", "Familie")
        self.assertTrue(db.sender_is_trusted(self.database_path, "news@bahn.deutschebahn.com"))
        for uid in (31, 32):
            item = self._save(uid, "versand@shop-test.de", f"Paket {uid}")
            db.dismiss_suggestion(self.database_path, item["id"])
        auto = self._save(33, "versand@shop-test.de", "Paket alt")
        db.auto_archive_suggestions(self.database_path, [auto["id"]])
        stats = db.sender_stats(self.database_path)
        self.assertEqual(stats["shop-test.de"], {"total": 3, "exported": 0, "removed": 2})
        self.assertEqual(stats["deutschebahn.com"], {"total": 1, "exported": 1, "removed": 0})


class ReanalysisTests(IgnoredSenderTests):
    def test_adds_only_new_findings_and_clears_failure(self):
        key = "u@imap.example/INBOX/1/40"
        db.save_processed_message(
            self.database_path, "u@imap.example/INBOX", "1", 40, key,
            [{"title": "Elternabend", "start": "2099-01-10T19:30", "confidence": 0.9}],
            {"subject": "Termine", "sender": "schule@example.org", "received_at": "2026-11-06T10:00:00+01:00"},
        )
        db.record_message_failure(self.database_path, key, "Timeout", {"subject": "Termine"})
        source = {"subject": "Termine", "sender": "schule@example.org", "received_at": "2026-11-06T10:00:00+01:00"}
        result = db.add_reanalysis_events(self.database_path, key, [
            {"title": "Elternabend 4b", "start": "2099-01-10T19:30", "confidence": 1.0},
            {"title": "Elternabend", "start": "2099-01-10", "confidence": 1.0},
            {"title": "Schulfest", "start": "2099-02-01", "confidence": 0.9},
            {"title": "Elternabend", "start": "2099-01-10T19:30", "confidence": 1.0, "action": "cancel"},
        ], source)
        self.assertEqual((len(result["added"]), result["known"], result["changes"]), (1, 2, 1))
        new = db.get_suggestion(self.database_path, result["added"][0])
        self.assertEqual((new["title"], new["event_index"]), ("Schulfest", 1))
        self.assertEqual(db.list_suggestion_history(self.database_path, new["id"])[0]["actor"], db.REANALYSIS_ACTOR)
        self.assertEqual(db.list_skipped_messages(self.database_path), [])
        self.assertTrue(db.source_known(self.database_path, key))
        self.assertFalse(db.source_known(self.database_path, "u@imap.example/INBOX/1/99"))
        again = db.add_reanalysis_events(self.database_path, key, [{"title": "Schulfest", "start": "2099-02-01", "confidence": 0.9}], source)
        self.assertEqual((again["added"], again["known"]), ([], 1))


class MaintenanceDbTests(IgnoredSenderTests):
    def test_ai_runs_inventory_and_reminder(self):
        db.record_ai_run(self.database_path, 12.34, True, "modell")
        db.record_ai_run(self.database_path, 3, False, "modell")
        rows = db.maintenance_rows(self.database_path, "2000-01-01")
        self.assertEqual([(r["seconds"], r["ok"]) for r in rows["ai_runs"]], [(12.3, 1), (3.0, 0)])
        item = self._save(50, "a@b.de", "Termin")
        self.assertTrue(db.set_suggestion_reminder(self.database_path, item["id"], "15"))
        self.assertEqual(db.get_suggestion(self.database_path, item["id"])["reminder"], "15")
        inventory = db.inventory(self.database_path)
        self.assertEqual((inventory["open"], inventory["mails"], inventory["total"]), (1, 1, 1))
        self.assertEqual(len(rows["mails"]), 0)  # vor dem Speichern abgefragt


class TargetCalendarTests(IgnoredSenderTests):
    def test_seed_from_existing_data_and_save(self):
        self.assertEqual(db.seed_target_calendars(self.database_path, []), [])
        db.save_processed_message(
            self.database_path, "m", "1", 60, "m/1/60",
            [{"title": "A", "start": "2099-01-01", "confidence": 1, "person": "Mia"},
             {"title": "B", "start": "2099-01-02", "confidence": 1, "person": "Familie"},
             {"title": "C", "start": "2099-01-03", "confidence": 1, "person": "Alex"}],
            {"subject": "s", "sender": "x@y.de", "received_at": "2026-11-06T10:00:00+01:00"},
        )
        adopted = db.seed_target_calendars(self.database_path, ["Mia", "Alex", "Familie", "Privat"])
        self.assertEqual(adopted, ["Mia", "Alex", "Familie"])
        targets = db.list_target_calendars(self.database_path)
        self.assertEqual([(t["name"], t["shared"]) for t in targets], [("Mia", False), ("Alex", False), ("Familie", True)])
        self.assertEqual(db.seed_target_calendars(self.database_path, []), [])  # nur einmal
        db.save_target_calendars(self.database_path, [{"name": " Ben ", "description": "Sohn", "shared": False}, {"name": ""}])
        self.assertEqual(db.list_target_calendars(self.database_path), [{"name": "Ben", "description": "Sohn", "shared": False}])

    def test_preset_from_env_has_priority(self):
        self.assertEqual(db.seed_target_calendars(self.database_path, ["X"], ["Alex", " Sam", "Alex", "Familie"]), ["Alex", "Sam", "Familie"])
        self.assertEqual([(t["name"], t["shared"]) for t in db.list_target_calendars(self.database_path)],
                         [("Alex", False), ("Sam", False), ("Familie", True)])
        self.assertEqual(db.seed_target_calendars(self.database_path, [], ["Neu"]), [])

    def test_legacy_name_check_is_removed(self):
        import sqlite3
        legacy = str(Path(self.temporary_directory.name) / "legacy.sqlite3")
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE person_corrections (suggestion_id INTEGER PRIMARY KEY, source_sender TEXT NOT NULL, "
            "source_subject TEXT NOT NULL, event_title TEXT NOT NULL, previous_person TEXT, "
            "corrected_person TEXT NOT NULL CHECK(corrected_person IN ('Alt1', 'Alt2')), created_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO person_corrections VALUES (1, 's@x.de', 'Betreff', 'Titel', NULL, 'Alt1', '2026-01-01')")
        connection.commit()
        connection.close()
        db.initialize(legacy)
        with db.connect(legacy) as check:
            sql = check.execute("SELECT sql FROM sqlite_master WHERE name = 'person_corrections'").fetchone()[0]
            self.assertNotIn("CHECK", sql)
            row = check.execute("SELECT corrected_person, sender_key FROM person_corrections").fetchone()
            self.assertEqual(row["corrected_person"], "Alt1")
            check.execute("INSERT INTO person_corrections (suggestion_id, source_sender, sender_key, source_subject, event_title, corrected_person, created_at) VALUES (2, 'a', 'a', 'b', 'c', 'Neu', 'x')")

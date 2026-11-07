import asyncio
import os
import re
from pathlib import Path
import tempfile
import time
import unittest
import unittest.mock
from urllib.parse import unquote
from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("IMAP_HOST", "127.0.0.1")
os.environ.setdefault("IMAP_USERNAME", "test@example.org")
os.environ.setdefault("IMAP_PASSWORD", "test-password")
os.environ.setdefault("AI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("AI_MODEL", "test-model")
_test_directory = tempfile.TemporaryDirectory()
os.environ["APP_DATABASE_PATH"] = os.path.join(
    _test_directory.name, "calendar-assistant.sqlite3"
)
os.environ["APP_BACKUP_DIR"] = os.path.join(_test_directory.name, "backups")

from fastapi.testclient import TestClient

from src import app as app_module, db


async def _wait_forever():
    # Ersetzt Hintergrundprozesse in Tests. Muss weiterlaufen, sonst wertet
    # der Wächter den Prozess als ausgefallen.
    await asyncio.Event().wait()


# Archivierung würde Testtermine mit heutigem Datum je nach Timing archivieren,
# die KI-Prüfung und IMAP IDLE würden echte Netzwerkverbindungen aufbauen,
# der Watchdog-Thread könnte den Testprozess beenden. Alles wird gezielt
# direkt getestet.
app_module._archive_worker = _wait_forever
app_module._ai_check_worker = _wait_forever
app_module._calendar_backup_worker = _wait_forever  # Nachauswertung würde mit Tests um die Datei konkurrieren
app_module._start_idle_watcher = lambda loop: None
app_module._start_watchdog = lambda: None

db.initialize(app_module.settings.database_path)
db.save_target_calendars(
    app_module.settings.database_path,
    [{"name": n, "shared": n == "Familie"} for n in ("Alex", "Sam", "Mia", "Ben", "Familie")],
)


class ActivityApiTests(unittest.TestCase):
    def test_caldav_discovery_requires_credentials_and_hides_calendar_urls(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                missing_config = replace(
                    app_module.settings,
                    caldav_username="",
                    caldav_app_password="",
                )
                with patch.object(app_module, "settings", missing_config):
                    response = client.post("/api/caldav/discover")
                self.assertEqual(response.status_code, 503)

                configured = replace(
                    app_module.settings,
                    caldav_username="tablet@example.org",
                    caldav_app_password="app-password",
                )
                with patch.object(app_module, "settings", configured):
                    with patch.object(
                        app_module.caldav,
                        "discover_icloud_calendars",
                        return_value=[
                            {
                                "name": "Familie",
                                "write_access": True,
                                "calendar_url": "https://example.org/private-calendar/",
                            }
                        ],
                    ):
                        response = client.post("/api/caldav/discover")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(),
                    {"calendars": [{"name": "Familie", "write_access": True, "target": True}]},
                )
                self.assertNotIn("private-calendar", response.text)

    def test_manual_check_and_live_activity(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    state = client.get("/api/activity").json()["status"]
                    if state["status"] == "success":
                        break
                    time.sleep(0.01)

                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Jetzt prüfen", page.text)
                self.assertIn('href="/protokoll"', page.text)
                self.assertNotIn("activity-log", page.text)
                protocol = client.get("/protokoll")
                self.assertEqual(protocol.status_code, 200)
                self.assertIn('id="activity-log"', protocol.text)
                self.assertIn('aria-current="page">Protokoll', protocol.text)

                response = client.post("/api/check")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "success")

                activity = client.get("/api/activity").json()
                self.assertEqual(activity["status"]["status"], "success")
                self.assertTrue(
                    any("manuell" in entry["message"] for entry in activity["logs"])
                )

    def test_suggestion_can_be_edited_and_validated(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                db.save_processed_message(
                    app_module.settings.database_path,
                    "edit-test-mailbox",
                    "edit-test-validity",
                    int(time.time()),
                    f"edit-test-{time.time_ns()}",
                    [
                        {
                            "title": "Original",
                            "start": "2026-11-04T17:00:00",
                            "end": None,
                            "location": None,
                            "person": "Falsch zugeordnet",
                            "notes": None,
                            "confidence": 0.9,
                        }
                    ],
                    {
                        "subject": "Test",
                        "sender": "test@example.org",
                        "received_at": "2026-11-03T18:00:00+01:00",
                    },
                )
                suggestion = next(
                    item
                    for item in db.list_suggestions(app_module.settings.database_path)
                    if item["title"] == "Original"
                )
                page = client.get("/")
                self.assertIn('data-field="start-date"', page.text)
                self.assertIn('data-field="start-time"', page.text)
                self.assertNotIn(">Bearbeiten</button>", page.text)
                self.assertIn('data-field="person"', page.text)
                self.assertIn("Alex", page.text)
                self.assertIn("Sam", page.text)
                self.assertIn("Mia", page.text)
                self.assertIn("Ben", page.text)
                self.assertIn("Familie", page.text)
                self.assertNotIn('name="person"', page.text)
                before = db.get_suggestion_version(app_module.settings.database_path)

                response = client.put(
                    f"/api/suggestions/{suggestion['id']}",
                    json={
                        "title": "Korrigierter Termin",
                        "start": "2026-11-04T18:00",
                        "end": "2026-11-04T19:00",
                        "location": "Sporthalle",
                        "person": "Mia",
                        "notes": "Eingang hinten",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertGreater(response.json()["suggestion_version"], before)
                updated = next(
                    item
                    for item in db.list_suggestions(app_module.settings.database_path)
                    if item["id"] == suggestion["id"]
                )
                self.assertEqual(updated["title"], "Korrigierter Termin")
                self.assertEqual(updated["person"], "Mia")
                self.assertEqual(updated["location"], "Sporthalle")
                self.assertEqual(updated["notes"], "Eingang hinten")
                learned = db.list_person_corrections(
                    app_module.settings.database_path, "test@example.org"
                )
                self.assertEqual(len(learned), 1)
                self.assertEqual(learned[0]["corrected_person"], "Mia")

                before_noop_version = db.get_suggestion_version(
                    app_module.settings.database_path
                )
                before_noop_activity = client.get("/api/activity").json()
                unchanged = client.put(
                    f"/api/suggestions/{suggestion['id']}",
                    json={
                        "title": updated["title"],
                        "start": "2026-11-04T18:00:00",
                        "end": "2026-11-04T19:00:00",
                        "location": updated["location"],
                        "person": updated["person"],
                        "notes": updated["notes"],
                    },
                )
                self.assertEqual(unchanged.status_code, 200)
                self.assertFalse(unchanged.json()["changed"])
                self.assertEqual(
                    unchanged.json()["suggestion_version"], before_noop_version
                )
                activity_after_noop = client.get(
                    f"/api/activity?after={max((entry['id'] for entry in before_noop_activity['logs']), default=0)}"
                ).json()
                self.assertFalse(
                    any(
                        f"Terminvorschlag {suggestion['id']} wurde bearbeitet."
                        in entry["message"]
                        for entry in activity_after_noop["logs"]
                    )
                )

                changed_again = client.put(
                    f"/api/suggestions/{suggestion['id']}",
                    json={
                        "title": "Nochmals korrigierter Termin",
                        "start": "2026-11-04T18:00",
                        "end": "2026-11-04T19:00",
                        "location": updated["location"],
                        "person": updated["person"],
                        "notes": updated["notes"],
                    },
                )
                self.assertTrue(changed_again.json()["changed"])

                invalid_person = client.put(
                    f"/api/suggestions/{suggestion['id']}",
                    json={
                        "title": updated["title"],
                        "start": updated["starts_at"],
                        "end": updated["ends_at"],
                        "location": updated["location"],
                        "person": "Unbekannt",
                        "notes": updated["notes"],
                    },
                )
                self.assertEqual(invalid_person.status_code, 422)

                invalid = client.put(
                    f"/api/suggestions/{suggestion['id']}",
                    json={
                        "title": "Ungültiger Termin",
                        "start": "2026-11-04T18:00",
                        "end": "2026-11-04T17:00",
                    },
                )
                self.assertEqual(invalid.status_code, 422)
                self.assertIn("Änderungsverlauf", client.get("/").text)
                correction_page = client.get("/korrekturen")
                self.assertIn("Korrigierter Termin", correction_page.text)
                deleted_correction = client.post(
                    f"/person-corrections/{suggestion['id']}/delete",
                    follow_redirects=False,
                )
                self.assertEqual(deleted_correction.status_code, 303)
                self.assertEqual(
                    db.list_person_corrections(
                        app_module.settings.database_path, "test@example.org"
                    ),
                    [],
                )

    def test_export_requires_manual_click_and_is_idempotent(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                db.save_processed_message(
                    app_module.settings.database_path,
                    "export-test-mailbox",
                    "export-test-validity",
                    int(time.time()),
                    f"export-test-{time.time_ns()}",
                    [
                        {
                            "title": "Familientermin",
                            "start": "2026-11-04T17:00:00",
                            "end": "2026-11-04T18:00:00",
                            "location": "Sporthalle",
                            "person": "Familie",
                            "notes": "Eingang hinten",
                            "confidence": 0.95,
                        }
                    ],
                    {
                        "subject": "Familientermin",
                        "sender": "test@example.org",
                        "received_at": "2026-11-03T18:00:00+01:00",
                    },
                )
                suggestion = next(
                    item
                    for item in db.list_suggestions(app_module.settings.database_path)
                    if item["title"] == "Familientermin"
                )
                configured = replace(
                    app_module.settings,
                    caldav_username="tablet@example.org",
                    caldav_app_password="app-password",
                )
                with patch.object(app_module, "settings", configured):
                    with patch.object(
                        app_module.caldav,
                        "create_icloud_event",
                        return_value="created",
                    ) as create_event:
                        response = client.post(
                            f"/api/suggestions/{suggestion['id']}/export"
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["status"], "created")
                        self.assertEqual(response.json()["calendar"], "Familie")
                        create_event.assert_called_once()
                        self.assertEqual(create_event.call_args.args[5], "Familie")

                        duplicate = client.post(
                            f"/api/suggestions/{suggestion['id']}/export"
                        )
                        self.assertEqual(duplicate.status_code, 200)
                        self.assertEqual(
                            duplicate.json()["status"], "already_exported"
                        )
                        create_event.assert_called_once()

                    archive = client.get("/archiv")
                    self.assertIn("Nach iCloud-Kalender", archive.text)
                    self.assertIn("Familie", archive.text)
                    self.assertNotIn(
                        "Familientermin",
                        client.get("/suggestions-fragment").text,
                    )
                    self.assertTrue(
                        db.restore_suggestion(
                            app_module.settings.database_path,
                            suggestion["id"],
                        )
                    )

                    with patch.object(
                        app_module.caldav,
                        "resend_icloud_event",
                        return_value="updated",
                    ) as resend_event:
                        resent = client.post(
                            f"/api/suggestions/{suggestion['id']}/resend"
                        )
                        self.assertEqual(resent.status_code, 200)
                        self.assertEqual(resent.json()["status"], "updated")
                        self.assertEqual(resent.json()["calendar"], "Familie")
                        resend_event.assert_called_once()
                        self.assertEqual(resend_event.call_args.args[5], "Familie")

                    with patch.object(
                        app_module.caldav,
                        "resend_icloud_event",
                    ) as resend_event:
                        not_exported = client.post(
                            f"/api/suggestions/999999/resend"
                        )
                        self.assertEqual(not_exported.status_code, 404)
                        resend_event.assert_not_called()

                    with patch.object(app_module, "settings", configured):
                        self.assertTrue(
                            db.restore_suggestion(
                                app_module.settings.database_path,
                                suggestion["id"],
                            )
                        )
                        edited = client.put(
                            f"/api/suggestions/{suggestion['id']}",
                            json={
                                "title": "Geändert",
                                "start": "2026-11-04T17:00",
                                "person": "Familie",
                            },
                        )
                        self.assertEqual(edited.status_code, 200)

                exported = db.get_suggestion(
                    app_module.settings.database_path, suggestion["id"]
                )
                self.assertEqual(exported["icloud_calendar"], "Familie")
                self.assertIsNotNone(exported["icloud_exported_at"])
                self.assertEqual(exported["title"], "Geändert")

    def test_confirmed_email_update_updates_existing_icloud_event(self):
        source_key = f"review-update-{time.time_ns()}"
        original_source = {
            "subject": "Training",
            "sender": "verein@example.org",
            "received_at": "2026-11-03T18:00:00+01:00",
            "message_id": f"<{source_key}@example.org>",
            "references": "",
        }
        original_event = {
            "title": "Training zur Probe",
            "start": "2026-11-06T16:00",
            "end": "2026-11-06T17:00",
            "location": "Sporthalle",
            "person": "Mia",
            "notes": None,
            "confidence": 0.9,
        }
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                db.save_processed_message(
                    app_module.settings.database_path,
                    "review-mailbox",
                    "review-validity",
                    int(time.time()),
                    source_key,
                    [original_event],
                    original_source,
                )
                target = next(
                    item
                    for item in db.list_suggestions(
                        app_module.settings.database_path, include_dismissed=True
                    )
                    if item["source_key"] == source_key
                )
                self.assertTrue(
                    db.mark_suggestion_exported(
                        app_module.settings.database_path,
                        target["id"],
                        f"family-calendar-assistant-{target['id']}@local.invalid",
                        "Mia",
                    )
                )
                db.save_processed_message(
                    app_module.settings.database_path,
                    "review-mailbox",
                    "review-validity",
                    int(time.time()) + 1,
                    f"{source_key}-reply",
                    [
                        {
                            **original_event,
                            "start": "2026-11-06T17:00",
                            "end": "2026-11-06T18:00",
                            "action": "update",
                        }
                    ],
                    {
                        **original_source,
                        "subject": "Training verschoben",
                        "message_id": f"<{source_key}-reply@example.org>",
                        "references": original_source["message_id"],
                    },
                )
                change_request = next(
                    item
                    for item in db.list_change_requests(app_module.settings.database_path)
                    if item["source_key"] == f"{source_key}-reply"
                )
                configured = replace(
                    app_module.settings,
                    caldav_username="tablet@example.org",
                    caldav_app_password="app-password",
                )
                with patch.object(app_module, "settings", configured):
                    with patch.object(
                        app_module.caldav,
                        "resend_icloud_event",
                        return_value="updated",
                    ) as resend:
                        response = client.post(
                            f"/api/change-requests/{change_request['id']}/apply"
                        )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    db.get_suggestion(
                        app_module.settings.database_path, target["id"]
                    )["starts_at"],
                    "2026-11-06T17:00",
                )
                self.assertEqual(
                    resend.call_args.args[6]["title"], "Training zur Probe"
                )
                self.assertEqual(
                    db.get_change_request(
                        app_module.settings.database_path, change_request["id"]
                    )["status"],
                    "applied",
                )

    def test_confirmed_email_cancellation_deletes_exported_event(self):
        source_key = f"review-cancel-{time.time_ns()}"
        source = {
            "subject": "Elternabend",
            "sender": "schule@example.org",
            "received_at": "2026-11-03T18:00:00+01:00",
            "message_id": f"<{source_key}@example.org>",
            "references": "",
        }
        event = {
            "title": "Elternabend",
            "start": "2026-11-08T19:00",
            "end": "2026-11-08T20:00",
            "location": "Schule",
            "person": "Familie",
            "notes": None,
            "confidence": 0.9,
        }
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                db.save_processed_message(
                    app_module.settings.database_path,
                    "cancel-mailbox",
                    "cancel-validity",
                    int(time.time()),
                    source_key,
                    [event],
                    source,
                )
                target = next(
                    item
                    for item in db.list_suggestions(
                        app_module.settings.database_path, include_dismissed=True
                    )
                    if item["source_key"] == source_key
                )
                db.mark_suggestion_exported(
                    app_module.settings.database_path,
                    target["id"],
                    f"family-calendar-assistant-{target['id']}@local.invalid",
                    "Familie",
                )
                db.save_processed_message(
                    app_module.settings.database_path,
                    "cancel-mailbox",
                    "cancel-validity",
                    int(time.time()) + 1,
                    f"{source_key}-reply",
                    [{**event, "action": "cancel"}],
                    {
                        **source,
                        "subject": "Elternabend abgesagt",
                        "message_id": f"<{source_key}-reply@example.org>",
                        "references": source["message_id"],
                    },
                )
                change_request = next(
                    item
                    for item in db.list_change_requests(app_module.settings.database_path)
                    if item["source_key"] == f"{source_key}-reply"
                )
                configured = replace(
                    app_module.settings,
                    caldav_username="tablet@example.org",
                    caldav_app_password="app-password",
                )
                with patch.object(app_module, "settings", configured):
                    with patch.object(
                        app_module.caldav,
                        "delete_icloud_event",
                        return_value="deleted",
                    ) as delete_event:
                        response = client.post(
                            f"/api/change-requests/{change_request['id']}/apply"
                        )

                self.assertEqual(response.status_code, 200)
                delete_event.assert_called_once()
                cancelled = db.get_suggestion(
                    app_module.settings.database_path, target["id"]
                )
                self.assertIsNotNone(cancelled["cancelled_at"])
                self.assertIsNotNone(cancelled["icloud_deleted_at"])

    def test_new_suggestion_is_available_before_mail_check_finishes(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                before = client.get("/api/activity").json()["suggestion_version"]
                db.save_processed_message(
                    app_module.settings.database_path,
                    "test-mailbox",
                    "test-validity",
                    int(time.time()),
                    f"live-update-{time.time_ns()}",
                    [
                        {
                            "title": "Live erkannter Termin",
                            "start": "2026-11-04T17:00:00",
                            "end": None,
                            "location": None,
                            "person": None,
                            "notes": None,
                            "confidence": 0.9,
                        }
                    ],
                    {
                        "subject": "Test",
                        "sender": "test@example.org",
                        "received_at": "2026-11-03T18:00:00+01:00",
                    },
                )

                activity = client.get("/api/activity").json()
                fragment = client.get("/suggestions-fragment")

                self.assertGreater(activity["suggestion_version"], before)
                self.assertIn("Live erkannter Termin", fragment.text)
                self.assertEqual(
                    int(fragment.text.split('data-version="')[1].split('"')[0]),
                    activity["suggestion_version"],
                )


def _add_suggestion(title, start, *, end=None, person=None, confidence=0.9, sender="schule@example.org", recurrence=None):
    db.save_processed_message(
        app_module.settings.database_path,
        "stage2-mailbox",
        "stage2-validity",
        int(time.time()),
        f"stage2-{time.time_ns()}",
        [{"title": title, "start": start, "end": end, "person": person, "confidence": confidence, "recurrence": recurrence}],
        {"subject": f"Betreff {title}", "sender": sender, "received_at": "2026-11-03T18:00:00+01:00"},
    )
    return next(
        item for item in db.list_suggestions(app_module.settings.database_path, include_dismissed=True)
        if item["title"] == title
    )


class StageTwoTests(unittest.TestCase):
    def _configured(self):
        return replace(
            app_module.settings,
            caldav_username="tablet@example.org",
            caldav_app_password="app-password",
        )

    def test_one_click_export_sets_calendar_learns_and_exports(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Elternabend 4b", "2099-01-15T19:30", person="Sam", sender="Lehrerin <klasse4b@example.org>")
                with patch.object(app_module, "settings", self._configured()):
                    with patch.object(app_module.caldav, "create_icloud_event", return_value="created") as create_event:
                        response = client.post(f"/api/suggestions/{item['id']}/export", json={"calendar": "Mia"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["calendar"], "Mia")
                self.assertEqual(create_event.call_args.args[5], "Mia")
                self.assertEqual(create_event.call_args.kwargs["default_event_minutes"], 60)
                stored = db.get_suggestion(app_module.settings.database_path, item["id"])
                self.assertEqual((stored["person"], stored["assignment_source"]), ("Mia", "manual"))
                corrections = db.list_person_corrections(app_module.settings.database_path, "klasse4b@example.org")
                self.assertEqual(corrections[0]["corrected_person"], "Mia")

    def test_one_click_export_rejects_unknown_calendar(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Zahnarzt", "2099-02-01T08:00")
                with patch.object(app_module, "settings", self._configured()):
                    response = client.post(f"/api/suggestions/{item['id']}/export", json={"calendar": "Privat"})
                self.assertEqual(response.status_code, 422)
                self.assertIsNone(db.get_suggestion(app_module.settings.database_path, item["id"])["person"])

    def test_past_suggestions_are_archived_unless_restored_on_purpose(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app):
                past = _add_suggestion("Vergangener Termin", "2020-05-01T10:00")
                future = _add_suggestion("Zukünftiger Termin", "2099-05-01T10:00")
                with self.assertLogs("src.app", level="INFO"):
                    app_module.archive_past_suggestions()
                path = app_module.settings.database_path
                self.assertEqual(db.get_suggestion(path, past["id"])["dismissed"], 1)
                self.assertEqual(db.get_suggestion(path, future["id"])["dismissed"], 0)
                history = [entry["action"] for entry in db.list_suggestion_history(path, past["id"])]
                self.assertIn("auto_archived", history)

                db.restore_suggestion(path, past["id"])
                app_module.archive_past_suggestions()
                self.assertEqual(db.get_suggestion(path, past["id"])["dismissed"], 0)

    def test_card_warns_when_similar_event_already_in_icloud(self):
        from src import calendar_backup
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                store = app_module.settings.calendar_store_path
                Path(store).unlink(missing_ok=True)
                item = _add_suggestion("Laternenumzug Kita", "2099-11-11T17:00")
                other = _add_suggestion("Zahnarzt Mia", "2099-11-11T09:00")
                exported_uid = app_module.caldav.event_uid(other["id"])
                cloud = {
                    "/c/a.ics": ('"1"', "BEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Laternenumzug der Kita\r\nDTSTART;TZID=Europe/Berlin:20991111T170000\r\nEND:VEVENT\r\n"),
                    "/c/b.ics": ('"1"', "BEGIN:VEVENT\r\nUID:y\r\nSUMMARY:Laternenumzug Kita\r\nDTSTART;VALUE=DATE:20991112\r\nEND:VEVENT\r\n"),
                    "/c/c.ics": ('"1"', f"BEGIN:VEVENT\r\nUID:{exported_uid}\r\nSUMMARY:Zahnarzt Mia\r\nDTSTART;TZID=Europe/Berlin:20991111T090000\r\nEND:VEVENT\r\n"),
                }
                with self.assertLogs("src.calendar_backup", level="INFO"):
                    calendar_backup.take_snapshot(
                        store, [{"name": "Familie", "url": "u"}],
                        lambda u: {h: e for h, (e, _) in cloud.items()},
                        lambda u, hs: {h: cloud[h] for h in hs},
                    )
                page = client.get("/suggestions-fragment").text
                card = page[page.index(f'id="suggestion-{item["id"]}"'):]
                card = card[: card.index("</article>")]
                self.assertEqual(card.count("Steht vermutlich schon in iCloud"), 1)
                self.assertIn("„Laternenumzug der Kita“ im Kalender „Familie“", card)
                other_card = page[page.index(f'id="suggestion-{other["id"]}"'):]
                other_card = other_card[: other_card.index("</article>")]
                self.assertNotIn("Steht vermutlich schon in iCloud", other_card)
                self.assertIn("Abgleich mit iCloud: Stand", page)
                Path(store).unlink(missing_ok=True)

    def test_day_view_marks_overlaps_and_shows_quote_and_colors(self):
        from src import calendar_backup
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                store = app_module.settings.calendar_store_path
                Path(store).unlink(missing_ok=True)
                db.save_processed_message(
                    app_module.settings.database_path, "dv", "dv", 1, f"dv-{time.time_ns()}",
                    [{"title": "Elternsprechtag", "start": "2099-11-10T17:00", "end": "2099-11-10T17:30",
                      "person": "Mia", "confidence": 0.9, "quote": "Elternsprechtag am 10.11. um 17 Uhr", "quote_source": "text"}],
                    {"subject": "Schule", "sender": "s@example.org", "received_at": "2026-11-04T08:00:00+00:00"},
                )
                cloud = {
                    "/c/1.ics": ('"1"', "BEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Training\r\nDTSTART;TZID=Europe/Berlin:20991110T170000\r\nDTEND;TZID=Europe/Berlin:20991110T180000\r\nEND:VEVENT\r\n"),
                    "/c/2.ics": ('"1"', "BEGIN:VEVENT\r\nUID:b\r\nSUMMARY:Abendessen\r\nDTSTART;TZID=Europe/Berlin:20991110T190000\r\nEND:VEVENT\r\n"),
                }
                with self.assertLogs("src.calendar_backup", level="INFO"):
                    calendar_backup.remember_calendars(store, [{"name": "Mia", "url": "u", "color": "#34c759"}])
                    calendar_backup.take_snapshot(
                        store, [{"name": "Mia", "url": "u"}],
                        lambda u: {h: e for h, (e, _) in cloud.items()},
                        lambda u, hs: {h: cloud[h] for h in hs},
                    )
                page = client.get("/suggestions-fragment").text
                self.assertIn("in iCloud: 2 Termine · 1 Überschneidung", page)
                self.assertIn("Überschneidet sich mit 1 Termin(en) im Kalender „Mia“", page)
                self.assertIn("17:00–18:00", page)
                self.assertIn("„Elternsprechtag am 10.11. um 17 Uhr“", page)
                self.assertIn("--cal-color: #34c759", page)
                Path(store).unlink(missing_ok=True)

    def test_fragment_shows_groups_uncertainty_and_choices(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                _add_suggestion("Unsicherer Termin", "2099-06-01T10:00", confidence=0.4, person="Ben")
                with patch.object(app_module, "settings", self._configured()):
                    page = client.get("/suggestions-fragment").text
                self.assertIn('class="group-heading">Später', page)
                self.assertIn("nur zu 40 % sicher", page)
                self.assertIn('data-calendar="Ben"', page)
                self.assertRegex(page, r'calendar-choice suggested"[^>]*data-calendar="Ben"')
                self.assertIn('data-default-minutes="60"', page)


class IgnoredSenderPageTests(unittest.TestCase):
    def test_ignore_sender_hides_entfernt_and_undo(self):
        path = app_module.settings.database_path
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                first = _add_suggestion("Paket kommt heute", "2099-03-01", sender="Shop <versand@lager.paketshop-test.de>")
                second = _add_suggestion("Paket verspätet", "2099-03-02", sender="info@paketshop-test.de")
                page = client.get("/suggestions-fragment").text
                self.assertIn("Absender ausblenden", page)
                self.assertIn("Nur versand@lager.paketshop-test.de", page)
                self.assertIn("Alles von paketshop-test.de", page)

                response = client.post(f"/api/suggestions/{first['id']}/ignore-sender", json={"scope": "domain"})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual((result["pattern"], result["created"], result["dismissed"], result["hidden"]), ("paketshop-test.de", True, True, 1))
                self.assertEqual(db.get_suggestion(path, first["id"])["dismissed"], 1)
                self.assertEqual(db.get_suggestion(path, second["id"])["hidden_reason"], "paketshop-test.de")

                page = client.get("/suggestions-fragment").text
                self.assertIn('id="hidden-suggestions"', page)
                self.assertIn("1 ausgeblendet", page)
                self.assertNotIn(f'id="suggestion-{second["id"]}"', page)
                self.assertIn(f'data-suggestion-id="{second["id"]}" data-title="Paket verspätet"', page)

                rules = client.get("/korrekturen").text
                self.assertIn("paketshop-test.de", rules)
                self.assertIn("1 ausgeblendet", rules)
                self.assertIn('aria-current="page">Regeln<', rules)

                # Rückgängig aus der Einblendung
                self.assertEqual(client.post("/api/ignored-senders/remove", json={"pattern": "paketshop-test.de"}).json()["shown"], 1)
                self.assertEqual(client.post(f"/api/suggestions/{first['id']}/undo-dismiss").status_code, 200)
                self.assertIsNone(db.get_suggestion(path, second["id"])["hidden_reason"])
                self.assertNotIn('id="hidden-suggestions"', client.get("/suggestions-fragment").text)
                client.post(f"/api/suggestions/{first['id']}/dismiss")
                client.post(f"/api/suggestions/{second['id']}/dismiss")

    def test_unhide_and_rules_form(self):
        path = app_module.settings.database_path
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Newsletter-Aktion", "2099-04-01", sender="news@werbung-test.de")
                response = client.post("/regeln/absender", data={"pattern": "werbung-test.de"}, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(db.get_suggestion(path, item["id"])["hidden_reason"], "werbung-test.de")
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/unhide").status_code, 200)
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/unhide").status_code, 409)
                self.assertIsNone(db.get_suggestion(path, item["id"])["hidden_reason"])

                invalid = client.post("/regeln/absender", data={"pattern": "kein muster"})
                self.assertEqual(invalid.status_code, 422)
                self.assertIn("E-Mail-Adresse oder Domain", invalid.text)
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/ignore-sender", json={"scope": "egal"}).status_code, 422)

                response = client.post("/regeln/absender/loeschen", data={"pattern": "werbung-test.de"}, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                rules_section = client.get("/korrekturen").text.split('id="quote"')[0]
                self.assertNotIn("werbung-test.de", rules_section)
                client.post(f"/api/suggestions/{item['id']}/dismiss")


class HideHintTests(unittest.TestCase):
    def test_hint_after_three_removals_and_trust(self):
        path = app_module.settings.database_path
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                for number in range(3):
                    old = _add_suggestion(f"Hinweis-Paket {number}", "2099-05-01", sender="versand@hinweis-test.de")
                    client.post(f"/api/suggestions/{old['id']}/dismiss")
                item = _add_suggestion("Hinweis-Paket neu", "2099-05-02", sender="versand@hinweis-test.de")
                page = client.get("/suggestions-fragment").text
                self.assertIn("Von hinweis-test.de hast du bisher 3 Vorschläge entfernt", page)
                self.assertIn('data-pattern="hinweis-test.de">Nicht mehr fragen', page)

                response = client.post("/api/sender-rules/trust", json={"pattern": "hinweis-test.de"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["created"])
                self.assertNotIn("Von hinweis-test.de hast du", client.get("/suggestions-fragment").text)
                rules = client.get("/korrekturen").text
                self.assertIn("Immer anzeigen", rules)
                self.assertIn("hinweis-test.de", rules)
                client.post("/regeln/absender/loeschen", data={"pattern": "hinweis-test.de"})
                self.assertIn("Von hinweis-test.de hast du", client.get("/suggestions-fragment").text)
                self.assertEqual(client.post("/api/sender-rules/trust", json={"pattern": "kein muster"}).status_code, 422)
                client.post(f"/api/suggestions/{item['id']}/dismiss")

    def test_newsletter_hidden_item_offers_trust(self):
        path = app_module.settings.database_path
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                db.save_processed_message(
                    path, "nl-mailbox", "1", 1, f"nl-{time.time_ns()}",
                    [{"title": "Herbstmarkt", "start": "2099-06-01", "confidence": 0.7, "hidden_reason": db.NEWSLETTER_REASON}],
                    {"subject": "Newsletter", "sender": "info@markt-test.de", "received_at": "2026-11-06T10:00:00+01:00"},
                )
                page = client.get("/suggestions-fragment").text
                self.assertIn("Grund: Newsletter, KI-Sicherheit 70 %", page)
                self.assertIn("markt-test.de immer zeigen", page)
                self.assertEqual(client.post("/api/sender-rules/trust", json={"pattern": "markt-test.de"}).json()["shown"], 1)
                self.assertNotIn('id="hidden-suggestions"', client.get("/suggestions-fragment").text)
                client.post("/regeln/absender/loeschen", data={"pattern": "markt-test.de"})
                item = next(i for i in db.list_suggestions(path) if i["title"] == "Herbstmarkt")
                client.post(f"/api/suggestions/{item['id']}/dismiss")


class ReanalysisApiTests(unittest.TestCase):
    def test_reanalysis_runs_in_background_and_adds_new_findings(self):
        from email.message import EmailMessage
        path = app_module.settings.database_path
        message = EmailMessage()
        message["Subject"] = "Termine Reanalyse"
        message["From"] = "schule@reanalyse-test.de"
        message.set_content("Text")
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Reanalyse Elternabend", "2099-07-01T19:00", sender="schule@reanalyse-test.de")
                self.assertEqual(client.post("/api/messages/reanalyze", json={"source_key": "gibt/es/nicht"}).status_code, 404)
                found = [
                    {"title": "Reanalyse Elternabend", "start": "2099-07-01T19:00", "confidence": 1.0},
                    {"title": "Reanalyse Sportfest", "start": "2099-07-10", "confidence": 0.9},
                ]
                with (
                    patch.object(app_module.mail, "fetch_for_reanalysis", return_value=message),
                    patch.object(app_module.mail, "analyze_message", return_value=found),
                ):
                    response = client.post("/api/messages/reanalyze", json={"source_key": item["source_key"]})
                    self.assertEqual(response.status_code, 202, response.text)
                    for _ in range(50):
                        state = client.get("/api/messages/reanalyze", params={"source_key": item["source_key"]}).json()
                        if state["status"] in ("done", "error"):
                            break
                        time.sleep(0.05)
                self.assertEqual(state["status"], "done", state)
                self.assertIn("1 neue(r) Vorschlag", state["message"])
                self.assertIn("1 schon vorhanden", state["message"])
                titles = [i["title"] for i in db.list_suggestions(path) if i["source_key"] == item["source_key"]]
                self.assertEqual(sorted(titles), ["Reanalyse Elternabend", "Reanalyse Sportfest"])
                self.assertIn("Mail erneut auswerten", client.get("/suggestions-fragment").text)

                with patch.object(app_module.mail, "fetch_for_reanalysis", side_effect=app_module.mail.ReanalysisError("Die Mail ist nicht mehr im Postfach.")):
                    client.post("/api/messages/reanalyze", json={"source_key": item["source_key"]})
                    for _ in range(50):
                        state = client.get("/api/messages/reanalyze", params={"source_key": item["source_key"]}).json()
                        if state["status"] in ("done", "error"):
                            break
                        time.sleep(0.05)
                self.assertEqual((state["status"], state["message"]), ("error", "Die Mail ist nicht mehr im Postfach."))

                rules = client.get("/korrekturen").text
                self.assertIn("Trefferquote je Absender", rules)
                self.assertIn("reanalyse-test.de", rules)
                for suggestion in db.list_suggestions(path):
                    if suggestion["source_key"] == item["source_key"]:
                        client.post(f"/api/suggestions/{suggestion['id']}/dismiss")


class StageCTests(unittest.TestCase):
    def test_reminder_select_api_and_reset_on_allday_change(self):
        path = app_module.settings.database_path
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Erinnerung Zahnarzt", "2099-08-01T09:00")
                page = client.get("/suggestions-fragment").text
                self.assertIn('class="reminder-select"', page)
                self.assertIn("Standard (1 Std. vorher)", page)
                response = client.post(f"/api/suggestions/{item['id']}/reminder", json={"reminder": "15"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["label"], "15 Min. vorher")
                self.assertEqual(db.get_suggestion(path, item["id"])["reminder"], "15")
                self.assertIn('<option value="15" selected>', client.get("/suggestions-fragment").text)
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/reminder", json={"reminder": "abc"}).status_code, 422)
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/reminder", json={"reminder": "999999"}).status_code, 422)
                self.assertEqual(client.post("/api/suggestions/999999/reminder", json={"reminder": None}).status_code, 404)
                response = client.put(f"/api/suggestions/{item['id']}", json={"title": "Erinnerung Zahnarzt", "start": "2099-08-01", "end": None, "location": "Praxis Dr. Müller, Hauptstr. 1", "person": None, "notes": None})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(db.get_suggestion(path, item["id"])["reminder"])
                page = client.get("/suggestions-fragment").text
                self.assertIn("Standard (Am Vortag um 18 Uhr)", page)
                self.assertIn('href="https://maps.apple.com/?q=Praxis%20Dr.%20M%C3%BCller%2C%20Hauptstr.%201"', page)
                client.post(f"/api/suggestions/{item['id']}/dismiss")

    def test_maintenance_page(self):
        path = app_module.settings.database_path
        db.record_ai_run(path, 75, True, "test-model")
        db.record_ai_run(path, 5, False, "test-model")
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                page = client.get("/protokoll/wartung")
                self.assertEqual(page.status_code, 200, page.text)
                for text in ("Wartungs-Übersicht", "Fehlgeschlagen", "(50 %)", "1 min 15 s", "test-model", "Datenbank-Sicherungen", "Verarbeitete Mails", 'aria-current="page">Protokoll<'):
                    self.assertIn(text, page.text)
                self.assertIn("/protokoll/wartung", client.get("/protokoll").text)


class TargetCalendarSettingsTests(unittest.TestCase):
    def test_settings_section_and_save(self):
        path = app_module.settings.database_path
        before = db.list_target_calendars(path)
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                page = client.get("/einstellungen").text
                self.assertIn('id="zielkalender"', page)
                self.assertIn('name="cal-0-name" value="Alex"', page)
                response = client.post("/einstellungen/zielkalender", data={
                    "cal-0-name": "Alex", "cal-0-target": "1", "cal-0-description": "Elternteil", "cal-0-position": "2",
                    "cal-1-name": "Mia", "cal-1-target": "1", "cal-1-position": "1",
                    "cal-2-name": "Familie", "cal-2-target": "1", "cal-2-shared": "1", "cal-2-position": "3",
                    "cal-3-name": "Sam", "cal-3-position": "4",
                }, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                saved = db.list_target_calendars(path)
                self.assertEqual([(c["name"], c["description"], c["shared"]) for c in saved],
                                 [("Mia", "", False), ("Alex", "Elternteil", False), ("Familie", "", True)])
                item = _add_suggestion("Zielkalender-Test", "2099-09-01")
                self.assertNotIn('data-calendar="Sam"', client.get("/suggestions-fragment").text)
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/export", json={"calendar": "Sam"}).status_code, 422)
                client.post(f"/api/suggestions/{item['id']}/dismiss")
        db.save_target_calendars(path, before)


class DemoModeTests(unittest.TestCase):
    def test_demo_seed_and_fakes(self):
        import tempfile as _tempfile
        from src import caldav, demo
        directory = _tempfile.TemporaryDirectory()
        path = os.path.join(directory.name, "demo.sqlite3")
        store = os.path.join(directory.name, "calendar-history.sqlite3")
        db.initialize(path)
        originals = {name: getattr(caldav, name) for name in ("discover_icloud_calendars", "create_icloud_event", "resend_icloud_event", "delete_icloud_event", "restore_event_resource", "open_client", "list_event_etags", "fetch_events")}
        try:
            demo.install_fakes()
            self.assertEqual(caldav.create_icloud_event("x"), "created")
            self.assertEqual([c["name"] for c in caldav.discover_icloud_calendars()], ["Alex", "Sam", "Mia", "Ben", "Familie"])
        finally:
            for name, function in originals.items():
                setattr(caldav, name, function)
        demo.seed(path, store)
        titles = {i["title"] for i in db.list_suggestions(path)}
        self.assertIn("Elternabend Klasse 4b", titles)
        self.assertIn("Elterngespräch Mia", titles)
        self.assertTrue(any(i["hidden_reason"] for i in db.list_suggestions(path)))
        self.assertEqual(len(db.list_target_calendars(path)), 5)
        count = len(db.list_suggestions(path, include_dismissed=True))
        demo.seed(path, store)  # nicht doppelt
        self.assertEqual(len(db.list_suggestions(path, include_dismissed=True)), count)
        english = os.path.join(directory.name, "demo-en.sqlite3")
        db.initialize(english)
        demo.seed(english, os.path.join(directory.name, "store-en.sqlite3"), "en")
        self.assertIn("Parents’ evening class 4b", {i["title"] for i in db.list_suggestions(english)})
        self.assertEqual(db.list_target_calendars(english)[-1], {"name": "Family", "description": "", "shared": True})
        directory.cleanup()

    def test_demo_config_does_not_need_credentials(self):
        from src.config import Settings
        with patch.dict(os.environ, {"DEMO_MODE": "1", "IMAP_HOST": "", "IMAP_USERNAME": "", "IMAP_PASSWORD": ""}):
            self.assertTrue(Settings.from_env().demo_mode)
        with patch.dict(os.environ, {"DEMO_MODE": "0"}):
            self.assertFalse(Settings.from_env().demo_mode)


class LanguageTests(unittest.TestCase):
    PAGES = ("/", "/archiv", "/korrekturen", "/sicherungen", "/sicherungen/kalender", "/protokoll",
             "/protokoll/wartung", "/einstellungen", "/hilfe", "/suggestions-fragment", "/change-requests-fragment")

    def test_every_page_renders_in_both_languages(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Sprachtest Elternabend", "2099-10-01T19:00", person="Mia",
                                       recurrence={"frequency": "weekly", "interval": 1, "weekdays": ["TH"], "until": None, "count": None, "exdates": []})
                for language in ("de", "en"):
                    client.cookies.set("lang", language)
                    for page in self.PAGES:
                        response = client.get(page)
                        self.assertEqual(response.status_code, 200, f"{language} {page}: {response.text[:300]}")
                        self.assertEqual(response.headers["content-language"], language)
                english = client.get("/").text
                self.assertIn('<html lang="en">', english)
                self.assertIn(">Suggestions<", english)
                self.assertIn("Every Thu, no end date", english)
                self.assertIn('/i18n/en.js?v=', english)
                self.assertIn("How it works", client.get("/hilfe").text)
                self.assertIn('"Suggestions"', client.get("/i18n/en.js").text)
                self.assertEqual(client.get("/i18n/fr.js").status_code, 404)
                self.assertIn("Please choose one of the target calendars", client.put(
                    f"/api/suggestions/{item['id']}",
                    json={"title": "x", "start": "2099-10-01T19:00", "end": None, "location": None, "person": "Unbekannt", "notes": None},
                ).json()["detail"])
                client.cookies.set("lang", "de")
                german = client.get("/").text
                self.assertIn('<html lang="de">', german)
                self.assertNotIn("/i18n/", german)
                client.cookies.clear()
                self.assertIn('<html lang="en">', client.get("/", headers={"Accept-Language": "en-US,en;q=0.8"}).text)
                client.post(f"/api/suggestions/{item['id']}/dismiss")


class SetupModeTests(unittest.TestCase):
    def _setup_settings(self):
        return replace(app_module.settings, setup_mode=True)

    def test_redirect_page_and_endpoints_only_in_setup_mode(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self.assertEqual(client.post("/api/setup/imap", json={"host": "h", "username": "u", "password": "p"}).status_code, 404)
                self.assertEqual(client.get("/einrichtung", follow_redirects=False).status_code, 303)
                with patch.object(app_module, "settings", self._setup_settings()):
                    response = client.get("/", follow_redirects=False)
                    self.assertEqual((response.status_code, response.headers["location"]), (303, "/einrichtung"))
                    self.assertEqual(client.get("/health").status_code, 200)
                    page = client.get("/einrichtung").text
                    self.assertIn('id="setup-form"', page)
                    self.assertIn("Europe/Berlin", page)
                    self.assertIn('id="theme-toggle"', page)
                    with patch.object(app_module.setup_wizard, "imap_test", return_value=["INBOX", "Schule"]):
                        self.assertEqual(client.post("/api/setup/imap", json={"host": "h", "username": "u", "password": "p"}).json()["folders"], ["INBOX", "Schule"])
                    with patch.object(app_module.setup_wizard, "imap_test", side_effect=app_module.setup_wizard.SetupError("Anmeldung falsch")):
                        response = client.post("/api/setup/imap", json={"host": "h", "username": "u", "password": "p"})
                        self.assertEqual((response.status_code, response.json()["detail"]), (422, "Anmeldung falsch"))
                    with patch.object(app_module.setup_wizard, "ai_models", return_value=["m1"]):
                        self.assertEqual(client.post("/api/setup/ai-models", json={"base_url": "http://ki:1/v1"}).json()["models"], ["m1"])
                    with patch.object(app_module.setup_wizard, "ai_probe", return_value=[{"title": "Elternabend", "start": "2099-01-01T19:30", "confidence": 0.9}]):
                        data = client.post("/api/setup/ai-probe", json={"base_url": "http://ki:1/v1", "model": "m1"}).json()
                        self.assertEqual(data["events"][0]["title"], "Elternabend")
                        self.assertEqual(data["events"][0]["confidence"], 90)
                    with patch.object(app_module.setup_wizard, "caldav_test", return_value=[{"name": "Alex"}]):
                        self.assertEqual(client.post("/api/setup/caldav", json={"username": "u", "password": "p"}).json()["calendars"], [{"name": "Alex"}])
                    self.assertTrue(client.get("/api/setup/state").json()["setup_mode"])

    def test_skip_and_resume(self):
        path = app_module.settings.database_path
        unconfigured = replace(app_module._env_settings, setup_mode=True, mail_configured=False)
        with patch.object(app_module.mail, "poll_mailbox"), \
                patch.object(app_module, "_env_settings", unconfigured), \
                patch.object(app_module, "settings", unconfigured):
            with TestClient(app_module.app) as client:
                self.assertEqual(client.get("/", follow_redirects=False).headers["location"], "/einrichtung")
                self.assertIn('id="setup-skip"', client.get("/einrichtung").text)
                self.assertEqual(client.post("/api/setup/skip").status_code, 200)
                self.assertFalse(app_module.settings.setup_mode)
                self.assertNotIn("Postfachabruf", app_module._workers)
                self.assertIn("KI-Prüfung", app_module._workers)
                page = client.get("/", follow_redirects=False)
                self.assertEqual(page.status_code, 200)
                self.assertIn('id="setup-resume"', page.text)
                self.assertEqual(client.get("/einrichtung", follow_redirects=False).headers["location"], "/")
                self.assertEqual(client.post("/api/setup/skip").status_code, 404)
                self.assertEqual(client.post("/api/check").status_code, 409)
                self.assertEqual(client.get("/health").json()["checks"]["mail"], "off")
            # Auch nach einem Neustart bleibt der Assistent aus
            with TestClient(app_module.app) as client:
                self.assertEqual(client.get("/", follow_redirects=False).status_code, 200)
                self.assertNotIn("Postfachabruf", app_module._workers)
                self.assertEqual(client.post("/api/setup/resume").status_code, 200)
                self.assertTrue(app_module.settings.setup_mode)
                self.assertEqual(client.get("/", follow_redirects=False).headers["location"], "/einrichtung")
        self.assertFalse(db.get_service_state(path).get(app_module.SETUP_SKIPPED_KEY))
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self.assertNotIn('id="setup-resume"', client.get("/").text)
                self.assertEqual(client.post("/api/setup/resume").status_code, 404)

    def test_finish_with_and_without_updater(self):
        before = db.list_target_calendars(app_module.settings.database_path)
        payload = {"language": "en", "timezone": "Europe/Berlin", "imap_host": "imap.example.org", "imap_username": "a@b.de",
                   "imap_password": "it's $x", "ai_base_url": "http://ki:1/v1", "ai_model": "m1",
                   "targets": [{"name": "Neu", "description": "Test", "shared": False}]}
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                with patch.object(app_module, "settings", self._setup_settings()):
                    with patch.object(app_module.updates, "updater_status", return_value=None):
                        data = client.post("/api/setup/finish", json=payload).json()
                        self.assertEqual(data["mode"], "manual")
                        self.assertIn('IMAP_PASSWORD="it\'s $$x"', data["env"])
                        self.assertIn("APP_LANGUAGE=en", data["env"])
                    self.assertEqual(db.list_target_calendars(app_module.settings.database_path)[0]["name"], "Neu")
                    with patch.object(app_module.updates, "updater_status", return_value={"state": "idle"}), \
                         patch.object(app_module.updates, "configure", return_value=(True, "gespeichert")) as configure:
                        self.assertEqual(client.post("/api/setup/finish", json=payload).json(), {"mode": "updater"})
                        self.assertEqual(configure.call_args.args[1]["IMAP_PASSWORD"], "it's $x")
                    self.assertEqual(client.post("/api/setup/finish", json={**payload, "timezone": "Mond/Krater"}).status_code, 422)
        db.save_target_calendars(app_module.settings.database_path, before)


class ReleaseCacheTests(unittest.TestCase):
    def test_failures_are_cached_briefly(self):
        from src import updates
        import httpx
        updates._cache.clear()
        with patch.object(updates.httpx, "get", side_effect=httpx.ConnectError("weg")) as get:
            self.assertIsNone(updates.releases("x/y"))
            self.assertIsNone(updates.releases("x/y"))
            self.assertEqual(get.call_count, 1)
        stamp, value = updates._cache["x/y"]
        updates._cache["x/y"] = (stamp - updates._FAILURE_CACHE_SECONDS - 1, value)
        response = unittest.mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = [{"tag_name": "v1.2.3", "body": "neu"}, {"tag_name": "nightly"}]
        with patch.object(updates.httpx, "get", return_value=response):
            self.assertEqual([r["version"] for r in updates.releases("x/y")], ["1.2.3"])
        updates._cache.clear()


class UpdatePageTests(unittest.TestCase):
    RELEASES = [
        {"version": "9.0.0", "key": (9, 0, 0), "published": "2099-01-01T00:00:00Z", "notes": "Neu: alles"},
        {"version": "0.1.0", "key": (0, 1, 0), "published": "2020-01-01T00:00:00Z", "notes": ""},
    ]

    def test_fragment_without_updater_and_start_validation(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                with patch.object(app_module.updates, "releases", return_value=self.RELEASES), \
                     patch.object(app_module.updates, "updater_status", return_value=None):
                    page = client.get("/sicherungen/update-fragment").text
                    self.assertIn("Neue Version 9.0.0 verfügbar", page)
                    self.assertIn("Neu: alles", page)
                    self.assertIn("docker compose --profile updater up -d", page)
                    self.assertIn('data-version="9.0.0" disabled', page)
                    self.assertEqual(client.post("/api/update", json={"version": "8.8.8"}).status_code, 422)
                    self.assertEqual(client.post("/api/update", json={"version": "9.0.0"}).status_code, 503)
                self.assertIn('id="update-container"', client.get("/sicherungen").text)

    def test_start_update_backs_up_first(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                with patch.object(app_module.updates, "releases", return_value=self.RELEASES), \
                     patch.object(app_module.updates, "updater_status", return_value={"state": "done", "backup": "calendar-assistant-2026-01-01_000000.sqlite3"}), \
                     patch.object(app_module.updates, "start_update", return_value=(True, "gestartet")) as start, \
                     patch.object(app_module.updates, "backup_calendar_store") as store_backup:
                    page = client.get("/sicherungen/update-fragment").text
                    self.assertIn("calendar-assistant-2026-01-01_000000.sqlite3", page)
                    response = client.post("/api/update", json={"version": "0.1.0", "restore_data": True})
                    self.assertEqual(response.status_code, 202, response.text)
                payload = start.call_args.args[1]
                self.assertEqual(payload["version"], "0.1.0")
                self.assertEqual(payload["previous"], app_module.version.VERSION)
                self.assertEqual(payload["restore_backup"], "calendar-assistant-2026-01-01_000000.sqlite3")
                self.assertTrue((Path(app_module.settings.backup_dir) / payload["backup"]).exists())
                store_backup.assert_called_once()


class CalendarBackupPageTests(unittest.TestCase):
    def tearDown(self):
        db.reset_app_settings(app_module.settings.database_path)
        app_module._reload_settings()

    def _seed(self):
        from src import calendar_backup
        from datetime import timedelta as _td
        store = app_module.settings.calendar_store_path
        Path(store).unlink(missing_ok=True)
        cloud = {"/c/fam/a.ics": ('"1"', "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Martinszug\r\nDTSTART:20261113T160000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")}
        calendars = [{"name": "Familie", "url": "https://p1.example/c/fam/"}]
        t0 = datetime(2026, 11, 4, tzinfo=timezone.utc)
        with self.assertLogs("src.calendar_backup", level="INFO"):
            calendar_backup.remember_calendars(store, calendars + [{"name": "Privat", "url": "https://p1.example/c/priv/"}])
            first = calendar_backup.take_snapshot(store, calendars, lambda u: {h: e for h, (e, _) in cloud.items()}, lambda u, hs: {h: cloud[h] for h in hs}, now=t0)
            cloud.clear()
            calendar_backup.take_snapshot(store, calendars, lambda u: {}, lambda u, hs: {}, now=t0 + _td(hours=1))
        return first["snapshot_id"]

    def test_search_page_and_single_restore(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                first = self._seed()
                latest = client.get("/sicherungen/kalender?zeitraum=alle").text
                self.assertIn("0 Termine", latest)
                self.assertIn(f'href="/sicherungen/kalender?stand={first}&amp;zeitraum=alle"', latest)  # Änderungsverlauf
                page = client.get(f"/sicherungen/kalender?stand={first}&zeitraum=alle")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Martinszug", page.text)
                self.assertIn("status-gelöscht", page.text)
                self.assertIn("November 2026", page.text)
                self.assertIn("Fr. 13.", page.text)
                self.assertIn("17:00", page.text)
                presence_id = int(re.search(r"/eintrag/(\d+)/wiederherstellen", page.text).group(1))

                configured = replace(app_module.settings, caldav_username="t@example.org", caldav_app_password="pw")
                with (
                    patch.object(app_module, "settings", configured),
                    patch.object(app_module.caldav, "discover_icloud_calendars", return_value=[{"name": "Familie", "calendar_url": "https://p1.example/c/fam-neu/", "write_access": True}]),
                    patch.object(app_module.caldav, "restore_event_resource", return_value="created") as restore,
                    patch.object(app_module, "_run_calendar_backup_quietly", new=AsyncMock()),
                ):
                    response = client.post(
                        f"/sicherungen/kalender/eintrag/{presence_id}/wiederherstellen",
                        headers={"Referer": f"http://testserver/sicherungen/kalender?stand={first}&restored=alt"},
                        follow_redirects=False,
                    )
                self.assertEqual(response.status_code, 303)
                self.assertIn("restored=Martinszug", response.headers["location"])
                self.assertNotIn("restored=alt", response.headers["location"])
                args = restore.call_args.args
                self.assertEqual(args[1:3], ("https://p1.example/c/fam-neu/", "a.ics"))
                self.assertIn("SUMMARY:Martinszug", args[3])

    def test_unchanged_events_offer_overwrite(self):
        from src import calendar_backup
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                store = app_module.settings.calendar_store_path
                Path(store).unlink(missing_ok=True)
                cloud = {"/c/a.ics": ('"1"', "BEGIN:VEVENT\r\nUID:a\r\nSUMMARY:Repair Café\r\nDTSTART:20261113T160000Z\r\nEND:VEVENT\r\n")}
                with self.assertLogs("src.calendar_backup", level="INFO"):
                    calendar_backup.take_snapshot(
                        store, [{"name": "Alex", "url": "u"}],
                        lambda u: {h: e for h, (e, _) in cloud.items()},
                        lambda u, hs: {h: cloud[h] for h in hs},
                    )
                page = client.get("/sicherungen/kalender?zeitraum=alle").text
                self.assertIn("Repair Café", page)
                self.assertNotIn("status-unverändert", page)
                self.assertEqual(page.count("Wiederherstellen</button>"), 1)
                self.assertIn('class="small ghost"', page)
                self.assertIn("„Repair Café“ im Kalender „Alex“ mit dem Stand aus dieser Sicherung überschreiben?", page)
                self.assertNotIn('class="tabs"', page)  # neuester Stand: keine Reiter
                Path(store).unlink(missing_ok=True)

    def test_history_tabs_diff_live_parts_and_paging(self):
        from src import calendar_backup
        from datetime import timedelta as _td
        store = app_module.settings.calendar_store_path
        Path(store).unlink(missing_ok=True)

        def event(uid, title, start, location=""):
            extra = f"LOCATION:{location}\r\n" if location else ""
            return f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nDTSTART:{start}\r\n{extra}END:VEVENT\r\n"

        cloud = {
            "/c/a.ics": ('"1"', event("a", "Martinszug", "20261113T160000Z")),
            "/c/b.ics": ('"1"', event("b", "Elternabend", "20261120T183000Z", "Raum 12")),
            "/c/c.ics": ('"1"', event("c", "Sommerfest", "20260601T100000Z")),
        }
        calendars = [{"name": "Familie", "url": "u"}]
        t0 = datetime(2026, 11, 4, tzinfo=timezone.utc)
        snap = lambda when: calendar_backup.take_snapshot(store, calendars, lambda u: {h: e for h, (e, _) in cloud.items()}, lambda u, hs: {h: cloud[h] for h in hs}, now=when)
        with self.assertLogs("src.calendar_backup", level="INFO"):
            first = snap(t0)["snapshot_id"]
            snap(t0 + _td(hours=1))  # ohne Änderungen
            del cloud["/c/a.ics"]
            cloud["/c/b.ics"] = ('"2"', event("b", "Elternabend", "20261120T190000Z", "Aula"))
            cloud["/c/d.ics"] = ('"1"', event("d", "Laternenbasteln", "20261121T150000Z"))
            snap(t0 + _td(hours=2))
        before = first + 1  # Stand direkt vor der Änderung
        today = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
        with patch.object(app_module.mail, "poll_mailbox"), patch.object(app_module, "datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda tz=None: today.astimezone(tz) if tz else today
            with TestClient(app_module.app) as client:
                newest = client.get("/sicherungen/kalender").text
                self.assertIn(f"stand={before}", newest)
                self.assertIn("1 gelöscht", newest)
                self.assertIn("1 geändert", newest)
                self.assertIn("Erste Sicherung", newest)
                self.assertNotIn("Sommerfest", newest)  # vergangen, Standard „Kommende“
                self.assertIn("Sommerfest", client.get("/sicherungen/kalender?zeitraum=vergangen").text)

                page = client.get(f"/sicherungen/kalender?stand={before}").text
                self.assertIn('aria-current="true"', page)
                self.assertIn("Stand vor den Änderungen", page)
                self.assertRegex(page, r'tab-gelöscht" data-live href="[^"]*"\s*aria-current="true">Gelöscht <span class="chip-count">1')
                self.assertIn("Martinszug", page)
                self.assertNotIn("Elternabend", page.split('id="backup-list"')[1])

                changed = client.get(f"/sicherungen/kalender?stand={before}&status=geändert").text
                listing = changed.split('id="backup-list"')[1]
                self.assertIn("<mark>Raum 12</mark>", listing)
                self.assertIn("<mark>Aula</mark>", listing)
                self.assertEqual(listing.count("Wiederherstellen</button>"), 1)

                self.assertRegex(page, r'tab-neu" data-live href="[^"]*">Neu <span class="chip-count">1')
                added = client.get(f"/sicherungen/kalender?stand={before}&status=neu").text.split('id="backup-list"')[1]
                self.assertIn("Laternenbasteln", added)
                self.assertIn("status-neu", added)
                self.assertNotIn("Wiederherstellen</button>", added)  # „neu“ lässt sich nicht wiederherstellen
                self.assertNotIn("Martinszug", added)

                part = client.get(f"/sicherungen/kalender?stand={before}&status=alle&teil=liste").text
                self.assertNotIn("<html", part)
                self.assertIn('id="backup-list"', part)
                with patch.object(app_module, "BACKUP_PAGE_SIZE", 1):
                    first_page = client.get(f"/sicherungen/kalender?stand={before}&status=alle&teil=liste").text
                    self.assertIn("seite=2", first_page)
                    self.assertIn("Martinszug", first_page)
                    more = client.get(f"/sicherungen/kalender?stand={before}&status=alle&seite=2&teil=mehr").text
                    self.assertIn("Elternabend", more)
                    self.assertNotIn("Martinszug", more)
                    self.assertNotIn("seite=3", more)
        Path(store).unlink(missing_ok=True)

    def test_history_grouped_by_day_week_month(self):
        def point(pid, when, before, deleted=0, changed=0, added=0):
            return {"id": pid, "taken_at": when, "before_id": before, "deleted": deleted, "changed": changed, "added": added}
        points = [  # neueste zuerst, wie change_points
            point(9, "2026-11-10T09:00:00+00:00", 8, deleted=1),           # Di., KW 46
            point(7, "2026-11-08T18:00:00+00:00", 6, changed=2),           # So., KW 45
            point(5, "2026-11-08T08:00:00+00:00", 4, added=1),             # So., KW 45
            point(3, "2026-11-02T08:00:00+00:00", 2, deleted=1),           # Mo., KW 45
            point(1, "2026-10-20T08:00:00+00:00", None, added=50),         # erste Sicherung
        ]
        weeks = app_module._history_groups(points, "woche")
        self.assertEqual([g["label"] for g in weeks], [
            "KW 46 · 9.–15. Nov. 2026", "KW 45 · 2.–8. Nov. 2026", "Di., 20. Okt. 2026, 10:00"])
        self.assertEqual((weeks[1]["deleted"], weeks[1]["changed"], weeks[1]["added"], weeks[1]["before_id"]), (1, 2, 1, 2))
        self.assertTrue(weeks[2]["first"])
        days = app_module._history_groups(points, "tag")
        self.assertEqual([g["before_id"] for g in days], [8, 4, 2, None])
        months = app_module._history_groups(points, "monat")
        self.assertEqual([g["label"] for g in months], ["November 2026", "Di., 20. Okt. 2026, 10:00"])
        self.assertEqual(months[0]["before_id"], 2)
        self.assertEqual(app_module.formatting.week_label(date(2026, 12, 28)), "KW 53 · 28. Dez. 2026 – 3. Jan. 2027")
        self.assertEqual(app_module.formatting.week_label(date(2026, 10, 26)), "KW 44 · 26. Okt. – 1. Nov. 2026")

    def test_history_grouping_choice_is_remembered(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self._seed()
                page = client.get("/sicherungen/kalender").text
                self.assertRegex(page, r'verlauf=woche"\s*aria-current="true"')  # Standard: Woche
                response = client.get("/sicherungen/kalender?verlauf=monat")
                self.assertEqual(response.cookies.get("verlauf"), "monat")
                self.assertRegex(client.get("/sicherungen/kalender").text, r'verlauf=monat"\s*aria-current="true"')
                client.cookies.clear()

    def test_mass_deletion_warning_and_dismiss(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                first = self._seed()
                with self.assertLogs("src.app", "WARNING"):
                    app_module._check_mass_deletion({
                        "snapshot_id": first + 1, "deleted": 8,
                        "deleted_by_calendar": {"Familie": 8, "Privat": 0},
                        "before_by_calendar": {"Familie": 10, "Privat": 4},
                    })
                for url in ("/", "/sicherungen", "/sicherungen/kalender"):
                    page = client.get(url).text
                    self.assertIn("Auffällig viele Termine fehlen", page, url)
                self.assertIn(f"/sicherungen/kalender?stand={first}&amp;status=gel%C3%B6scht&amp;zeitraum=alle", page)
                self.assertIn("8 Termine (Familie)", page)
                response = client.post("/sicherungen/warnung/ausblenden", headers={"Referer": "http://testserver/sicherungen"}, follow_redirects=False)
                self.assertEqual((response.status_code, response.headers["location"]), (303, "/sicherungen"))
                self.assertNotIn("Auffällig viele Termine fehlen", client.get("/").text)
                app_module._check_mass_deletion({"snapshot_id": first + 1, "deleted": 2,
                                                 "deleted_by_calendar": {"Familie": 2}, "before_by_calendar": {"Familie": 3}})
                self.assertNotIn("Auffällig viele Termine fehlen", client.get("/").text)  # unter der Schwelle

    def test_calendar_selection_defaults_to_all_and_can_exclude(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self._seed()
                configured = replace(app_module.settings, caldav_username="t@example.org", caldav_app_password="pw")
                with patch.object(app_module, "settings", configured):
                    page = client.get("/sicherungen").text
                    self.assertRegex(page, r'value="Privat" checked')
                    self.assertIn("Letzte Sicherung", page)
                    client.post(
                        "/sicherungen/kalender/auswahl",
                        content="calendar=Familie",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    self.assertEqual(app_module._excluded_calendars(), {"Privat"})
                    page = client.get("/sicherungen").text
                    self.assertRegex(page, r'value="Familie" checked')
                    self.assertNotRegex(page, r'value="Privat" checked')


class LoginTests(unittest.TestCase):
    def setUp(self):
        app_module._login_failures.clear()

    def test_without_password_everything_is_open(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self.assertEqual(client.get("/", follow_redirects=False).status_code, 200)
                self.assertEqual(client.get("/anmelden", follow_redirects=False).headers["location"], "/")
                self.assertNotIn('action="/abmelden"', client.get("/").text)

    def test_password_protects_pages_and_api(self):
        protected = replace(app_module.settings, app_password="geheim-123")
        with patch.object(app_module.mail, "poll_mailbox"), patch.object(app_module, "LOGIN_FAILURE_DELAY", 0):
            with TestClient(app_module.app) as client, patch.object(app_module, "settings", protected):
                response = client.get("/archiv?q=x", follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/anmelden?weiter=%2Farchiv%3Fq%3Dx")
                self.assertEqual(client.get("/api/suggestions").status_code, 401)
                self.assertEqual(client.post("/api/check").status_code, 401)
                for url in ("/health", "/static/app.css", "/api/version", "/i18n/en.js"):
                    self.assertEqual(client.get(url).status_code, 200, url)
                self.assertIn('name="passwort"', client.get("/anmelden").text)
                form = {"Content-Type": "application/x-www-form-urlencoded"}
                with self.assertLogs("src.app", "WARNING"):
                    wrong = client.post("/anmelden", content="passwort=falsch&weiter=/archiv", headers=form, follow_redirects=False)
                self.assertIn("fehler=", wrong.headers["location"])
                self.assertNotIn(app_module.SESSION_COOKIE, wrong.cookies)
                ok = client.post("/anmelden", content="passwort=geheim-123&weiter=/archiv&bleiben=1", headers=form, follow_redirects=False)
                self.assertEqual(ok.headers["location"], "/archiv")
                self.assertEqual(client.get("/archiv", follow_redirects=False).status_code, 200)
                self.assertIn('action="/abmelden"', client.get("/").text)
                # Fremdes Ziel wird ignoriert
                evil = client.post("/anmelden", content="passwort=geheim-123&weiter=//evil.example", headers=form, follow_redirects=False)
                self.assertEqual(evil.headers["location"], "/")
                # Neues Passwort meldet ab
                with patch.object(app_module, "settings", replace(protected, app_password="anderes-456")):
                    self.assertEqual(client.get("/", follow_redirects=False).status_code, 303)
                client.post("/abmelden")
                self.assertEqual(client.get("/", follow_redirects=False).status_code, 303)
                client.cookies.clear()

    def test_repeated_failures_lock_out(self):
        protected = replace(app_module.settings, app_password="geheim-123")
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        with patch.object(app_module.mail, "poll_mailbox"), patch.object(app_module, "LOGIN_FAILURE_DELAY", 0):
            with TestClient(app_module.app) as client, patch.object(app_module, "settings", protected):
                with self.assertLogs("src.app", "WARNING"):
                    for _ in range(5):
                        client.post("/anmelden", content="passwort=nein", headers=form, follow_redirects=False)
                locked = client.post("/anmelden", content="passwort=geheim-123", headers=form, follow_redirects=False)
                self.assertIn("Fehlversuche", unquote(locked.headers["location"]))
                self.assertNotIn(app_module.SESSION_COOKIE, locked.cookies)


class StatisticsAndReasonTests(unittest.TestCase):
    def test_reason_after_dismiss_feeds_statistics_and_prompt(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                path = app_module.settings.database_path
                item = _add_suggestion("Sommerschlussverkauf", "2099-07-01T10:00", sender="news@shop.example")
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/reason", json={"reason": "kein_termin"}).status_code, 409)
                client.post(f"/api/suggestions/{item['id']}/dismiss")
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/reason", json={"reason": "quatsch"}).status_code, 422)
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/reason", json={"reason": "kein_termin"}).status_code, 200)
                rejected = db.recent_rejections(path)
                self.assertIn("Sommerschlussverkauf", [r["title"] for r in rejected])
                from src import ai as ai_module
                prompt = ai_module.build_system_prompt([], rejected)
                self.assertIn('"Sommerschlussverkauf" aus der E-Mail "Betreff Sommerschlussverkauf" (shop.example)', prompt)
                page = client.get("/protokoll/statistik").text
                self.assertIn("Gründe beim Ablehnen", page)
                self.assertIn("Kein Termin", page)
                self.assertIn('href="/protokoll/statistik"', client.get("/protokoll").text)
                client.post(f"/api/suggestions/{item['id']}/undo-dismiss")
                self.assertNotIn("Sommerschlussverkauf", [r["title"] for r in db.recent_rejections(path)])
                client.post(f"/api/suggestions/{item['id']}/dismiss")


def _run_async(coroutine):
    """Eigene Loop, ohne die aktuelle Loop der Testumgebung zu verändern."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class IdleStateTests(unittest.TestCase):
    def test_combined_idle_state_prefers_problems(self):
        class Fake:
            def __init__(self, status):
                self.state = {"status": status}
        try:
            app_module._idle_watchers[:] = [Fake("active"), Fake("error"), Fake("active")]
            self.assertEqual(app_module._idle_state()["status"], "error")
            app_module._idle_watchers[:] = [Fake("active"), Fake("active")]
            self.assertEqual(app_module._idle_state()["status"], "active")
            app_module._idle_watchers[:] = []
            self.assertIsNone(app_module._idle_state())
        finally:
            app_module._idle_watchers.clear()


class NotificationAppTests(unittest.TestCase):
    def _with_channel(self, **values):
        return replace(app_module.settings, ntfy_url="https://ntfy.sh/test-thema", **values)

    def test_new_suggestions_are_announced_with_titles_or_count(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app):
                before = db.max_suggestion_id(app_module.settings.database_path)
                _add_suggestion("Laternenumzug", "2099-11-11T17:00")
                with patch.object(app_module, "settings", self._with_channel()), patch.object(app_module.notify, "send") as send:
                    _run_async(app_module._notify_new_suggestions(before))
                title, message = send.call_args.args[1:3]
                self.assertEqual(title, "1 neuer Terminvorschlag")
                self.assertIn("Laternenumzug", message)
                with patch.object(app_module, "settings", self._with_channel(notify_detail="anzahl")), patch.object(app_module.notify, "send") as send:
                    _run_async(app_module._notify_new_suggestions(before))
                self.assertNotIn("Laternenumzug", send.call_args.args[2])
                with patch.object(app_module, "settings", self._with_channel(notify_suggestions="0")), patch.object(app_module.notify, "send") as send:
                    _run_async(app_module._notify_new_suggestions(before))
                send.assert_not_called()

    def test_outage_reported_once_after_delay_and_recovery(self):
        path = app_module.settings.database_path
        error = [{"key": "ai", "label": "KI-Server", "state": "error", "text": "Nicht erreichbar"}]
        ok = [{"key": "ai", "label": "KI-Server", "state": "ok", "text": "Erreichbar"}]
        app_module._outage_since.clear()
        db.set_service_state(path, notified_outage_ai="")
        with patch.object(app_module, "settings", self._with_channel(notify_outage_minutes=60)), \
                patch.object(app_module.notify, "send") as send:
            with patch.object(app_module, "_status_summary", return_value=error):
                app_module._check_outages(now=1000.0)
                app_module._check_outages(now=1000.0 + 59 * 60)
                send.assert_not_called()
                app_module._check_outages(now=1000.0 + 61 * 60)
                app_module._check_outages(now=1000.0 + 90 * 60)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[1], "Störung: KI-Server")
            with patch.object(app_module, "_status_summary", return_value=ok):
                app_module._check_outages(now=2000.0 + 99 * 60)
                app_module._check_outages(now=2000.0 + 100 * 60)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.args[1], "Wieder in Ordnung: KI-Server")

    def test_new_version_reported_once(self):
        path = app_module.settings.database_path
        db.set_service_state(path, notified_version="")
        newer = [{"version": "99.0.0", "key": (99, 0, 0)}]
        with patch.object(app_module, "settings", self._with_channel(update_check=True)), \
                patch.object(app_module.updates, "releases", return_value=newer), \
                patch.object(app_module.notify, "send") as send:
            app_module._check_new_version()
            app_module._check_new_version()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], "Neue Version 99.0.0")

    def test_test_endpoint_and_settings_section(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self.assertEqual(client.post("/api/benachrichtigungen/test").status_code, 409)
                self.assertIn("Noch kein Kanal eingerichtet", client.get("/einstellungen").text)
                with patch.object(app_module, "settings", self._with_channel()), \
                        patch.object(app_module.notify, "send", return_value={"ntfy": "ok"}):
                    self.assertEqual(client.post("/api/benachrichtigungen/test").json(), {"results": {"ntfy": "ok"}})
                    page = client.get("/einstellungen").text
                    self.assertIn("Eingerichtete Kanäle: ntfy", page)
                    self.assertIn('id="notify-test"', page)


class AutoExportTests(unittest.TestCase):
    def _configured(self):
        return replace(app_module.settings, caldav_username="tablet@example.org", caldav_app_password="app-password",
                       ntfy_url="https://ntfy.sh/test-thema")

    def test_auto_export_only_for_clear_cases(self):
        path = app_module.settings.database_path
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                before = db.max_suggestion_id(path)
                sure = _add_suggestion("Schwimmkurs", "2099-03-01T15:00", person="Sam", confidence=0.97, sender="Verein <kurse@schwimmen.example>")
                unsure = _add_suggestion("Vielleicht Fest", "2099-03-02T15:00", person="Sam", confidence=0.8, sender="kurse@schwimmen.example")
                no_calendar = _add_suggestion("Ohne Kalender", "2099-03-03T15:00", confidence=0.99, sender="kurse@schwimmen.example")
                other = _add_suggestion("Elternabend", "2099-03-04T15:00", person="Sam", confidence=0.99, sender="schule@example.org")
                self.assertEqual(client.post("/regeln/automatisch", content="pattern=schwimmen.example&schwelle=50",
                                             headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code, 422)
                client.post("/regeln/automatisch", content="pattern=schwimmen.example&schwelle=95",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
                candidates = db.auto_export_candidates(path, before, "2026-11-07")
                self.assertEqual([c["id"] for c in candidates], [sure["id"]])
                with patch.object(app_module, "settings", self._configured()), \
                        patch.object(app_module, "_calendar_names", return_value=["Sam", "Familie"]), \
                        patch.object(app_module.caldav, "create_icloud_event", return_value="created") as create, \
                        patch.object(app_module.notify, "send") as send:
                    with self.assertLogs("src.app", "INFO"):
                        exported = _run_async(app_module._auto_export(before))
                        _run_async(app_module._notify_new_suggestions(before, exported))
                self.assertEqual([item["title"] for item in exported], ["Schwimmkurs"])
                create.assert_called_once()
                self.assertTrue(db.get_suggestion(path, sure["id"])["icloud_exported_at"])
                self.assertFalse(db.get_suggestion(path, unsure["id"])["icloud_exported_at"])
                history = [h["action"] for h in db.list_suggestion_history(path, sure["id"])]
                self.assertIn("auto_exported", history)
                title, message = send.call_args.args[1:3]
                self.assertEqual(title, "3 neue Terminvorschläge")
                self.assertIn("Automatisch eingetragen:", message)
                self.assertIn("Schwimmkurs → Sam", message)
                page = client.get("/korrekturen").text
                self.assertIn("schwimmen.example", page)
                self.assertIn("ab 95 % Sicherheit", page)
                client.post("/regeln/automatisch/loeschen", content="pattern=schwimmen.example",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(db.list_auto_senders(path), [])
                for item in (sure, unsure, no_calendar, other):
                    client.post(f"/api/suggestions/{item['id']}/dismiss")


class HealthAndStatusTests(unittest.TestCase):
    def test_health_endpoint_and_status_bar(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                response = client.get("/health")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["status"], "ok")
                self.assertEqual(body["version"], app_module.version.VERSION)
                self.assertEqual(set(body["checks"]), {"mail", "idle", "ai", "icloud"})

                page = client.get("/").text
                self.assertIn('id="service-status"', page)
                self.assertIn(f"Version {app_module.version.VERSION}", page)
                self.assertIn("health", client.get("/api/activity").json())

                finished = asyncio.Future()
                finished.set_result(None)
                with patch.dict(app_module._workers, {"Test": finished}):
                    response = client.get("/health")
                self.assertEqual(response.status_code, 503)
                self.assertIn("Test", response.json()["problems"][0])

    def test_successful_check_is_persisted(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                client.post("/api/check")
                state = db.get_service_state(app_module.settings.database_path)
                self.assertIn("mail_last_success", state)


class NewMailNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_mail_during_check_triggers_followup(self):
        calls = []

        async def fake_check(trigger):
            calls.append(trigger)
            if len(calls) == 1:
                await app_module._on_new_mail()  # kommt während des Abrufs
            return True

        with patch.object(app_module, "_perform_mail_check", side_effect=fake_check):
            with self.assertLogs("src.app", level="INFO"):
                await app_module._on_new_mail()
        self.assertEqual(calls, ["neue E-Mail", "während des Abrufs neu gemeldet"])
        self.assertFalse(app_module._mail_check_lock.locked())


def _add_change_request(target_id, action, event):
    import json as _json
    with db.connect(app_module.settings.database_path) as connection:
        return connection.execute(
            "INSERT INTO change_requests (source_key, action, event_data, related_suggestion_id, "
            "source_subject, source_sender, source_received_at, created_at) "
            "VALUES (?, ?, ?, ?, 'Folge', 'verein@example.org', '2026-11-04T10:00:00+00:00', '2026-11-04T10:00:00+00:00')",
            (f"cr-{time.time_ns()}", action, _json.dumps(event), target_id),
        ).lastrowid


class SeriesTests(unittest.TestCase):
    WEEKLY = {"frequency": "weekly", "interval": 1, "weekdays": ["TU"], "until": "2099-12-29", "count": None, "exdates": []}

    def _configured(self):
        return replace(app_module.settings, caldav_username="t@example.org", caldav_app_password="pw")

    def test_edit_series_rule_and_validation(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Training Serie", "2099-11-10T17:00")  # Dienstag
                body = {"title": item["title"], "start": item["starts_at"], "end": None,
                        "location": None, "person": None, "notes": None,
                        "recurrence": {"frequency": "weekly", "weekdays": ["TU", "TH"], "until": "15.12.2099", "exdates": ["17.11.2099"]}}
                response = client.put(f"/api/suggestions/{item['id']}", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                stored = db.get_suggestion(app_module.settings.database_path, item["id"])
                self.assertEqual(stored["recurrence"]["weekdays"], ["TU", "TH"])
                self.assertEqual(stored["recurrence"]["exdates"], ["2099-11-17"])
                page = client.get("/suggestions-fragment").text
                self.assertIn("Jeden Di. und Do. bis 15.12.2099, außer 17.11.2099", page)
                self.assertIn("nächster Termin", page)

                # Beginn auf einen Mittwoch verschieben passt nicht zur Regel
                moved = {**body, "start": "2099-11-11T17:00"}
                moved.pop("recurrence")
                response = client.put(f"/api/suggestions/{item['id']}", json=moved)
                self.assertEqual(response.status_code, 422)
                self.assertIn("Wochentage", response.json()["detail"])

                # Ohne recurrence-Feld bleibt die Serie erhalten (z. B. Ein-Klick-Export)
                with patch.object(app_module, "settings", self._configured()):
                    with patch.object(app_module.caldav, "create_icloud_event", return_value="created") as create:
                        client.post(f"/api/suggestions/{item['id']}/export", json={"calendar": "Mia"})
                self.assertEqual(create.call_args.args[6]["recurrence"]["weekdays"], ["TU", "TH"])

    def test_follow_up_cancels_single_occurrence_only(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Fußballtraining", "2099-11-10T17:00", recurrence=self.WEEKLY)
                path = app_module.settings.database_path
                with db.connect(path) as connection:
                    connection.execute(
                        "UPDATE suggestions SET icloud_exported_at = '2026-11-04', icloud_calendar = 'Mia', dismissed = 1 WHERE id = ?",
                        (item["id"],),
                    )
                bad = _add_change_request(item["id"], "cancel", {"title": "Training", "start": "2099-11-18T17:00"})
                with patch.object(app_module, "settings", self._configured()):
                    response = client.post(f"/api/change-requests/{bad}/apply")
                self.assertEqual(response.status_code, 409)  # Mittwoch ist kein Serientermin

                request_id = _add_change_request(item["id"], "cancel", {"title": "Training", "start": "2099-11-24T17:00"})
                self.assertIn("Nur Termin am", client.get("/change-requests-fragment").text)
                with (
                    patch.object(app_module, "settings", self._configured()),
                    patch.object(app_module.caldav, "resend_icloud_event", return_value="updated") as resend,
                    patch.object(app_module.caldav, "delete_icloud_event") as delete,
                ):
                    response = client.post(f"/api/change-requests/{request_id}/apply")
                self.assertEqual(response.status_code, 200, response.text)
                delete.assert_not_called()
                self.assertEqual(resend.call_args.args[6]["recurrence"]["exdates"], ["2099-11-24"])
                stored = db.get_suggestion(path, item["id"])
                self.assertIsNone(stored["cancelled_at"])
                actions = [entry["action"] for entry in db.list_suggestion_history(path, item["id"])]
                self.assertIn("occurrence_cancelled", actions)

    def test_follow_up_changing_single_occurrence_is_not_applied(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Schwimmen Serie", "2099-11-10T17:00", recurrence=self.WEEKLY)
                request_id = _add_change_request(item["id"], "update", {"title": "Schwimmen", "start": "2099-11-24T18:00"})
                fragment = client.get("/change-requests-fragment").text
                self.assertIn("nur einen einzelnen Termin der Serie", fragment)
                response = client.post(f"/api/change-requests/{request_id}/apply")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(db.get_suggestion(app_module.settings.database_path, item["id"])["starts_at"], "2099-11-10T17:00")


class DesignTests(unittest.TestCase):
    def test_all_pages_use_shared_head_theme_toggle_and_no_inline_styles(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                for url in ("/", "/archiv", "/korrekturen", "/einstellungen", "/sicherungen", "/sicherungen/kalender"):
                    page = client.get(url).text
                    self.assertIn('/static/app.css?v=', page, url)
                    self.assertIn('rel="manifest"', page, url)
                    self.assertIn('id="theme-toggle"', page, url)
                    self.assertNotIn("<style>", page, url)
                for asset, kind in (
                    ("/static/app.css", "text/css"),
                    ("/static/app.js", "javascript"),
                    ("/static/theme-init.js", "javascript"),
                    ("/static/icon-180.png", "image/png"),
                    ("/static/icon.svg", "image/svg"),
                ):
                    response = client.get(asset)
                    self.assertEqual(response.status_code, 200, asset)
                    self.assertIn(kind, response.headers["content-type"], asset)
                response = client.get("/api/version")
                self.assertEqual(response.json(), {"version": app_module.version.VERSION})
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn(f'<meta name="app-version" content="{app_module.version.VERSION}">', client.get("/").text)
                manifest = client.get("/static/manifest.webmanifest").json()
                self.assertEqual(manifest["display"], "standalone")
                for icon in manifest["icons"]:
                    self.assertEqual(client.get(icon["src"]).status_code, 200, icon["src"])


class StartPageInteractionTests(unittest.TestCase):
    def test_dismiss_undo_keeps_auto_archiving_but_archive_restore_does_not(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                path = app_module.settings.database_path
                item = _add_suggestion("Undo-Test", "2099-03-01T10:00")
                response = client.post(f"/api/suggestions/{item['id']}/dismiss")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(db.get_suggestion(path, item["id"])["dismissed"], 1)

                response = client.post(f"/api/suggestions/{item['id']}/undo-dismiss")
                self.assertEqual(response.status_code, 200)
                restored = db.get_suggestion(path, item["id"])
                self.assertEqual((restored["dismissed"], restored["keep_active"]), (0, 0))
                actions = [entry["action"] for entry in db.list_suggestion_history(path, item["id"])]
                self.assertIn("dismiss_undone", actions)

                # Übertragene Vorschläge lassen sich nicht per Rückgängig zurückholen.
                with db.connect(path) as connection:
                    connection.execute("UPDATE suggestions SET dismissed = 1, icloud_exported_at = 'x' WHERE id = ?", (item["id"],))
                self.assertEqual(client.post(f"/api/suggestions/{item['id']}/undo-dismiss").status_code, 409)
                # Bewusstes Wiederherstellen aus dem Archiv geht weiterhin und setzt keep_active.
                client.post(f"/suggestions/{item['id']}/restore")
                self.assertEqual(db.get_suggestion(path, item["id"])["keep_active"], 1)

    def test_rejected_change_request_can_be_reopened_once(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Reopen-Test", "2099-03-02T10:00")
                request_id = _add_change_request(item["id"], "update", {"title": "Neu", "start": "2099-03-02T11:00"})
                self.assertEqual(client.post(f"/api/change-requests/{request_id}/reject").status_code, 200)
                self.assertEqual(client.post(f"/api/change-requests/{request_id}/reopen").status_code, 200)
                self.assertEqual(db.get_change_request(app_module.settings.database_path, request_id)["status"], "pending")
                self.assertEqual(client.post(f"/api/change-requests/{request_id}/reopen").status_code, 409)

    def test_start_page_structure_and_activity_without_logs(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                item = _add_suggestion("Strukturtest Elternabend", "2099-04-01T19:00", person="Sam")
                page = client.get("/").text
                for element_id in (
                    "page-config", "toast-region", "suggestion-search", "selection-bar", "selection-count",
                    "select-visible", "selection-clear", "batch-dismiss", "show-details", "filter-empty",
                    "suggestions-container", "change-requests", "check-button", "check-status",
                ):
                    self.assertIn(f'id="{element_id}"', page, element_id)
                card = page[page.index(f'id="suggestion-{item["id"]}"'):]
                card = card[: card.index("</article>")]
                self.assertIn('data-person="Sam"', card)
                self.assertIn("strukturtest elternabend", card)
                self.assertIn('class="card-select"', card)
                self.assertIn('class="when-badge', card)
                self.assertIn('<details class="card-more">', card)
                activity = client.get("/api/activity?include_logs=false").json()
                self.assertEqual(activity["logs"], [])


class ArchiveAndHelpTests(unittest.TestCase):
    def test_archive_search_filter_status_and_pages(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                path = app_module.settings.database_path
                created = []
                for index in range(27):
                    item = _add_suggestion(f"Archivtest Übung {index}", f"2099-05-{index % 28 + 1:02d}T10:00", person="Ben")
                    db.dismiss_suggestion(path, item["id"])
                    created.append(item["id"])
                gone = _add_suggestion("Archivtest Vergangen Ölwechsel", "2020-01-02T10:00", person="Alex")
                app_module.archive_past_suggestions()

                page = client.get("/archiv?q=archivtest").text
                self.assertIn("28 Treffer · Seite 1 von 2", page)
                self.assertIn("Archivtest Vergangen Ölwechsel", page)  # zuletzt archiviert → zuerst
                self.assertIn('rel="next"', page)
                self.assertIn("Archivtest Übung", client.get("/archiv?q=archivtest&seite=2").text)

                umlaut = client.get("/archiv?q=ÖLWECHSEL").text  # Groß/klein auch bei Umlauten
                self.assertIn("1 Treffer", umlaut)
                auto = client.get("/archiv?q=archivtest&status=automatisch").text
                self.assertIn("1 Treffer", auto)
                self.assertIn("Automatisch archiviert", auto)
                self.assertIn("27 Treffer", client.get("/archiv?q=archivtest&kalender=Ben").text)
                self.assertIn("Wiederherstellen<span class=\"visually-hidden\">: Archivtest", page)

    def test_help_page_and_landmarks(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                help_page = client.get("/hilfe").text
                self.assertIn("So funktioniert’s", help_page)
                self.assertIn('class="help-link" href="/hilfe" aria-label="Hilfe: So funktioniert’s" aria-current="page"', help_page)
                for url in ("/", "/archiv", "/korrekturen", "/einstellungen", "/sicherungen", "/sicherungen/kalender", "/protokoll", "/hilfe"):
                    page = client.get(url).text
                    self.assertEqual(page.count('<main id="main"'), 1, url)
                    self.assertIn('href="#main">Zum Inhalt springen', page, url)
                index = client.get("/").text
                self.assertIn('class="info-toggle" aria-expanded="false"', index)
                self.assertIn('<details class="status-panel" id="status-panel"', index)
                self.assertIn('id="status-summary"', index)
                self.assertNotIn('<a href="/protokoll" class="meta">', index)


class CrossSiteRequestTests(unittest.TestCase):
    def test_rejects_write_requests_from_other_websites(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                for headers in (
                    {"Sec-Fetch-Site": "cross-site"},
                    {"Sec-Fetch-Site": "same-site"},
                    {"Origin": "https://evil.example"},
                    {"Origin": "null"},
                ):
                    response = client.post(
                        "/api/suggestions/1/export", headers=headers
                    )
                    self.assertEqual(response.status_code, 403, headers)
                    response = client.post(
                        "/suggestions/1/dismiss", headers=headers
                    )
                    self.assertEqual(response.status_code, 403, headers)

    def test_allows_requests_from_own_page_and_reads_from_anywhere(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                for headers in (
                    {"Sec-Fetch-Site": "same-origin"},
                    {"Origin": "http://testserver"},
                    {},
                ):
                    response = client.post(
                        "/suggestions/999999/dismiss", headers=headers
                    )
                    self.assertEqual(response.status_code, 404, headers)
                response = client.get(
                    "/api/activity", headers={"Sec-Fetch-Site": "cross-site"}
                )
                self.assertEqual(response.status_code, 200)


class SettingsPageTests(unittest.TestCase):
    def tearDown(self):
        db.reset_app_settings(app_module.settings.database_path)
        app_module._reload_settings()

    def test_settings_can_be_changed_and_reset(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                page = client.get("/einstellungen")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Abrufintervall", page.text)
                self.assertNotIn("test-password", page.text)

                response = client.post(
                    "/einstellungen",
                    content="imap_poll_seconds=10&default_event_minutes=90",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(app_module.settings.imap_poll_seconds, 600)
                self.assertEqual(app_module.settings.default_event_minutes, 90)
                self.assertIn("angepasst", client.get("/einstellungen").text)

                response = client.post(
                    "/einstellungen",
                    content="imap_poll_seconds=0&default_event_minutes=30",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(response.status_code, 422)
                self.assertIn("Mindestens 1 Minuten", response.text)
                self.assertEqual(app_module.settings.default_event_minutes, 90)

                client.post("/einstellungen/zuruecksetzen")
                self.assertEqual(app_module.settings.imap_poll_seconds, 300)
                self.assertEqual(app_module.settings.default_event_minutes, 60)

    def test_settings_survive_restart_and_reject_other_websites(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                response = client.post(
                    "/einstellungen",
                    content="default_event_minutes=45",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Sec-Fetch-Site": "cross-site",
                    },
                )
                self.assertEqual(response.status_code, 403)
                client.post(
                    "/einstellungen",
                    content="default_event_minutes=45",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            app_module.settings = app_module._env_settings
            with TestClient(app_module.app):
                self.assertEqual(app_module.settings.default_event_minutes, 45)

    def test_settings_page_ignores_backup_fields(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                page = client.get("/einstellungen").text
                self.assertNotIn('name="backup_hour"', page)
                client.post(
                    "/einstellungen",
                    content="backup_hour=7",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                self.assertEqual(app_module.settings.backup_hour, 3)


class BackupPageTests(unittest.TestCase):
    def tearDown(self):
        db.reset_app_settings(app_module.settings.database_path)
        app_module._reload_settings()

    def _form(self, client, url, body=""):
        return client.post(
            url,
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_create_download_delete_and_settings(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                response = self._form(client, "/sicherungen/erstellen")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Sicherung erstellt", response.text)
                name = app_module.backup.list_backups(
                    app_module.settings.backup_dir
                )[0]["name"]

                download = client.get(f"/sicherungen/{name}/download")
                self.assertEqual(download.status_code, 200)
                self.assertTrue(download.content.startswith(b"SQLite format 3"))

                for bad in ("..%2F..%2Fetc%2Fpasswd", "data.sqlite3", "x"):
                    self.assertEqual(
                        client.get(f"/sicherungen/{bad}/download").status_code, 404
                    )

                response = self._form(client, "/sicherungen/einstellungen", "backup_hour=5&backup_keep_days=30")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(app_module.settings.backup_hour, 5)
                self.assertEqual(app_module.settings.backup_keep_days, 30)

                response = self._form(client, "/sicherungen/einstellungen", "backup_hour=25")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(app_module.settings.backup_hour, 5)

                before = len(app_module.backup.list_backups(app_module.settings.backup_dir))
                self._form(client, f"/sicherungen/{name}/loeschen")
                after = len(app_module.backup.list_backups(app_module.settings.backup_dir))
                self.assertEqual(after, before - 1)

    def test_restore_brings_back_old_state_and_keeps_safety_copy(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self._form(client, "/einstellungen", "default_event_minutes=45")
                with self.assertLogs("src.backup", level="INFO"):
                    old = app_module.backup.create_backup(
                        app_module.settings.database_path,
                        app_module.settings.backup_dir,
                        14,
                        now=datetime(2020, 1, 1, tzinfo=timezone.utc),
                    )
                self._form(client, "/einstellungen", "default_event_minutes=90")
                self.assertEqual(app_module.settings.default_event_minutes, 90)
                count = len(app_module.backup.list_backups(app_module.settings.backup_dir))
                version = db.get_suggestion_version(app_module.settings.database_path)

                response = self._form(client, f"/sicherungen/{old.name}/wiederherstellen")
                self.assertEqual(response.status_code, 200)
                self.assertIn("wiederhergestellt", response.text)
                self.assertEqual(app_module.settings.default_event_minutes, 45)
                self.assertEqual(
                    len(app_module.backup.list_backups(app_module.settings.backup_dir)),
                    count + 1,
                )
                self.assertGreater(
                    db.get_suggestion_version(app_module.settings.database_path), version
                )

    def test_restore_refused_while_mail_check_runs(self):
        with patch.object(app_module.mail, "poll_mailbox"):
            with TestClient(app_module.app) as client:
                self._form(client, "/sicherungen/erstellen")
                name = app_module.backup.list_backups(
                    app_module.settings.backup_dir
                )[0]["name"]
                with patch.object(app_module._mail_check_lock, "locked", return_value=True):
                    response = self._form(client, f"/sicherungen/{name}/wiederherstellen")
                self.assertEqual(response.status_code, 409)


class MailWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_check_waits_for_active_check_and_defers_next_run(self):
        lock = app_module._mail_check_lock
        await lock.acquire()
        try:
            with patch.object(
                app_module,
                "_perform_mail_check",
                new_callable=AsyncMock,
            ) as perform_check:
                scheduled = asyncio.create_task(
                    app_module._run_scheduled_mail_check()
                )
                await asyncio.sleep(0)
                self.assertFalse(scheduled.done())
                perform_check.assert_not_awaited()

                lock.release()
                await scheduled

                perform_check.assert_not_awaited()
        finally:
            if lock.locked():
                lock.release()


if __name__ == "__main__":
    unittest.main()

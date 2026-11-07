import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src import health

NOW = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(
    imap_poll_seconds=300, calendar_backup_minutes=60, ai_model="kalender-assistent",
    ai_base_url="http://ai/v1", ai_api_key="", mail_configured=True,
)


def items(**overrides):
    args = dict(
        service_state={}, check_state={"status": "idle"}, ai_state=None, idle_state=None,
        last_calendar_snapshot=None, caldav_configured=True, now=NOW,
    )
    args.update(overrides)
    return {item["key"]: item for item in health.status_items(SETTINGS, **args)}


def ago(**delta):
    return (NOW - timedelta(**delta)).isoformat()


class StatusItemTests(unittest.TestCase):
    def test_mail_states(self):
        self.assertEqual(items(service_state={"mail_last_success": ago(minutes=3)})["mail"]["state"], "ok")
        self.assertIn("vor 3 Min.", items(service_state={"mail_last_success": ago(minutes=3)})["mail"]["text"])
        self.assertEqual(items(service_state={"mail_last_success": ago(hours=2)})["mail"]["state"], "warn")
        failed = items(service_state={"mail_last_success": ago(hours=1), "mail_last_error": ago(minutes=1)})
        self.assertEqual(failed["mail"]["state"], "error")
        self.assertEqual(items(check_state={"status": "running"})["mail"]["text"], "Abruf läuft …")

    def test_mail_not_configured_after_skipped_setup(self):
        with patch.object(health, "_age", side_effect=AssertionError("kein Abruf erwartet")):
            result = {item["key"]: item for item in health.status_items(
                SimpleNamespace(**{**vars(SETTINGS), "mail_configured": False}),
                service_state={}, check_state={"status": "idle"}, ai_state=None, idle_state=None,
                last_calendar_snapshot=None, caldav_configured=False, now=NOW,
            )}
        self.assertEqual((result["mail"]["state"], result["mail"]["text"]), ("off", "Nicht eingerichtet"))
        self.assertEqual(result["idle"]["state"], "off")

    def test_idle_ai_and_icloud_states(self):
        self.assertEqual(items(idle_state={"status": "active"})["idle"]["state"], "ok")
        self.assertEqual(items(idle_state={"status": "unsupported"})["idle"]["state"], "off")
        self.assertIn("alle 5 Min.", items(idle_state={"status": "error"})["idle"]["text"])
        self.assertEqual(items(ai_state={"reachable": False, "error": "ConnectError"})["ai"]["state"], "error")
        self.assertEqual(items(ai_state={"reachable": True, "model_found": False})["ai"]["state"], "warn")
        self.assertIn("vorhanden", items(ai_state={"reachable": True, "model_found": True})["ai"]["text"])
        self.assertEqual(items(caldav_configured=False)["icloud"]["state"], "off")
        fresh = {"taken_at": ago(minutes=20), "status": "ok"}
        self.assertEqual(items(last_calendar_snapshot=fresh)["icloud"]["state"], "ok")
        stale = {"taken_at": ago(hours=5), "status": "ok"}
        self.assertEqual(items(last_calendar_snapshot=stale)["icloud"]["state"], "warn")


class AiCheckTests(unittest.TestCase):
    def test_model_found_and_unreachable(self):
        request = httpx.Request("GET", "http://ai/v1/models")
        ok = httpx.Response(200, json={"data": [{"id": "kalender-assistent"}]}, request=request)
        with patch.object(health.httpx, "get", return_value=ok):
            result = health.check_ai_server(SETTINGS)
        self.assertEqual((result["reachable"], result["model_found"]), (True, True))
        with patch.object(health.httpx, "get", side_effect=httpx.ConnectError("weg", request=request)):
            result = health.check_ai_server(SETTINGS)
        self.assertEqual((result["reachable"], result["error"]), (False, "ConnectError"))


if __name__ == "__main__":
    unittest.main()


class PollIntervalTests(unittest.TestCase):
    def test_idle_connection_uses_longer_interval(self):
        settings = SimpleNamespace(imap_poll_seconds=300, imap_idle_poll_seconds=1800)
        self.assertEqual(health.effective_poll_seconds(settings, {"status": "active"}), 1800)
        for state in (None, {"status": "error"}, {"status": "connecting"}, {"status": "unsupported"}):
            self.assertEqual(health.effective_poll_seconds(settings, state), 300)

    def test_status_text_and_staleness_follow_interval(self):
        settings = SimpleNamespace(**{**vars(SETTINGS), "imap_idle_poll_seconds": 1800})
        last = (NOW - timedelta(minutes=40)).isoformat()
        active = health.status_items(
            settings, service_state={"mail_last_success": last}, check_state={"status": "idle"},
            ai_state=None, idle_state={"status": "active"}, last_calendar_snapshot=None,
            caldav_configured=True, now=NOW,
        )
        by_key = {item["key"]: item for item in active}
        self.assertEqual(by_key["mail"]["state"], "ok")
        self.assertIn("Abruf alle 30 Min.", by_key["idle"]["text"])
        broken = health.status_items(
            settings, service_state={"mail_last_success": last}, check_state={"status": "idle"},
            ai_state=None, idle_state={"status": "error"}, last_calendar_snapshot=None,
            caldav_configured=True, now=NOW,
        )
        by_key = {item["key"]: item for item in broken}
        self.assertEqual(by_key["mail"]["state"], "warn")
        self.assertIn("Abruf alle 5 Min.", by_key["idle"]["text"])

import unittest

from src import settings_store
from src.config import Settings


def _settings(**changes):
    base = dict(
        imap_host="imap.example.org", imap_port=993, imap_username="u",
        imap_password="p", imap_folder="INBOX", imap_poll_seconds=300,
        imap_initial_lookback_days=14, imap_max_message_bytes=10 * 1024 * 1024,
        ai_base_url="http://ai/v1", ai_api_key="", ai_model="m",
        ai_timeout_seconds=1800, caldav_url="https://caldav", caldav_username="",
        caldav_app_password="", caldav_timeout_seconds=30,
        caldav_timezone="Europe/Berlin", database_path=":memory:", log_level="INFO",
    )
    base.update(changes)
    return Settings(**base)


class SettingsStoreTests(unittest.TestCase):
    def test_parses_values_with_units_and_limits(self):
        values, errors = settings_store.parse_form(
            {
                "imap_poll_seconds": "10",
                "imap_max_message_bytes": "20",
                "default_event_minutes": "90",
                "caldav_timezone": "Europe/Vienna",
                "ai_model": " qwen3-vl ",
            }
        )
        self.assertEqual(errors, {})
        self.assertEqual(values["imap_poll_seconds"], 600)
        self.assertEqual(values["imap_max_message_bytes"], 20 * 1024 * 1024)
        self.assertEqual(values["default_event_minutes"], 90)
        self.assertEqual(values["caldav_timezone"], "Europe/Vienna")
        self.assertEqual(values["ai_model"], "qwen3-vl")

    def test_reports_invalid_values(self):
        _, errors = settings_store.parse_form(
            {
                "imap_poll_seconds": "0",
                "backup_hour": "24",
                "default_event_minutes": "abc",
                "caldav_timezone": "Mars/Olympus",
                "ai_model": "",
                "imap_folder": "INBOX\r\nA1 DELETE",
            }
        )
        self.assertEqual(
            set(errors),
            {
                "imap_poll_seconds", "backup_hour", "default_event_minutes",
                "caldav_timezone", "ai_model", "imap_folder",
            },
        )

    def test_cannot_change_server_addresses_or_credentials(self):
        values, errors = settings_store.parse_form(
            {"ai_base_url": "http://evil.example/v1", "imap_password": "x"}
        )
        self.assertEqual((values, errors), ({}, {}))
        self.assertNotIn("ai_base_url", settings_store.PARAMETERS_BY_KEY)

    def test_applies_valid_overrides_and_ignores_broken_ones(self):
        with self.assertLogs("src.settings_store", level="WARNING"):
            result = settings_store.apply_overrides(
                _settings(),
                {
                    "imap_poll_seconds": "600",
                    "backup_hour": "99",
                    "caldav_timezone": "Nowhere/Land",
                    "ai_base_url": "http://evil.example/v1",
                },
            )
        self.assertEqual(result.imap_poll_seconds, 600)
        self.assertEqual(result.backup_hour, 3)
        self.assertEqual(result.caldav_timezone, "Europe/Berlin")
        self.assertEqual(result.ai_base_url, "http://ai/v1")

    def test_display_value_uses_units(self):
        settings = _settings(imap_poll_seconds=600)
        parameter = settings_store.PARAMETERS_BY_KEY["imap_poll_seconds"]
        self.assertEqual(settings_store.display_value(parameter, settings), "10")


if __name__ == "__main__":
    unittest.main()

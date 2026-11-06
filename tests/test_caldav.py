import unittest
from unittest.mock import patch

import httpx

from src import caldav


def _multistatus(inner: str) -> bytes:
    return f"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
      {inner}
    </d:multistatus>""".encode()


def _response(href: str, properties: str) -> str:
    return f"""<d:response>
      <d:href>{href}</d:href>
      <d:propstat>
        <d:prop>{properties}</d:prop>
        <d:status>HTTP/1.1 200 OK</d:status>
      </d:propstat>
    </d:response>"""


class CalDAVDiscoveryTests(unittest.TestCase):
    @patch("src.caldav.httpx.Client")
    def test_discovers_calendar_names_and_reported_write_access(self, client_factory):
        responses = [
            _multistatus(
                _response(
                    "/",
                    "<d:current-user-principal><d:href>/principal/</d:href>"
                    "</d:current-user-principal>",
                )
            ),
            _multistatus(
                _response(
                    "/principal/",
                    "<c:calendar-home-set><d:href>/home/</d:href>"
                    "</c:calendar-home-set>",
                )
            ),
            _multistatus(
                _response(
                    "/home/family/",
                    "<d:resourcetype><d:collection/><c:calendar/>"
                    "</d:resourcetype><d:displayname>Familie</d:displayname>"
                    '<a:calendar-color xmlns:a="http://apple.com/ns/ical/">#FF2968FF</a:calendar-color>'
                    "<d:current-user-privilege-set><d:privilege><d:read/>"
                    "<d:write/></d:privilege></d:current-user-privilege-set>",
                )
                + _response(
                    "/home/child/",
                    "<d:resourcetype><d:collection/><c:calendar/>"
                    "</d:resourcetype><d:displayname>Mia</d:displayname>",
                )
            ),
        ]
        client = client_factory.return_value.__enter__.return_value
        client.request.side_effect = [
            httpx.Response(
                207,
                content=body,
                request=httpx.Request("PROPFIND", url),
            )
            for body, url in zip(
                responses,
                (
                    "https://caldav.icloud.com/",
                    "https://caldav.icloud.com/principal/",
                    "https://caldav.icloud.com/home/",
                ),
            )
        ]

        calendars = caldav.discover_icloud_calendars(
            "https://caldav.icloud.com", "tablet@example.org", "app-password", 30
        )

        self.assertEqual(
            calendars,
            [
                {
                    "name": "Familie",
                    "write_access": True,
                    "calendar_url": "https://caldav.icloud.com/home/family/",
                    "color": "#ff2968",
                },
                {
                    "name": "Mia",
                    "write_access": None,
                    "calendar_url": "https://caldav.icloud.com/home/child/",
                    "color": None,
                },
            ],
        )
        self.assertEqual(client.request.call_count, 3)
        self.assertTrue(
            all(call.kwargs["headers"]["Depth"] in {"0", "1"} for call in client.request.call_args_list)
        )

    def test_builds_escaped_icalendar_in_local_timezone(self):
        content = caldav._build_icalendar(
            {
                "starts_at": "2026-11-04T17:00",
                "ends_at": "2026-11-04T18:30",
                "title": "Turnier, Mia;\nFinale",
                "location": "Sporthalle",
                "notes": "Treffpunkt: Eingang",
            },
            "test-event@local.invalid",
            "Europe/Berlin",
        ).decode()

        self.assertIn("BEGIN:VTIMEZONE", content)
        self.assertIn("TZID:Europe/Berlin", content)
        self.assertIn("TZOFFSETTO:+0100", content)
        self.assertIn("DTSTART;TZID=Europe/Berlin:20261104T170000", content)
        self.assertIn("DTEND;TZID=Europe/Berlin:20261104T183000", content)
        self.assertIn(r"SUMMARY:Turnier\, Mia\;\nFinale", content)
        self.assertIn("LOCATION:Sporthalle", content)
        self.assertIn("DESCRIPTION:Treffpunkt: Eingang", content)
        self.assertTrue(content.endswith("\r\n"))

    def test_adds_one_hour_end_for_timed_event_without_end(self):
        content = caldav._build_icalendar(
            {"starts_at": "2026-11-13T17:00", "ends_at": None, "title": "Martinszug"},
            "test-event@local.invalid",
            "Europe/Berlin",
        ).decode()

        self.assertIn("DTSTART;TZID=Europe/Berlin:20261113T170000", content)
        self.assertIn("DTEND;TZID=Europe/Berlin:20261113T180000", content)

    def test_default_duration_is_configurable(self):
        content = caldav._build_icalendar(
            {"starts_at": "2026-11-13T17:00", "title": "Martinszug"},
            "test-event@local.invalid",
            "Europe/Berlin",
            default_event_minutes=90,
        ).decode()

        self.assertIn("DTEND;TZID=Europe/Berlin:20261113T183000", content)

    def test_adds_reminders_for_timed_and_all_day_events(self):
        timed = caldav._build_icalendar(
            {"starts_at": "2026-11-13T17:00", "title": "Martinszug"},
            "uid@local.invalid", "Europe/Berlin", reminder_minutes=60, allday_reminder_hour=18,
        ).decode()
        self.assertIn("BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Martinszug\r\nTRIGGER:-PT60M\r\nEND:VALARM\r\nEND:VEVENT", timed)
        all_day = caldav._build_icalendar(
            {"starts_at": "2027-03-02", "title": "Klassenfahrt"},
            "uid@local.invalid", "Europe/Berlin", reminder_minutes=60, allday_reminder_hour=18,
        ).decode()
        self.assertIn("TRIGGER:-PT6H", all_day)
        none = caldav._build_icalendar(
            {"starts_at": "2027-03-02", "title": "Klassenfahrt"},
            "uid@local.invalid", "Europe/Berlin", reminder_minutes=0, allday_reminder_hour=24,
        ).decode()
        self.assertNotIn("VALARM", none)
        self.assertNotIn(
            "VALARM",
            caldav._build_icalendar({"starts_at": "2026-11-13T17:00", "title": "x"}, "u", "Europe/Berlin").decode(),
        )

    def test_default_end_stays_valid_across_dst_change(self):
        content = caldav._build_icalendar(
            {"starts_at": "2026-03-29T01:30", "title": "Nachts"},
            "test-event@local.invalid",
            "Europe/Berlin",
        ).decode()

        self.assertIn("DTEND;TZID=Europe/Berlin:20260329T033000", content)

    def test_rejects_nonexistent_local_time_during_dst_transition(self):
        with self.assertRaisesRegex(ValueError, "Zeitumstellung"):
            caldav._build_icalendar(
                {
                    "starts_at": "2026-03-29T02:30",
                    "title": "Zeitumstellung",
                },
                "test-event@local.invalid",
                "Europe/Berlin",
            )

    def test_rejects_ambiguous_local_time_during_dst_transition(self):
        with self.assertRaisesRegex(ValueError, "doppeldeutig"):
            caldav._build_icalendar(
                {
                    "starts_at": "2026-10-25T02:30",
                    "title": "Zeitumstellung",
                },
                "test-event@local.invalid",
                "Europe/Berlin",
            )

    @patch("src.caldav.discover_icloud_calendars")
    @patch("src.caldav.httpx.Client")
    def test_creates_event_with_conditional_put(self, client_factory, discover):
        discover.return_value = [
            {
                "name": "Familie",
                "write_access": True,
                "calendar_url": "https://caldav.icloud.com/home/family/",
            }
        ]
        client = client_factory.return_value.__enter__.return_value
        request = httpx.Request(
            "PUT",
            "https://caldav.icloud.com/home/family/event.ics",
        )
        client.put.return_value = httpx.Response(201, request=request)
        event = {
            "id": 17,
            "title": "Gemeinsamer Termin",
            "starts_at": "2026-11-04T17:00",
            "ends_at": "2026-11-04T18:00",
            "location": None,
            "notes": None,
        }

        result = caldav.create_icloud_event(
            "https://caldav.icloud.com",
            "tablet@example.org",
            "app-password",
            30,
            "Europe/Berlin",
            "Familie",
            event,
        )

        self.assertEqual(result, "created")
        discover.assert_called_once()
        self.assertEqual(client.put.call_args.kwargs["headers"]["If-None-Match"], "*")
        self.assertIn(
            "UID:family-calendar-assistant-17@local.invalid",
            client.put.call_args.kwargs["content"].decode(),
        )

    @patch("src.caldav.discover_icloud_calendars")
    @patch("src.caldav.httpx.Client")
    def test_resends_event_to_same_icloud_resource_without_create_guard(
        self, client_factory, discover
    ):
        discover.return_value = [
            {
                "name": "Familie",
                "write_access": True,
                "calendar_url": "https://caldav.icloud.com/home/family/",
            }
        ]
        client = client_factory.return_value.__enter__.return_value
        client.put.return_value = httpx.Response(
            204,
            request=httpx.Request(
                "PUT", "https://caldav.icloud.com/home/family/event.ics"
            ),
        )

        result = caldav.resend_icloud_event(
            "https://caldav.icloud.com",
            "tablet@example.org",
            "app-password",
            30,
            "Europe/Berlin",
            "Familie",
            {
                "id": 17,
                "title": "Korrigierter Termin",
                "starts_at": "2026-11-04T17:00",
                "ends_at": "2026-11-04T18:00",
            },
        )

        self.assertEqual(result, "updated")
        self.assertNotIn("If-None-Match", client.put.call_args.kwargs["headers"])
        self.assertTrue(
            client.put.call_args.args[0].endswith(
                f"{caldav.sha256(caldav.event_uid(17).encode()).hexdigest()}.ics"
            )
        )

    @patch("src.caldav.discover_icloud_calendars")
    @patch("src.caldav.httpx.Client")
    def test_deletes_only_the_matching_icloud_resource_with_write_permission(
        self, client_factory, discover
    ):
        discover.return_value = [
            {
                "name": "Familie",
                "write_access": True,
                "calendar_url": "https://caldav.icloud.com/home/family/",
            }
        ]
        client = client_factory.return_value.__enter__.return_value
        client.delete.return_value = httpx.Response(
            204,
            request=httpx.Request(
                "DELETE", "https://caldav.icloud.com/home/family/event.ics"
            ),
        )

        result = caldav.delete_icloud_event(
            "https://caldav.icloud.com",
            "tablet@example.org",
            "app-password",
            30,
            "Familie",
            {"id": 17},
        )

        self.assertEqual(result, "deleted")
        requested_url = client.delete.call_args.args[0]
        self.assertTrue(
            requested_url.endswith(
                f"{caldav.sha256(caldav.event_uid(17).encode()).hexdigest()}.ics"
            )
        )

    @patch("src.caldav.discover_icloud_calendars")
    def test_refuses_calendar_without_confirmed_write_permission(self, discover):
        discover.return_value = [
            {
                "name": "Familie",
                "write_access": None,
                "calendar_url": "https://caldav.icloud.com/home/family/",
            }
        ]

        with self.assertRaisesRegex(ValueError, "keine bestätigte Schreibberechtigung"):
            caldav.create_icloud_event(
                "https://caldav.icloud.com",
                "tablet@example.org",
                "app-password",
                30,
                "Europe/Berlin",
                "Familie",
                {"id": 17, "title": "Termin", "starts_at": "2026-11-04"},
            )


if __name__ == "__main__":
    unittest.main()


class AlarmTriggerTests(unittest.TestCase):
    def test_own_reminder_overrides_default(self):
        t = caldav.alarm_trigger
        self.assertEqual(t(None, False, 60, 18), "-PT60M")
        self.assertIsNone(t(None, False, 0, 18))
        self.assertEqual(t(None, True, 60, 18), "-PT6H")
        self.assertIsNone(t(None, True, 60, 24))
        self.assertIsNone(t("none", False, 60, 18))
        self.assertEqual(t("15", False, 60, 18), "-PT15M")
        self.assertEqual(t("0", False, 60, 18), "PT0M")
        self.assertEqual(t("-480", True, 60, 18), "PT480M")
        self.assertEqual(t("kaputt", False, 60, 18), "-PT60M")

    def test_icalendar_uses_suggestion_reminder(self):
        content = caldav._build_icalendar(
            {"starts_at": "2026-11-13", "title": "Ausflug", "reminder": "-480"},
            "test-event@local.invalid", "Europe/Berlin", reminder_minutes=60, allday_reminder_hour=18,
        ).decode()
        self.assertIn("TRIGGER:PT480M", content)
        content = caldav._build_icalendar(
            {"starts_at": "2026-11-13T17:00", "title": "Ohne", "reminder": "none"},
            "test-event@local.invalid", "Europe/Berlin", reminder_minutes=60,
        ).decode()
        self.assertNotIn("VALARM", content)

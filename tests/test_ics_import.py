import unittest

from src import ics_import

OUTLOOK = """BEGIN:VCALENDAR
METHOD:REQUEST
BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
END:VTIMEZONE
BEGIN:VEVENT
UID:abc-1
SUMMARY:Elterngespr\\u00e4ch Klasse 4b
DTSTART;TZID=W. Europe Standard Time:20261120T163000
DTEND;TZID=W. Europe Standard Time:20261120T170000
LOCATION:Grundschule\\, Raum 12
DESCRIPTION:Bitte Zeugnis mitbringen.\\nDanke
BEGIN:VALARM
TRIGGER:-PT15M
DESCRIPTION:Erinnerung
END:VALARM
END:VEVENT
END:VCALENDAR
""".replace("\\u00e4", "ä")


class ParseInvitationTests(unittest.TestCase):
    def test_outlook_invitation_with_windows_timezone(self):
        [event] = ics_import.parse_invitation(OUTLOOK, "Europe/Berlin", "einladung.ics")
        self.assertEqual(event["title"], "Elterngespräch Klasse 4b")
        self.assertEqual((event["start"], event["end"]), ("2026-11-20T16:30", "2026-11-20T17:00"))
        self.assertEqual(event["location"], "Grundschule, Raum 12")
        self.assertEqual(event["notes"], "Bitte Zeugnis mitbringen.\nDanke")
        self.assertEqual((event["action"], event["confidence"], event["quote_source"]), ("new", 1.0, "ics"))
        self.assertEqual(event["source_part"], "einladung.ics")
        self.assertIsNone(event["recurrence"])

    def test_utc_time_is_converted_and_allday_end_is_inclusive(self):
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:u1\nSUMMARY:Zug nach Köln\n"
            "DTSTART:20260701T060000Z\nDURATION:PT2H\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:u2\nSUMMARY:Camping\nDTSTART;VALUE=DATE:20260801\n"
            "DTEND;VALUE=DATE:20260804\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:u3\nSUMMARY:Feiertag\nDTSTART;VALUE=DATE:20261225\n"
            "DTEND;VALUE=DATE:20261226\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        train, camping, holiday = ics_import.parse_invitation(text, "Europe/Berlin")
        self.assertEqual((train["start"], train["end"]), ("2026-07-01T08:00", "2026-07-01T10:00"))
        self.assertEqual((camping["start"], camping["end"]), ("2026-08-01", "2026-08-03"))
        self.assertEqual((holiday["start"], holiday["end"]), ("2026-12-25", None))
        self.assertEqual(train["source_part"], "Kalendereinladung")

    def test_recurrence_supported_and_unsupported(self):
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:r1\nSUMMARY:Training\n"
            "DTSTART;TZID=Europe/Berlin:20261103T170000\nRRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20270330T000000Z\n"
            "EXDATE;TZID=Europe/Berlin:20261222T170000\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:r1\nRECURRENCE-ID;TZID=Europe/Berlin:20261110T170000\nSUMMARY:Training (verlegt)\n"
            "DTSTART;TZID=Europe/Berlin:20261111T170000\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:r2\nSUMMARY:Stammtisch\nDTSTART:20261105T190000\n"
            "RRULE:FREQ=MONTHLY;BYDAY=1TH\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        training, stammtisch = ics_import.parse_invitation(text, "Europe/Berlin")
        self.assertEqual(training["recurrence"]["frequency"], "weekly")
        self.assertEqual(training["recurrence"]["until"], "2027-03-30")
        self.assertEqual(training["recurrence"]["exdates"], ["2026-12-22"])
        self.assertIsNone(stammtisch["recurrence"])
        self.assertEqual(stammtisch["confidence"], 0.6)
        self.assertIn("Bitte von Hand prüfen", stammtisch["notes"])

    def test_cancellation(self):
        text = (
            "BEGIN:VCALENDAR\nMETHOD:CANCEL\nBEGIN:VEVENT\nUID:c1\nSUMMARY:Elternabend\n"
            "DTSTART:20261120T193000\nSTATUS:CANCELLED\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        [event] = ics_import.parse_invitation(text, "Europe/Berlin")
        self.assertEqual((event["action"], event["start"]), ("cancel", "2026-11-20T19:30"))

    def test_drop_ai_duplicates(self):
        ics = ics_import.parse_invitation(OUTLOOK, "Europe/Berlin")
        ai_events = [
            {"title": "Gespräch", "start": "2026-11-20T16:30"},
            {"title": "Elterngespräch 4b", "start": "2026-11-20"},
            {"title": "Schulfest", "start": "2026-11-20T10:00"},
            {"title": "Elterngespräch", "start": "2026-12-01T16:30"},
        ]
        kept, dropped = ics_import.drop_duplicates_of(ai_events, ics)
        self.assertEqual(dropped, 2)
        self.assertEqual([e["title"] for e in kept], ["Schulfest", "Elterngespräch"])

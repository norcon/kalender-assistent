import unittest
from datetime import date, datetime, timezone

from src import formatting

TZ = "Europe/Berlin"


class FormattingTests(unittest.TestCase):
    def test_event_time_in_german(self):
        self.assertEqual(
            formatting.event_time("2026-11-13T17:00", TZ), "Fr., 13. Nov. 2026, 17:00 Uhr"
        )
        self.assertEqual(formatting.event_time("2026-03-01", TZ), "So., 1. März 2026")
        # Zeit mit Offset wird in lokale Zeit umgerechnet.
        self.assertEqual(
            formatting.event_time("2026-11-13T16:00:00+00:00", TZ),
            "Fr., 13. Nov. 2026, 17:00 Uhr",
        )
        self.assertEqual(formatting.event_time("kaputt", TZ), "kaputt")

    def test_event_range(self):
        self.assertEqual(
            formatting.event_range("2026-11-13T17:00", "2026-11-13T18:30", TZ),
            "Fr., 13. Nov. 2026, 17:00 Uhr – 18:30 Uhr",
        )
        self.assertEqual(
            formatting.event_range("2027-05-05", "2027-05-10", TZ),
            "Mi., 5. Mai 2027 – Mo., 10. Mai 2027",
        )

    def test_timestamp_and_relative_time(self):
        self.assertEqual(
            formatting.timestamp("2026-11-04T09:44:43+00:00", TZ), "Mi., 4. Nov. 2026, 10:44"
        )
        now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(formatting.relative_time("2026-11-04T11:58:00+00:00", now), "vor 2 Min.")
        self.assertEqual(formatting.relative_time("2026-11-04T09:00:00+00:00", now), "vor 3 Std.")
        self.assertEqual(formatting.relative_time("2026-11-03T09:00:00+00:00", now), "gestern")
        self.assertEqual(formatting.relative_time("2026-10-25T12:00:00+00:00", now), "vor 10 Tagen")

    def test_relative_day(self):
        today = date(2026, 11, 4)
        cases = {0: "heute", 1: "morgen", 2: "übermorgen", -1: "gestern", -5: "vor 5 Tagen",
                 3: "in 3 Tagen", 13: "in 13 Tagen", 14: "in 2 Wochen", 59: "in 8 Wochen",
                 61: "in 2 Monaten", 30 * 12: "in 12 Monaten"}
        from datetime import timedelta
        for delta, expected in cases.items():
            self.assertEqual(formatting.relative_day(today + timedelta(days=delta), today), expected, delta)
        self.assertEqual(formatting.relative_day(None, today), "")

    def test_history_labels(self):
        self.assertEqual(formatting.history_label("email_change_rejected"), "Änderung aus Folge-Mail abgelehnt")
        self.assertEqual(formatting.history_label("something_new"), "something new")

    def test_groups_by_period(self):
        today = date(2026, 11, 4)  # Mittwoch
        items = [
            {"id": 1, "starts_at": "2026-11-03T10:00"},
            {"id": 2, "starts_at": "2026-11-04"},
            {"id": 3, "starts_at": "2026-11-05T08:00"},
            {"id": 4, "starts_at": "2026-11-08"},
            {"id": 5, "starts_at": "2026-11-09"},
            {"id": 6, "starts_at": "2026-11-15T23:00"},
            {"id": 7, "starts_at": "2026-11-16"},
            {"id": 8, "starts_at": "unklar"},
        ]
        groups = formatting.group_suggestions(items, today, TZ)
        self.assertEqual(
            [(label, [item["id"] for item in group]) for label, group in groups],
            [
                ("Vergangen", [1]), ("Heute", [2]), ("Morgen", [3]),
                ("Diese Woche", [4]), ("Nächste Woche", [5, 6]),
                ("Später", [7]), ("Ohne gültiges Datum", [8]),
            ],
        )

    def test_event_has_ended(self):
        now = datetime(2026, 11, 13, 17, 59)
        ended = lambda **item: formatting.event_has_ended(item, now, TZ, 60)
        self.assertFalse(ended(starts_at="2026-11-13T17:00"))
        self.assertTrue(ended(starts_at="2026-11-13T16:59"))
        self.assertTrue(ended(starts_at="2026-11-13T10:00", ends_at="2026-11-13T17:30"))
        self.assertFalse(ended(starts_at="2026-11-13"))
        self.assertTrue(ended(starts_at="2026-11-12"))
        self.assertFalse(ended(starts_at="2026-11-10", ends_at="2026-11-13"))
        self.assertFalse(ended(starts_at="unklar"))


if __name__ == "__main__":
    unittest.main()


class ReminderAndSizeTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(formatting.reminder_label(1440, False), "1 Tag vorher")
        self.assertEqual(formatting.reminder_label(90, False), "90 Min. vorher")
        self.assertEqual(formatting.reminder_label(-480, True), "Am Tag um 8 Uhr")
        self.assertEqual(formatting.reminder_label(360, True), "Am Vortag um 18 Uhr")
        self.assertEqual(formatting.reminder_label(9000, True), "1 Woche vorher um 18 Uhr")

    def test_options_mark_current_and_keep_unknown_value(self):
        options = formatting.reminder_options(None, False, 60, 18)
        self.assertEqual(options[0], ("", "Standard (1 Std. vorher)", True))
        self.assertEqual(options[1], ("none", "Keine", False))
        self.assertIn(("15", "15 Min. vorher", False), options)
        allday = formatting.reminder_options("-480", True, 60, 24)
        self.assertEqual(allday[0][1], "Standard (keine)")
        self.assertIn(("-480", "Am Tag um 8 Uhr", True), allday)
        custom = formatting.reminder_options("45", False, 0, 18)
        self.assertEqual(custom[0][1], "Standard (keine)")
        self.assertIn(("45", "45 Min. vorher", True), custom)

    def test_file_size_and_duration(self):
        self.assertEqual(formatting.file_size(512), "512 Bytes")
        self.assertEqual(formatting.file_size(1536), "1,5 KB")
        self.assertEqual(formatting.file_size(250 * 1024 * 1024), "250 MB")
        self.assertEqual(formatting.duration(None), "–")
        self.assertEqual(formatting.duration(42), "42 s")
        self.assertEqual(formatting.duration(125), "2 min 05 s")
        self.assertEqual(formatting.duration(3725), "1 h 02 min")

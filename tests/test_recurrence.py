import unittest
from datetime import date, datetime

from src import caldav, formatting, recurrence

TUE = date(2026, 11, 10)  # Dienstag


def rule(**raw):
    return recurrence.normalize({"frequency": "weekly", **raw}, TUE)


class NormalizeTests(unittest.TestCase):
    def test_normalizes_and_validates(self):
        self.assertIsNone(recurrence.normalize(None, TUE))
        self.assertIsNone(recurrence.normalize({"frequency": ""}, TUE))
        result = rule(weekdays=["th", "TU"], until="18.12.2026", exdates="22.12.2026, 2026-11-17")
        self.assertEqual(result["weekdays"], ["TU", "TH"])
        self.assertEqual(result["until"], "2026-12-18")
        self.assertEqual(result["exdates"], ["2026-11-17", "2026-12-22"])
        self.assertIsNone(result["count"])
        for bad, message in [
            ({"frequency": "hourly"}, "Unbekannte"),
            ({"frequency": "weekly", "interval": 0}, "Intervall"),
            ({"frequency": "weekly", "weekdays": ["MO"]}, "Wochentage"),
            ({"frequency": "weekly", "until": "2026-11-01"}, "vor dem Beginn"),
            ({"frequency": "daily", "count": 9999}, "Anzahl"),
            ({"frequency": "daily", "exdates": ["31.02.2026"]}, "kein gültiges Datum"),
        ]:
            with self.assertRaisesRegex(ValueError, message):
                recurrence.normalize(bad, TUE)
        fixed = recurrence.normalize({"frequency": "weekly", "weekdays": ["MO"]}, TUE, fix_weekday=True)
        self.assertEqual(fixed["weekdays"], ["MO", "TU"])

    def test_until_wins_over_count(self):
        self.assertIsNone(rule(until="2026-12-01", count=3)["count"])


class OccurrenceTests(unittest.TestCase):
    def dates(self, r, start=TUE, n=6):
        return [d.isoformat() for d in list(recurrence.occurrences(r, start))[:n]]

    def test_weekly_with_days_interval_and_exdates(self):
        r = rule(weekdays=["TU", "TH"], interval=2, count=5, exdates=["2026-11-12"])
        # COUNT zählt vor Abzug der Ausnahmen (RFC 5545)
        self.assertEqual(self.dates(r), ["2026-11-10", "2026-11-24", "2026-11-26", "2026-12-08"])
        self.assertEqual(recurrence.last_occurrence(r, TUE), date(2026, 12, 8))

    def test_monthly_skips_missing_days_and_yearly_leap_day(self):
        monthly = recurrence.normalize({"frequency": "monthly", "count": 4}, date(2027, 1, 31))
        self.assertEqual(self.dates(monthly, date(2027, 1, 31)), ["2027-01-31", "2027-03-31", "2027-05-31", "2027-07-31"])
        yearly = recurrence.normalize({"frequency": "yearly", "count": 2}, date(2028, 2, 29))
        self.assertEqual(self.dates(yearly, date(2028, 2, 29)), ["2028-02-29", "2032-02-29"])

    def test_next_and_membership(self):
        r = rule(weekdays=["TU"], until="2026-12-31")
        self.assertEqual(recurrence.next_occurrence(r, TUE, date(2026, 11, 11)), date(2026, 11, 17))
        self.assertIsNone(recurrence.next_occurrence(r, TUE, date(2027, 1, 1)))
        self.assertTrue(recurrence.is_occurrence(r, TUE, date(2026, 11, 24)))
        self.assertFalse(recurrence.is_occurrence(r, TUE, date(2026, 11, 25)))
        self.assertIsNone(recurrence.last_occurrence(rule(weekdays=["TU"]), TUE))  # offen
        self.assertEqual(len(list(recurrence.occurrences(rule(), TUE))), recurrence.MAX_OCCURRENCES)

    def test_describe(self):
        self.assertEqual(recurrence.describe(None), "Keine")
        self.assertEqual(recurrence.describe(rule(weekdays=["TU", "TH"], until="2026-12-18", exdates=["2026-12-22"])),
                         "Jeden Di. und Do. bis 18.12.2026, außer 22.12.2026")
        self.assertEqual(recurrence.describe(rule(interval=2, weekdays=["MO", "TU", "FR"])),
                         "Alle 2 Wochen am Mo., Di. und Fr., ohne Ende")
        self.assertEqual(recurrence.describe(recurrence.normalize({"frequency": "monthly", "count": 10}, TUE)),
                         "Monatlich, 10 Termine")


class IcalTests(unittest.TestCase):
    def test_timed_series_with_until_in_utc_and_exdates(self):
        content = caldav._build_icalendar(
            {"starts_at": "2026-11-10T17:00", "ends_at": "2026-11-10T18:30", "title": "Training",
             "recurrence": rule(weekdays=["TU", "TH"], until="2027-06-30", exdates=["2026-12-22"])},
            "uid@x", "Europe/Berlin",
        ).decode()
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20270630T215959Z", content)
        self.assertIn("EXDATE;TZID=Europe/Berlin:20261222T170000", content)
        # Zeitzonenregeln reichen bis ins Jahr des Serienendes
        self.assertIn("DTSTART:20270328T020000", content)

    def test_all_day_series_with_count(self):
        content = caldav._build_icalendar(
            {"starts_at": "2026-11-10", "title": "Müll",
             "recurrence": recurrence.normalize({"frequency": "weekly", "interval": 2, "count": 6, "exdates": ["2026-11-24"]}, TUE)},
            "uid@x", "Europe/Berlin",
        ).decode()
        self.assertIn("RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=6", content)
        self.assertIn("EXDATE;VALUE=DATE:20261124", content)


class SeriesFormattingTests(unittest.TestCase):
    def test_series_grouped_by_next_and_archived_after_last(self):
        item = {"id": 1, "starts_at": "2026-11-10T17:00", "recurrence": rule(weekdays=["TU"], until="2026-12-01")}
        groups = formatting.group_suggestions([item], date(2026, 11, 26), "Europe/Berlin")
        self.assertEqual(groups[0][0], "Nächste Woche")  # 1.12. statt Vergangen
        ended = lambda now: formatting.event_has_ended(item, now, "Europe/Berlin", 60)
        self.assertFalse(ended(datetime(2026, 12, 1, 17, 59)))
        self.assertTrue(ended(datetime(2026, 12, 1, 18, 0)))
        open_series = {**item, "recurrence": rule(weekdays=["TU"])}
        self.assertFalse(formatting.event_has_ended(open_series, datetime(2030, 1, 1), "Europe/Berlin", 60))


if __name__ == "__main__":
    unittest.main()

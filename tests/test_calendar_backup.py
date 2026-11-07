import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from src import calendar_backup, caldav


def ics(uid, summary, start="20261113T170000", extra=""):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:{summary}\r\n"
        f"DTSTART;TZID=Europe/Berlin:{start}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class FakeICloud:
    """Kalender als {name: {href: (etag, ics)}}; zählt geladene Termine."""

    def __init__(self, calendars):
        self.calendars = calendars
        self.fetched = 0
        self.fail = set()

    def list_etags(self, url):
        if url in self.fail:
            raise httpx.ConnectError("weg")
        return {href: etag for href, (etag, _) in self.calendars[url].items()}

    def fetch(self, url, hrefs):
        self.fetched += len(hrefs)
        return {href: self.calendars[url][href] for href in hrefs if href in self.calendars[url]}


class CalendarBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = str(Path(self.directory.name) / "history.sqlite3")
        self.cloud = FakeICloud(
            {
                "familie": {
                    "/c/f/a.ics": ("1", ics("a", "St. Martinszug")),
                    "/c/f/b.ics": ("1", ics("b", "Elternabend", "20261120T193000")),
                },
                "mia": {"/c/l/c.ics": ("1", ics("c", "Training", extra="RRULE:FREQ=WEEKLY\r\n"))},
            }
        )
        self.calendars = [{"name": "Familie", "url": "familie"}, {"name": "Mia", "url": "mia"}]

    def snapshot(self, when):
        with self.assertLogs("src.calendar_backup", level="INFO"):
            return calendar_backup.take_snapshot(
                self.store, self.calendars, self.cloud.list_etags, self.cloud.fetch, now=when
            )

    def test_only_changes_are_fetched_and_history_is_reconstructable(self):
        t0 = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
        first = self.snapshot(t0)
        self.assertEqual((first["added"], first["total"]), (3, 3))
        self.assertEqual(self.cloud.fetched, 3)

        unchanged = self.snapshot(t0 + timedelta(hours=1))
        self.assertEqual((unchanged["added"], unchanged["changed"], unchanged["deleted"]), (0, 0, 0))
        self.assertEqual(self.cloud.fetched, 3)  # nichts erneut geladen

        familie = self.cloud.calendars["familie"]
        familie["/c/f/a.ics"] = ("2", ics("a", "St. Martinszug (verschoben)", "20261114T170000"))
        del familie["/c/f/b.ics"]
        familie["/c/f/d.ics"] = ("1", ics("d", "Laternenbasteln"))
        third = self.snapshot(t0 + timedelta(hours=2))
        self.assertEqual((third["added"], third["changed"], third["deleted"]), (1, 1, 1))
        self.assertEqual(self.cloud.fetched, 5)

        old = calendar_backup.search(self.store, first["snapshot_id"])
        status = {row["summary"]: row["status"] for row in old}
        self.assertEqual(
            status,
            {"St. Martinszug": "geändert", "Elternabend": "gelöscht", "Training": "unverändert"},
        )
        changed_only = calendar_backup.search(self.store, first["snapshot_id"], only_changed=True)
        self.assertEqual({row["summary"] for row in changed_only}, {"St. Martinszug", "Elternabend"})
        martin = next(row for row in old if row["summary"] == "St. Martinszug")
        self.assertEqual(martin["current_summary"], "St. Martinszug (verschoben)")

        latest = calendar_backup.search(self.store, third["snapshot_id"])
        self.assertEqual(
            {row["summary"] for row in latest},
            {"St. Martinszug (verschoben)", "Laternenbasteln", "Training"},
        )
        self.assertEqual(
            [row["summary"] for row in calendar_backup.search(self.store, first["snapshot_id"], text="eltern")],
            ["Elternabend"],
        )
        self.assertEqual(
            [r["summary"] for r in calendar_backup.search(self.store, first["snapshot_id"], calendar="Mia")],
            ["Training"],
        )
        version = calendar_backup.get_version(self.store, martin["id"])
        self.assertIn("SUMMARY:St. Martinszug\r\n", version["ics"])

    def test_period_status_counts_and_change_points(self):
        t0 = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
        first = self.snapshot(t0)
        del self.cloud.calendars["familie"]["/c/f/b.ics"]
        second = self.snapshot(t0 + timedelta(hours=1))
        self.snapshot(t0 + timedelta(hours=2))  # unverändert
        sid = first["snapshot_id"]
        counts = calendar_backup.count_by_status(self.store, sid)
        self.assertEqual((counts["gelöscht"], counts["alle"], counts["neu"]), (1, 3, 0))
        deleted = calendar_backup.search(self.store, sid, status="gelöscht")
        self.assertEqual([row["summary"] for row in deleted], ["Elternabend"])
        all_rows = calendar_backup.search(self.store, sid, period="kommend", today=date(2000, 1, 1))
        self.assertEqual(len(all_rows), 3)
        # „Training“ ist eine Serie ohne Ende und bleibt deshalb „kommend“.
        self.assertEqual([r["summary"] for r in calendar_backup.search(self.store, sid, period="kommend", today=date(2100, 1, 1))], ["Training"])
        self.assertEqual(len(calendar_backup.search(self.store, sid, period="vergangen", today=date(2100, 1, 1))), 2)
        page = calendar_backup.search(self.store, sid, limit=2, offset=2)
        self.assertEqual(len(page), 1)
        self.cloud.calendars["familie"]["/c/f/n.ics"] = ("1", ics("n", "Laternenbasteln"))
        latest = self.snapshot(t0 + timedelta(hours=3))
        self.assertEqual(calendar_backup.count_by_status(self.store, sid)["neu"], 1)
        self.assertEqual([r["summary"] for r in calendar_backup.search(self.store, sid, status="neu")], ["Laternenbasteln"])
        self.assertEqual(calendar_backup.count_by_status(self.store, sid, text="training")["neu"], 0)
        points = calendar_backup.change_points(calendar_backup.list_snapshots(self.store))
        self.assertEqual([p["id"] for p in points], [latest["snapshot_id"], second["snapshot_id"], sid])
        self.assertEqual(points[1]["before_id"], sid)
        self.assertIsNone(points[2]["before_id"])

    def test_mass_deletion_thresholds(self):
        md = calendar_backup.mass_deletion
        self.assertEqual(md({"deleted": 3, "deleted_by_calendar": {"A": 3}, "before_by_calendar": {"A": 4}}), [])
        self.assertEqual(md({"deleted": 6, "deleted_by_calendar": {"A": 6}, "before_by_calendar": {"A": 100}}), [])
        self.assertEqual([h["calendar"] for h in md({"deleted": 6, "deleted_by_calendar": {"A": 6, "B": 0}, "before_by_calendar": {"A": 10, "B": 5}})], ["A"])
        many = md({"deleted": 25, "deleted_by_calendar": {"A": 15, "B": 10}, "before_by_calendar": {"A": 500, "B": 400}})
        self.assertEqual([(h["calendar"], h["deleted"]) for h in many], [("A", 15), ("B", 10)])
        t0 = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
        self.snapshot(t0)
        self.cloud.calendars["familie"].clear()
        result = self.snapshot(t0 + timedelta(hours=1))
        self.assertEqual(result["deleted_by_calendar"]["Familie"], 2)
        self.assertEqual(result["before_by_calendar"]["Familie"], 2)
        self.assertEqual(calendar_backup.previous_snapshot_id(self.store, result["snapshot_id"]), result["snapshot_id"] - 1)

    def test_running_series_counts_as_upcoming(self):
        familie = self.cloud.calendars["familie"]
        familie["/c/f/s.ics"] = ("1", "BEGIN:VEVENT\r\nUID:s\r\nSUMMARY:Turnen\r\nDTSTART:20250101T160000Z\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n")
        familie["/c/f/e.ics"] = ("1", "BEGIN:VEVENT\r\nUID:e\r\nSUMMARY:Alt\r\nDTSTART:20250101T160000Z\r\nRRULE:FREQ=WEEKLY;UNTIL=20250301T000000Z\r\nEND:VEVENT\r\n")
        sid = self.snapshot(datetime(2026, 11, 4, tzinfo=timezone.utc))["snapshot_id"]
        upcoming = {row["summary"] for row in calendar_backup.search(self.store, sid, period="kommend", today=date(2026, 11, 7))}
        self.assertIn("Turnen", upcoming)
        self.assertNotIn("Alt", upcoming)
        ordered = [row["summary"] for row in calendar_backup.search(self.store, sid, period="kommend", today=date(2026, 11, 7))]
        self.assertEqual(ordered[-1], "Turnen")  # laufende Serie nach den datierten Terminen

    def test_failed_calendar_is_not_treated_as_deleted(self):
        t0 = datetime(2026, 11, 4, 10, tzinfo=timezone.utc)
        self.snapshot(t0)
        self.cloud.fail.add("familie")
        result = self.snapshot(t0 + timedelta(hours=1))
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["errors"], ["Familie: ConnectError"])
        snapshot = calendar_backup.list_snapshots(self.store)[0]
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(len(calendar_backup.search(self.store, result["snapshot_id"])), 3)

    def test_prune_keeps_needed_versions_only(self):
        t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
        first = self.snapshot(t0)
        self.cloud.calendars["familie"]["/c/f/a.ics"] = ("2", ics("a", "Neu"))
        self.snapshot(t0 + timedelta(days=10))
        with self.assertLogs("src.calendar_backup", level="INFO"):
            removed = calendar_backup.prune(self.store, 5, now=t0 + timedelta(days=12))
        self.assertEqual(removed, 1)
        self.assertIsNone(calendar_backup.get_version(self.store, 1))  # alte Version weg
        snapshots = calendar_backup.list_snapshots(self.store)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(
            {row["summary"] for row in calendar_backup.search(self.store, snapshots[0]["id"])},
            {"Neu", "Elternabend", "Training"},
        )
        self.assertNotEqual(first["snapshot_id"], snapshots[0]["id"])

    def test_parse_event_uses_master_and_local_time(self):
        data = ics("x", "Serie\\, wöchentlich", extra="RRULE:FREQ=WEEKLY\r\nLOCATION:Halle\; Eingang B\r\n")
        data = data.replace(
            "END:VCALENDAR",
            "BEGIN:VEVENT\r\nUID:x\r\nRECURRENCE-ID:20261120T170000\r\nSUMMARY:Ausnahme\r\nEND:VEVENT\r\nEND:VCALENDAR",
        )
        info = calendar_backup.parse_event(data)
        self.assertEqual(info["summary"], "Serie, wöchentlich")
        self.assertEqual(info["location"], "Halle; Eingang B")
        self.assertTrue(info["recurring"])
        self.assertEqual(info["dtstart"], "2026-11-13T17:00:00+01:00")
        self.assertEqual(calendar_backup.parse_event(ics("y", "x", "20261113"))["dtstart"], "2026-11-13")


class DayViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = str(Path(self.directory.name) / "history.sqlite3")

    def event(self, uid, summary, lines):
        return f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{summary}\r\n{lines}END:VEVENT\r\nEND:VCALENDAR\r\n"

    def snapshot(self, events):
        cloud = {f"/c/{i}.ics": ('"1"', ics) for i, ics in enumerate(events)}
        with self.assertLogs("src.calendar_backup", level="INFO"):
            calendar_backup.take_snapshot(
                self.store, [{"name": "Mia", "url": "u"}],
                lambda u: {h: e for h, (e, _) in cloud.items()},
                lambda u, hs: {h: cloud[h] for h in hs},
            )

    def test_parse_end_duration_rrule_exdates(self):
        info = calendar_backup.parse_event(self.event("a", "Training",
            "DTSTART;TZID=Europe/Berlin:20261110T170000\r\nDURATION:PT1H30M\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20270630T215959Z\r\n"
            "EXDATE;TZID=Europe/Berlin:20261222T170000,20261224T170000\r\n"))
        self.assertEqual(info["dtend"], "2026-11-10T18:30:00+01:00")
        self.assertEqual(info["rrule"], "FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20270630T215959Z")
        self.assertEqual(info["exdates"], ["2026-12-22", "2026-12-24"])

    def test_rule_translation_supports_common_and_skips_exotic(self):
        start = date(2026, 11, 10)
        rule = calendar_backup.rule_from_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;WKST=MO", start)
        self.assertEqual((rule["interval"], rule["weekdays"]), (2, ["TU"]))
        self.assertIsNotNone(calendar_backup.rule_from_rrule("FREQ=MONTHLY;BYMONTHDAY=10;COUNT=5", start))
        for exotic in ("FREQ=MONTHLY;BYDAY=2TU", "FREQ=MONTHLY;BYMONTHDAY=15", "FREQ=WEEKLY;BYSETPOS=1", "FREQ=HOURLY", None):
            self.assertIsNone(calendar_backup.rule_from_rrule(exotic, start), exotic)

    def test_events_on_day_includes_series_multi_day_and_skips_exceptions(self):
        self.snapshot([
            self.event("t", "Training", "DTSTART;TZID=Europe/Berlin:20261110T170000\r\nDTEND;TZID=Europe/Berlin:20261110T183000\r\n"
                       "RRULE:FREQ=WEEKLY;BYDAY=TU,TH\r\nEXDATE;TZID=Europe/Berlin:20261126T170000\r\n"),
            self.event("u", "Urlaub", "DTSTART;VALUE=DATE:20261123\r\nDTEND;VALUE=DATE:20261128\r\n"),
            self.event("z", "Zahnarzt", "DTSTART:20261124T080000Z\r\nDTEND:20261124T083000Z\r\n"),
            self.event("x", "Exotisch", "DTSTART;TZID=Europe/Berlin:20261103T090000\r\nRRULE:FREQ=MONTHLY;BYDAY=4TU\r\n"),
        ])
        tz = "Europe/Berlin"
        day = calendar_backup.events_on_day(self.store, date(2026, 11, 24), tz)
        self.assertEqual([(e["summary"], e["all_day"]) for e in day],
                         [("Urlaub", True), ("Zahnarzt", False), ("Training", False)])
        training = day[2]
        self.assertEqual((training["start"].strftime("%H:%M"), training["end"].strftime("%H:%M")), ("17:00", "18:30"))
        self.assertEqual(day[1]["start"].strftime("%H:%M"), "09:00")  # UTC → lokal
        self.assertEqual([e["summary"] for e in calendar_backup.events_on_day(self.store, date(2026, 11, 26), tz)], ["Urlaub"])  # Ausnahme
        self.assertEqual(calendar_backup.events_on_day(self.store, date(2026, 11, 28), tz), [])  # DTEND exklusiv, Samstag

    def test_backfill_upgrades_old_versions(self):
        self.snapshot([self.event("b", "Alt", "DTSTART:20261124T080000Z\r\nDTEND:20261124T090000Z\r\n")])
        with calendar_backup.connect(self.store) as connection:
            connection.execute("UPDATE event_versions SET dtend = NULL, parser_version = 1")
        with self.assertLogs("src.calendar_backup", level="INFO"):
            self.assertEqual(calendar_backup.backfill(self.store), 1)
        self.assertEqual(calendar_backup.backfill(self.store), 0)
        day = calendar_backup.events_on_day(self.store, date(2026, 11, 24), "Europe/Berlin")
        self.assertEqual(day[0]["end"].strftime("%H:%M"), "10:00")


class CalDavResourceTests(unittest.TestCase):
    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://p1.example")

    def test_lists_etags_and_multigets_in_batches(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "PROPFIND":
                body = (
                    '<d:multistatus xmlns:d="DAV:">'
                    '<d:response><d:href>/123/calendars/fam/</d:href><d:propstat><d:prop>'
                    '<d:resourcetype><d:collection/></d:resourcetype></d:prop>'
                    '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                    + "".join(
                        f'<d:response><d:href>/123/calendars/fam/{i}.ics</d:href><d:propstat><d:prop>'
                        f'<d:getetag>"e{i}"</d:getetag><d:resourcetype/></d:prop>'
                        '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                        for i in range(3)
                    )
                    + "</d:multistatus>"
                )
                return httpx.Response(207, text=body)
            hrefs = request.content.decode().count("<d:href>")
            body = '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + "".join(
                f'<d:response><d:href>/123/calendars/fam/{i}.ics</d:href><d:propstat><d:prop>'
                f'<d:getetag>"e{i}"</d:getetag><c:calendar-data>BEGIN:VCALENDAR&#13;\nX:{i}&#13;\nEND:VCALENDAR</c:calendar-data>'
                '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
                for i in range(hrefs)
            ) + "</d:multistatus>"
            return httpx.Response(207, text=body)

        with self.client(handler) as client:
            url = "https://p1.example/123/calendars/fam/"
            etags = caldav.list_event_etags(client, url)
            self.assertEqual(etags, {f"/123/calendars/fam/{i}.ics": f'"e{i}"' for i in range(3)})
            events = caldav.fetch_events(client, url, sorted(etags), batch_size=2)
        self.assertEqual([r.method for r in requests], ["PROPFIND", "REPORT", "REPORT"])
        self.assertEqual(len(events), 2)  # Fake liefert pro Batch Indizes ab 0
        self.assertIn("\r\n", events["/123/calendars/fam/0.ics"][1])

    def test_restore_creates_or_overwrites_with_conditions(self):
        seen = []
        exists = {"value": False}

        def handler(request):
            seen.append((request.method, dict(request.headers)))
            if request.method == "GET":
                return httpx.Response(200, headers={"ETag": '"cur"'}) if exists["value"] else httpx.Response(404)
            return httpx.Response(201)

        with self.client(handler) as client:
            url = "https://p1.example/123/calendars/fam/"
            self.assertEqual(caldav.restore_event_resource(client, url, "a.ics", "ICS"), "created")
            self.assertEqual(seen[-1][1].get("if-none-match"), "*")
            exists["value"] = True
            self.assertEqual(caldav.restore_event_resource(client, url, "a.ics", "ICS"), "overwritten")
            self.assertEqual(seen[-1][1].get("if-match"), '"cur"')


if __name__ == "__main__":
    unittest.main()

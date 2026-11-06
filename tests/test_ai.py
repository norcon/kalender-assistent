import unittest
from unittest.mock import Mock, patch

from src.ai import extract_events, validate_events


class ValidateEventsTests(unittest.TestCase):
    def test_accepts_valid_timed_and_all_day_events(self):
        events = validate_events(
            {
                "events": [
                    {
                        "title": "Training",
                        "start": "2026-11-04T17:00:00+01:00",
                        "end": "2026-11-04T18:00:00+01:00",
                        "confidence": 0.9,
                    },
                    {
                        "title": "Schulfest",
                        "start": "2026-11-05",
                        "confidence": 0.7,
                    },
                ]
            }
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["title"], "Training")
        self.assertIsNone(events[1]["end"])

    def test_skips_invalid_candidates(self):
        events = validate_events(
            {
                "events": [
                    {"title": "Ohne Datum", "confidence": 0.5},
                    {
                        "title": "Ungültiges Datum",
                        "start": "morgen",
                        "confidence": 0.5,
                    },
                    {
                        "title": "Zu geringe Sicherheit",
                        "start": "2026-11-05",
                        "confidence": 2,
                    },
                ]
            }
        )
        self.assertEqual(events, [])

    def test_requires_events_array(self):
        with self.assertRaises(ValueError):
            validate_events({"events": "keine Liste"})

    def test_validates_actions_and_defaults_to_new(self):
        events = validate_events(
            {
                "events": [
                    {
                        "title": "Training",
                        "start": "2026-11-05T17:00",
                    },
                    {
                        "title": "Training abgesagt",
                        "start": "2026-11-05T17:00",
                        "action": "cancel",
                    },
                    {
                        "title": "Ungültige Aktion",
                        "start": "2026-11-05T17:00",
                        "action": "delete",
                    },
                ]
            }
        )
        self.assertEqual([event["action"] for event in events], ["new", "cancel"])

    def test_only_accepts_exact_family_calendar_names_and_leaves_uncertain_values_unassigned(self):
        events = validate_events(
            {
                "events": [
                    {
                        "title": "Mia Training",
                        "start": "2026-11-05T17:00",
                        "person": "Mia",
                    },
                    {
                        "title": "Sam appointment",
                        "start": "2026-11-05T17:00",
                        "person": " sam ",
                    },
                    {
                        "title": "Schulveranstaltung",
                        "start": "2026-11-05",
                        "person": "Kind 1",
                    },
                    {
                        "title": "Unklarer Termin",
                        "start": "2026-11-05",
                        "person": "Beide Kinder",
                    },
                ]
            },
            ("Alex", "Sam", "Mia", "Ben", "Familie"),
        )

        self.assertEqual([event["person"] for event in events], ["Mia", "Sam", None, None])

    def test_system_prompt_requires_explicit_evidence_for_calendar_assignment(self):
        from src.ai import build_system_prompt

        prompt = build_system_prompt([
            {"name": "Alex", "description": "Elternteil", "shared": False},
            {"name": "Mia", "description": "Tochter, 4. Klasse", "shared": False},
            {"name": "Familie", "description": "", "shared": True},
        ])
        self.assertIn('- "Alex" – Elternteil', prompt)
        self.assertIn('- "Mia" – Tochter, 4. Klasse', prompt)
        self.assertIn('- "Familie" – gemeinsamer Kalender', prompt)
        self.assertIn('Wähle "Familie" nur bei einem ausdrücklich', prompt)
        self.assertIn("person=null", prompt)
        self.assertIn("Absender, Empfänger, CC", prompt)
        self.assertNotIn("{calendar_rules}", prompt)

    def test_system_prompt_without_calendars_and_without_shared_calendar(self):
        from src.ai import build_system_prompt

        self.assertIn('gib immer "person": null zurück', build_system_prompt([]))
        prompt = build_system_prompt([{"name": 'Te"st', "description": "", "shared": False}])
        self.assertIn("- \"Te'st\"", prompt)
        self.assertNotIn("nur bei einem ausdrücklich", prompt)

    @patch("src.ai.httpx.post")
    def test_uses_long_read_timeout_for_local_generation(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": '{"events": []}'}}]
        }
        post.return_value = response

        result = extract_events(
            "http://localhost:1234/v1",
            "",
            "local-model",
            600,
            "Betreff",
            "sender",
            "2026-11-03T18:00:00+01:00",
            "Text",
        )

        self.assertEqual(result, [])
        timeout = post.call_args.kwargs["timeout"]
        self.assertEqual(timeout.read, 600)
        self.assertEqual(timeout.connect, 10)
        self.assertEqual(timeout.write, 30)

    @patch("src.ai.httpx.post")
    def test_sends_supported_image_as_multimodal_message(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": '{"events": []}'}}]
        }
        post.return_value = response
        extract_events(
            "http://localhost:1234/v1",
            "",
            "vision-model",
            60,
            "Termin",
            "sender",
            "2026-11-03T18:00:00+01:00",
            "",
            images=[
                {"name": "einladung.png", "mime_type": "image/png", "data": b"png-data"}
            ],
        )

        content = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual(content[1]["text"], "Bildanhang: einladung.png")
        self.assertEqual(
            content[2]["image_url"]["url"],
            "data:image/png;base64,cG5nLWRhdGE=",
        )


class RecurrenceValidationTests(unittest.TestCase):
    def test_recurrence_is_normalized_or_dropped(self):
        from src.ai import validate_events
        events = validate_events({"events": [
            {"title": "Training", "start": "2026-11-10T17:00", "confidence": 0.9,
             "recurrence": {"frequency": "weekly", "weekdays": ["TU"], "until": "2027-06-30"}},
            {"title": "Kaputt", "start": "2026-11-10T17:00", "confidence": 0.9,
             "recurrence": {"frequency": "stündlich"}},
        ]})
        self.assertEqual(events[0]["recurrence"]["until"], "2027-06-30")
        self.assertIsNone(events[1]["recurrence"])  # Einzeltermin statt Ablehnung


class QuoteTests(unittest.TestCase):
    def test_quotes_must_appear_in_text_unless_from_images(self):
        from src.ai import check_quotes
        text = "Liebe Eltern,\nder Elternabend findet am 12. November um 19:30 Uhr statt."
        events = [
            {"title": "Elternabend", "quote": "Der Elternabend  findet am 12. November um 19:30 Uhr statt."},
            {"title": "Erfunden", "quote": "Elternabend am 12.11. um halb acht"},
        ]
        with self.assertLogs("src.ai", level="WARNING"):
            checked = check_quotes([dict(e) for e in events], text, has_images=False)
        self.assertEqual(checked[0]["quote_source"], "text")
        self.assertIsNone(checked[1]["quote"])
        from_image = check_quotes([dict(events[1])], text, has_images=True)
        self.assertEqual(from_image[0]["quote_source"], "image")


if __name__ == "__main__":
    unittest.main()


class NonAppointmentPromptTests(unittest.TestCase):
    def test_prompt_excludes_shipping_payment_and_advertising(self):
        from src.ai import build_system_prompt
        text = " ".join(build_system_prompt([]).split())
        for phrase in ("Versand-, Liefer-, Zustell- und Abholbenachrichtigungen", "Zahlungs-, Rückgabe- und Widerrufsfristen", "Werbung, Newsletter", "Handwerker-, Monteur- oder Servicetermin", "gebuchte Reise"):
            self.assertIn(phrase, text)
        self.assertNotIn("Buchungs", text)

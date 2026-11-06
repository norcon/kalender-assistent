import unittest

from src.person_learning import apply_person_corrections


class PersonLearningTests(unittest.TestCase):
    def test_applies_manual_correction_for_similar_title_from_same_sender(self):
        events = [
            {"title": "Fußballtraining Mia", "person": None},
            {"title": "Elternabend Klasse 4", "person": None},
        ]
        corrections = [
            {
                "source_sender": "Fußballverein <verein@example.org>",
                "event_title": "Fußballtraining Mia",
                "corrected_person": "Mia",
            }
        ]

        updated, applied = apply_person_corrections(
            events, corrections, "verein@example.org"
        )

        self.assertEqual(applied, 1)
        self.assertEqual(updated[0]["person"], "Mia")
        self.assertIsNone(updated[1]["person"])

    def test_does_not_apply_correction_to_different_sender_or_unrelated_title(self):
        corrections = [
            {
                "source_sender": "verein@example.org",
                "event_title": "Fußballtraining Mia",
                "corrected_person": "Mia",
            }
        ]
        events = [{"title": "Elternabend Klasse 4", "person": "Alex"}]

        wrong_sender, count = apply_person_corrections(
            events, corrections, "schule@example.org"
        )
        self.assertEqual(count, 0)
        self.assertEqual(wrong_sender[0]["person"], "Alex")

        unrelated, count = apply_person_corrections(
            events, corrections, "verein@example.org"
        )
        self.assertEqual(count, 0)
        self.assertEqual(unrelated[0]["person"], "Alex")

    def test_conflicting_manual_corrections_remain_unassigned(self):
        events = [{"title": "Fußballtraining Mia", "person": None}]
        corrections = [
            {
                "source_sender": "verein@example.org",
                "event_title": "Fußballtraining Mia",
                "corrected_person": "Mia",
            },
            {
                "source_sender": "verein@example.org",
                "event_title": "Fußballtraining Mia",
                "corrected_person": "Alex",
            },
        ]

        updated, applied = apply_person_corrections(
            events, corrections, "verein@example.org"
        )

        self.assertEqual(applied, 0)
        self.assertIsNone(updated[0]["person"])


if __name__ == "__main__":
    unittest.main()

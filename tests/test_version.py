import re
import unittest
from pathlib import Path

from src import version

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


class VersionTests(unittest.TestCase):
    def test_version_matches_newest_changelog_entry(self):
        newest = re.search(r"^## (\d+\.\d+\.\d+) – (\d{4}-\d{2}-\d{2})$", CHANGELOG.read_text(), re.M)
        self.assertIsNotNone(newest, "CHANGELOG.md enthält keinen Versionseintrag")
        self.assertEqual(newest.group(1), version.VERSION, "Version in src/version.py und CHANGELOG.md weichen ab")
        self.assertEqual(newest.group(2), version.RELEASED)


if __name__ == "__main__":
    unittest.main()

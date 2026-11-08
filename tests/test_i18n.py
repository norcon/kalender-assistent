"""Übersetzungen: alle markierten Texte müssen in src/locales/en.json stehen."""

import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_JINJA = re.compile(r'''\b_n?\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')(?:\s*,\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))?''')
_JS = re.compile(r'''\bt\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''')


def _literal(token: str) -> str:
    return ast.literal_eval(token)


def _python_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name not in {"_", "gettext", "ngettext"}:
            continue
        count = 2 if name == "ngettext" else 1
        for argument in node.args[:count]:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                keys.add(argument.value)
    return keys


def collect_keys() -> set[str]:
    keys: set[str] = set()
    for template in (ROOT / "templates").glob("*.html"):
        if template.name.endswith("_en.html"):
            continue
        for match in _JINJA.finditer(template.read_text(encoding="utf-8")):
            keys.update(_literal(group) for group in match.groups() if group)
    for script in (ROOT / "static").glob("*.js"):
        keys.update(_literal(m.group(1)) for m in _JS.finditer(script.read_text(encoding="utf-8")))
    for module in (ROOT / "src").glob("*.py"):
        keys.update(_python_keys(module))

    # Texttabellen, die erst beim Anzeigen übersetzt werden
    from src import app, db, formatting, settings_store
    keys.update(formatting.HISTORY_LABELS.values())
    keys.update(app.ARCHIVE_STATUS.values())
    keys.update(app._MESSAGES.values())
    keys.update(label for fields in app.CONNECTION_SECTIONS.values() for _key, label, _kind in fields)
    keys.update(label for _key, label, _kind in app.MAILBOX_FIELDS)
    for _channel, title, description, _keys in app.NOTIFY_CHANNEL_GROUPS:
        keys.update({title, description})
    keys.update({
        "Vergangen", "Heute", "Morgen", "Diese Woche", "Nächste Woche", "Später", "Ohne gültiges Datum",
        "unverändert", "geändert", "gelöscht",
        db.SHARED_ACTOR, "Automatische Erkennung", db.REANALYSIS_ACTOR,
        "Nachrichtentext", "Kalendereinladung", "Beginn", "Ende", "Ausnahme",
        "Datenbank", "Datenbank-Sicherungen", "Kalender-Sicherung",
        "IMAP-Server", "IMAP-Benutzer", "KI-Server", "iCloud-Server", "iCloud-Benutzer",
        "iCloud-Passwort", "gesetzt", "nicht gesetzt",
    })
    for parameter in settings_store.PARAMETERS:
        keys.update({parameter.group, parameter.label, parameter.help})
        if parameter.unit:
            keys.add(parameter.unit)
        keys.update(label for _value, label in parameter.choices)
    return {key for key in keys if key.strip()}


class TranslationTests(unittest.TestCase):
    def test_every_marked_text_has_an_english_translation(self):
        catalog = json.loads((ROOT / "src" / "locales" / "en.json").read_text(encoding="utf-8"))
        missing = sorted(collect_keys() - set(catalog))
        self.assertEqual(missing, [], f"{len(missing)} Übersetzung(en) fehlen")

    def test_placeholders_match(self):
        catalog = json.loads((ROOT / "src" / "locales" / "en.json").read_text(encoding="utf-8"))
        pattern = re.compile(r"\{(\w+)\}")
        wrong = [
            key for key, value in catalog.items()
            if set(pattern.findall(key)) != set(pattern.findall(value))
        ]
        self.assertEqual(wrong, [])

    def test_language_negotiation_and_formatting(self):
        from datetime import date
        from src import formatting, i18n, recurrence
        self.assertEqual(i18n.negotiate("en", "de", None), "en")
        self.assertEqual(i18n.negotiate(None, "auto", "en-US,en;q=0.9,de;q=0.8"), "en")
        self.assertEqual(i18n.negotiate(None, "auto", "fr-FR"), "de")
        self.assertEqual(i18n.negotiate("xx", "en", "de"), "en")
        token = i18n.activate("en")
        try:
            self.assertEqual(formatting.event_time("2026-11-06T17:00", "Europe/Berlin"), "Fri, 6 Nov 2026, 17:00")
            self.assertEqual(formatting.relative_day(date(2026, 11, 7), date(2026, 11, 6)), "tomorrow")
            self.assertEqual(i18n.ngettext("{count} Termin", "{count} Termine", 2), "2 events")
            rule = {"frequency": "weekly", "interval": 1, "weekdays": ["TU", "TH"], "until": None, "count": None, "exdates": []}
            self.assertEqual(recurrence.describe(rule), "Every Tue and Thu, no end date")
        finally:
            i18n.reset(token)
        self.assertEqual(formatting.event_time("2026-11-06T17:00", "Europe/Berlin"), "Fr., 6. Nov. 2026, 17:00 Uhr")


if __name__ == "__main__":
    keys = sorted(collect_keys())
    catalog_path = ROOT / "src" / "locales" / "en.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
    print(json.dumps([k for k in keys if k not in catalog], ensure_ascii=False, indent=0))

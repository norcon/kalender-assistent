"""Demo-Modus (DEMO_MODE=1): erfundene Beispieldaten, keine Verbindungen nach außen.

Für Ausprobieren und Screenshots. Postfach, KI und iCloud werden nicht
angesprochen; „Übertragen“ tut nur so.
"""

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import caldav, calendar_backup, db, ics_import

logger = logging.getLogger(__name__)

_DE = {
    "family": "Familie",
    "desc": ("Elternteil, arbeitet in Teilzeit", "Elternteil, Elternbeirat Klasse 4b",
             "Tochter, 4. Klasse, Schwimmverein", "Sohn, Kita, Fußball E-Jugend"),
    "school": "Grundschule Am Park <info@grundschule.example>",
    "club": "FC Beispiel <jugend@fc-beispiel.example>",
    "teacher": "Frau Lehrerin <klasse4b@grundschule.example>",
    "parent_evening": ("Elternabend Klasse 4b", "Grundschule Am Park, Raum 12",
                       "Bitte Unterlagen zur Klassenfahrt mitbringen.",
                       "Der Elternabend der Klasse 4b findet morgen um 19:30 Uhr in Raum 12 statt."),
    "dentist": ("Zahnarzt Mia – Kontrolle", "Termin für Mia am Montag um 8:15 Uhr", "terminkarte.jpg"),
    "crafts": ("Laternenbasteln Kita", "Kita Sonnenschein"),
    "zoo": "Familienausflug Zoo",
    "week_subject": "Termine der Woche",
    "training": ("Fußballtraining E-Jugend", "Sportplatz Am Wald",
                 "Training ab sofort immer zur gleichen Zeit bis zu den Osterferien.", "Neue Trainingszeiten"),
    "invite": ("Elterngespräch Mia", "Grundschule Am Park\\, Raum 4", "Einladung Elterngespräch"),
    "parcels": ("Lieferung Bastelset", "Paket kommt heute", "Zustellung verschoben"),
    "delivery": ("Lieferfenster Waschmaschine", "Ihr Liefertermin"),
    "parcel_hidden": ("Sendung unterwegs", "Ihre Sendung"),
    "newsletter": ("Herbstmarkt mit 20 % Rabatt", "Unser Newsletter", "Gartencenter <news@garten.example>"),
    "summer": ("Sommerfest Verein", "Sommerfest"),
    "cancel": ("Fußballtraining", "Training fällt aus"),
    "cloud": ("Yoga", "Elternabend 4b"),
}
_EN = {
    "family": "Family",
    "desc": ("parent, works part-time", "parent, class 4b parents’ council",
             "daughter, 4th grade, swimming club", "son, nursery, junior football"),
    "school": "Park Primary School <office@school.example>",
    "club": "FC Example <youth@fc-example.example>",
    "teacher": "Ms Teacher <class4b@school.example>",
    "parent_evening": ("Parents’ evening class 4b", "Park Primary School, room 12",
                       "Please bring the school trip forms.",
                       "The parents’ evening for class 4b takes place tomorrow at 7:30 pm in room 12."),
    "dentist": ("Dentist Mia – check-up", "Appointment for Mia on Monday at 8:15 am", "appointment-card.jpg"),
    "crafts": ("Lantern crafts at nursery", "Sunshine Nursery"),
    "zoo": "Family trip to the zoo",
    "week_subject": "This week’s dates",
    "training": ("Junior football training", "Woodside sports ground",
                 "From now on training is always at the same time until the Easter holidays.", "New training times"),
    "invite": ("Parent-teacher meeting Mia", "Park Primary School\\, room 4", "Invitation: parent-teacher meeting"),
    "parcels": ("Craft kit delivery", "Parcel arrives today", "Delivery rescheduled"),
    "delivery": ("Washing machine delivery window", "Your delivery date"),
    "parcel_hidden": ("Parcel on its way", "Your parcel"),
    "newsletter": ("Autumn market with 20 % off", "Our newsletter", "Garden centre <news@garden.example>"),
    "summer": ("Club summer party", "Summer party"),
    "cancel": ("Football training", "Training cancelled"),
    "cloud": ("Yoga", "Parents’ evening 4b"),
}
_COLORS = ("#ff2968", "#ffcc00", "#1badf8", "#63da38", "#cc73e1")


def calendars(language: str = "de") -> tuple[dict[str, Any], ...]:
    text = _EN if language == "en" else _DE
    names = ("Alex", "Sam", "Mia", "Ben", text["family"])
    descriptions = (*text["desc"], "")
    return tuple(
        {"name": name, "color": color, "description": description, "shared": index == 4}
        for index, (name, color, description) in enumerate(zip(names, _COLORS, descriptions))
    )


CALENDARS = calendars("de")


def install_fakes(language: str = "de") -> None:
    """Ersetzt alle schreibenden und lesenden iCloud-Zugriffe durch Attrappen."""

    def discover(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"name": c["name"], "write_access": True, "calendar_url": f"demo://{c['name']}", "color": c["color"]}
            for c in calendars(language)
        ]

    def created(*_args: Any, **_kwargs: Any) -> str:
        return "created"

    def deleted(*_args: Any, **_kwargs: Any) -> bool:
        return True

    @contextmanager
    def no_client(*_args: Any, **_kwargs: Any):
        yield None

    caldav.discover_icloud_calendars = discover
    caldav.create_icloud_event = created
    caldav.resend_icloud_event = created
    caldav.delete_icloud_event = deleted
    caldav.restore_event_resource = created
    caldav.open_client = no_client
    caldav.list_event_etags = lambda *_a, **_k: {}
    caldav.fetch_events = lambda *_a, **_k: {}
    logger.info("Demo-Modus: iCloud-Zugriffe sind abgeschaltet, Übertragungen werden nur simuliert.")


def _day(offset: int, time: str | None = None) -> str:
    value = (date.today() + timedelta(days=offset)).isoformat()
    return f"{value}T{time}" if time else value


def _save(path: str, uid: int, events: list[dict[str, Any]], subject: str, sender: str, days_ago: int = 0) -> None:
    received = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db.save_processed_message(
        path, "demo@demo.example/INBOX", "1", uid, f"demo@demo.example/INBOX/1/{uid}", events,
        {"subject": subject, "sender": sender, "received_at": received},
    )


def seed(database_path: str, calendar_store_path: str, language: str = "de") -> None:
    """Legt einmalig Beispieldaten an (nur in eine leere Datenbank)."""
    if db.list_suggestions(database_path, include_dismissed=True):
        return
    text = _EN if language == "en" else _DE
    demo_calendars = calendars(language)
    family = text["family"]
    db.save_target_calendars(database_path, [dict(c) for c in demo_calendars])
    calendar_backup.remember_calendars(
        calendar_store_path, [{"name": c["name"], "url": f"demo://{c['name']}", "color": c["color"]} for c in demo_calendars]
    )

    weekday = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][(date.today() + timedelta(days=5)).weekday()]
    title, location, notes, quote = text["parent_evening"]
    dentist, dentist_quote, dentist_file = text["dentist"]
    crafts, crafts_place = text["crafts"]
    _save(database_path, 1, [
        {"title": title, "start": _day(1, "19:30"), "end": _day(1, "21:00"), "location": location,
         "person": "Sam", "confidence": 0.92, "notes": notes, "quote": quote, "quote_source": "text"},
        {"title": dentist, "start": _day(3, "08:15"), "person": "Mia", "confidence": 0.55,
         "quote": dentist_quote, "quote_source": "image", "source_part": dentist_file},
        {"title": crafts, "start": _day(9, "15:00"), "person": None, "confidence": 0.81, "location": crafts_place},
        {"title": text["zoo"], "start": _day(30), "person": family, "confidence": 0.95},
    ], text["week_subject"], text["school"])
    training, ground, training_quote, training_subject = text["training"]
    _save(database_path, 2, [
        {"title": training, "start": _day(5, "17:00"), "end": _day(5, "18:30"), "location": ground,
         "person": "Ben", "confidence": 0.88,
         "recurrence": {"frequency": "weekly", "interval": 1, "weekdays": [weekday], "until": _day(120),
                        "count": None, "exdates": [_day(19)]},
         "quote": training_quote, "quote_source": "text"},
    ], training_subject, text["club"])

    invite_title, invite_place, invite_subject = text["invite"]
    invitation = (
        f"BEGIN:VCALENDAR\nMETHOD:REQUEST\nBEGIN:VEVENT\nUID:demo-invite\nSUMMARY:{invite_title}\n"
        f"DTSTART;TZID=Europe/Berlin:{_day(6).replace('-', '')}T163000\n"
        f"DTEND;TZID=Europe/Berlin:{_day(6).replace('-', '')}T170000\n"
        f"LOCATION:{invite_place}\nEND:VEVENT\nEND:VCALENDAR\n"
    )
    _save(database_path, 3, ics_import.parse_invitation(invitation, "Europe/Berlin", "invitation.ics" if language == "en" else "einladung.ics"),
          invite_subject, text["teacher"])

    shop = "Shop <shipping@shop.example>" if language == "en" else "Versand <versand@shop.example>"
    for uid, title in zip((10, 11, 12), text["parcels"]):
        _save(database_path, uid, [{"title": title, "start": _day(1), "confidence": 1.0}], title, shop, days_ago=5)
        item = next(i for i in db.list_suggestions(database_path) if i["title"] == title)
        db.dismiss_suggestion(database_path, item["id"])
    delivery, delivery_subject = text["delivery"]
    _save(database_path, 13, [{"title": delivery, "start": _day(4), "confidence": 0.8}], delivery_subject, shop)
    parcel, parcel_subject = text["parcel_hidden"]
    _save(database_path, 14, [{"title": parcel, "start": _day(2), "confidence": 1.0}],
          parcel_subject, "Parcels <noreply@parcel.example>")
    db.add_ignored_sender(database_path, "parcel.example", "noreply@parcel.example", parcel_subject)
    news_title, news_subject, news_sender = text["newsletter"]
    _save(database_path, 15, [{"title": news_title, "start": _day(8), "confidence": 0.7,
                               "hidden_reason": db.NEWSLETTER_REASON}], news_subject, news_sender)

    summer, summer_subject = text["summer"]
    _save(database_path, 20, [{"title": summer, "start": _day(-40), "person": family, "confidence": 0.9}],
          summer_subject, text["club"], days_ago=50)
    old = next(i for i in db.list_suggestions(database_path) if i["title"] == summer)
    db.mark_suggestion_exported(database_path, old["id"], "demo-uid", family)

    series = next(i for i in db.list_suggestions(database_path) if i["title"] == training)
    cancel_title, cancel_subject = text["cancel"]
    now = datetime.now(timezone.utc).isoformat()
    with db.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO change_requests (source_key, action, event_data, related_suggestion_id, "
            "source_subject, source_sender, source_received_at, created_at) "
            "VALUES ('demo@demo.example/INBOX/1/30', 'cancel', ?, ?, ?, ?, ?, ?)",
            (json.dumps({"title": cancel_title, "start": _day(12, "17:00")}), series["id"],
             cancel_subject, text["club"], now, now),
        )

    today = date.today()
    fmt = lambda offset, hm: (today + timedelta(days=offset)).strftime("%Y%m%d") + "T" + hm.replace(":", "") + "00"  # noqa: E731
    yoga, evening = text["cloud"]
    cloud = {
        "/a.ics": ('"a"', f"BEGIN:VEVENT\r\nUID:a\r\nSUMMARY:{yoga}\r\nDTSTART;TZID=Europe/Berlin:{fmt(1, '19:00')}\r\nDTEND;TZID=Europe/Berlin:{fmt(1, '20:00')}\r\nEND:VEVENT\r\n"),
        "/b.ics": ('"b"', f"BEGIN:VEVENT\r\nUID:b\r\nSUMMARY:{evening}\r\nDTSTART;TZID=Europe/Berlin:{fmt(1, '19:30')}\r\nDTEND;TZID=Europe/Berlin:{fmt(1, '21:00')}\r\nEND:VEVENT\r\n"),
    }
    calendar_backup.take_snapshot(
        calendar_store_path, [{"name": "Sam", "url": "demo://Sam"}],
        lambda _url: {href: etag for href, (etag, _) in cloud.items()},
        lambda _url, hrefs: {href: cloud[href] for href in hrefs},
    )
    for seconds, ok in ((95, True), (140, True), (60, False), (210, True)):
        db.record_ai_run(database_path, seconds, ok, "demo-model")
    logger.info("Demo-Modus: Beispieldaten angelegt (%s).", language)

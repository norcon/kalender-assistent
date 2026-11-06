import re
import unicodedata
from email.utils import parseaddr
from typing import Any


_WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)
_STOP_WORDS = {
    "am",
    "an",
    "bei",
    "der",
    "die",
    "ein",
    "eine",
    "einer",
    "eines",
    "fuer",
    "im",
    "in",
    "mit",
    "nach",
    "neue",
    "und",
    "von",
    "zum",
    "zur",
}


def sender_key(sender: str) -> str:
    return parseaddr(sender)[1].strip().casefold()


def _title_tokens(title: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", title.casefold())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return {
        token
        for token in _WORD_PATTERN.findall(ascii_text)
        if len(token) > 2 and token not in _STOP_WORDS
    }


def title_similarity(left: str, right: str) -> float:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0
    if left_tokens == right_tokens:
        return 1
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def apply_person_corrections(
    events: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    sender: str,
) -> tuple[list[dict[str, Any]], int]:
    sender_address = sender_key(sender)
    if not sender_address:
        return events, 0

    matching_corrections = [
        correction
        for correction in corrections
        if sender_key(str(correction.get("source_sender", ""))) == sender_address
    ]
    if not matching_corrections:
        return events, 0

    corrected_events = []
    applied_count = 0
    for event in events:
        scored = [
            (
                title_similarity(
                    str(event.get("title", "")), str(correction["event_title"])
                ),
                correction["corrected_person"],
            )
            for correction in matching_corrections
        ]
        best_score = max((score for score, _ in scored), default=0)
        best_people = {
            person for score, person in scored if score == best_score and score >= 0.6
        }
        updated_event = dict(event)
        if updated_event.get("action", "new") == "new" and len(best_people) == 1:
            learned_person = next(iter(best_people))
            if updated_event.get("person") != learned_person:
                updated_event["person"] = learned_person
                updated_event["person_source"] = "learned"
                applied_count += 1
        corrected_events.append(updated_event)
    return corrected_events, applied_count

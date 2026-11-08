import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from .i18n import _
from .person_learning import title_similarity


SHARED_ACTOR = "Gemeinsamer Zugang (Person nicht feststellbar)"


def connect(database_path: str) -> sqlite3.Connection:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize(database_path: str) -> None:
    with connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS mailbox_state (
                mailbox_key TEXT PRIMARY KEY,
                uid_validity TEXT NOT NULL,
                last_uid INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_messages (
                source_key TEXT PRIMARY KEY,
                mailbox_key TEXT NOT NULL,
                uid INTEGER NOT NULL,
                message_id TEXT,
                processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                event_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT,
                location TEXT,
                person TEXT,
                notes TEXT,
                confidence REAL NOT NULL,
                source_subject TEXT NOT NULL,
                source_sender TEXT NOT NULL,
                source_received_at TEXT NOT NULL,
                source_part TEXT,
                assignment_source TEXT NOT NULL DEFAULT 'unassigned',
                possible_duplicate_of INTEGER REFERENCES suggestions(id),
                created_at TEXT NOT NULL,
                dismissed INTEGER NOT NULL DEFAULT 0,
                icloud_uid TEXT,
                icloud_calendar TEXT,
                icloud_exported_at TEXT,
                icloud_deleted_at TEXT,
                cancelled_at TEXT,
                UNIQUE(source_key, event_index)
            );
            CREATE INDEX IF NOT EXISTS suggestions_active
                ON suggestions(dismissed, starts_at);
            CREATE TABLE IF NOT EXISTS person_corrections (
                suggestion_id INTEGER PRIMARY KEY,
                source_sender TEXT NOT NULL,
                sender_key TEXT NOT NULL,
                source_subject TEXT NOT NULL,
                event_title TEXT NOT NULL,
                previous_person TEXT,
                corrected_person TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS person_corrections_sender
                ON person_corrections(source_sender);
            CREATE TABLE IF NOT EXISTS change_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('update', 'cancel')),
                event_data TEXT NOT NULL,
                related_suggestion_id INTEGER REFERENCES suggestions(id),
                source_subject TEXT NOT NULL,
                source_sender TEXT NOT NULL,
                source_received_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'applied', 'rejected')),
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS change_requests_pending
                ON change_requests(status, created_at);
            CREATE TABLE IF NOT EXISTS suggestion_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_id INTEGER NOT NULL REFERENCES suggestions(id),
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS suggestion_history_suggestion
                ON suggestion_history(suggestion_id, created_at);
            CREATE TABLE IF NOT EXISTS message_failures (
                source_key TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                last_error TEXT NOT NULL,
                source_subject TEXT NOT NULL,
                source_sender TEXT NOT NULL,
                source_received_at TEXT NOT NULL,
                last_failed_at TEXT NOT NULL,
                skipped_at TEXT
            );
            CREATE TABLE IF NOT EXISTS service_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_senders (
                pattern TEXT PRIMARY KEY,
                min_confidence INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ignored_senders (
                pattern TEXT PRIMARY KEY,
                example_sender TEXT NOT NULL DEFAULT '',
                example_subject TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS target_calendars (
                name TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                shared INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ai_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                seconds REAL NOT NULL,
                ok INTEGER NOT NULL,
                model TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ai_runs_started ON ai_runs(started_at);
            CREATE TABLE IF NOT EXISTS suggestion_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                version INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO suggestion_meta(singleton, version)
                VALUES (1, 0);
            CREATE TRIGGER IF NOT EXISTS suggestions_insert_revision
            AFTER INSERT ON suggestions
            BEGIN
                UPDATE suggestion_meta SET version = version + 1
                WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS suggestions_update_revision
            AFTER UPDATE ON suggestions
            BEGIN
                UPDATE suggestion_meta SET version = version + 1
                WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS change_requests_insert_revision
            AFTER INSERT ON change_requests
            BEGIN
                UPDATE suggestion_meta SET version = version + 1
                WHERE singleton = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS change_requests_update_revision
            AFTER UPDATE ON change_requests
            BEGIN
                UPDATE suggestion_meta SET version = version + 1
                WHERE singleton = 1;
            END;
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(suggestions)")
        }
        for column, definition in (
            ("icloud_uid", "TEXT"),
            ("icloud_calendar", "TEXT"),
            ("icloud_exported_at", "TEXT"),
            ("assignment_source", "TEXT NOT NULL DEFAULT 'unassigned'"),
            ("possible_duplicate_of", "INTEGER REFERENCES suggestions(id)"),
            ("icloud_deleted_at", "TEXT"),
            ("cancelled_at", "TEXT"),
            ("source_part", "TEXT"),
            ("keep_active", "INTEGER NOT NULL DEFAULT 0"),
            ("recurrence", "TEXT"),
            ("source_quote", "TEXT"),
            ("quote_source", "TEXT"),
            ("hidden_reason", "TEXT"),
            ("reminder", "TEXT"),
            ("dismiss_reason", "TEXT"),
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE suggestions ADD COLUMN {column} {definition}"
                )
        connection.execute(
            "UPDATE suggestions SET assignment_source = 'ai' "
            "WHERE person IS NOT NULL AND assignment_source = 'unassigned'"
        )
        message_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(processed_messages)")
        }
        if "message_id" not in message_columns:
            connection.execute(
                "ALTER TABLE processed_messages ADD COLUMN message_id TEXT"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS processed_messages_message_id "
            "ON processed_messages(message_id)"
        )
        sender_rule_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ignored_senders)")
        }
        if "mode" not in sender_rule_columns:
            # hide = ausblenden, show = immer anzeigen (nicht fragen, kein Newsletter-Filter)
            connection.execute(
                "ALTER TABLE ignored_senders ADD COLUMN mode TEXT NOT NULL DEFAULT 'hide'"
            )
        _drop_legacy_person_check(connection)
        correction_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(person_corrections)")
        }
        if "sender_key" not in correction_columns:
            connection.execute(
                "ALTER TABLE person_corrections ADD COLUMN sender_key TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                """
                UPDATE person_corrections
                SET sender_key = lower(trim(source_sender))
                WHERE sender_key = ''
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS person_corrections_sender_key "
            "ON person_corrections(sender_key)"
        )


def _drop_legacy_person_check(connection: sqlite3.Connection) -> None:
    """Ältere Datenbanken erlaubten nur fest eingebaute Kalendernamen."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'person_corrections'"
    ).fetchone()
    if not row or "CHECK(corrected_person IN" not in (row["sql"] or ""):
        return
    columns = [r["name"] for r in connection.execute("PRAGMA table_info(person_corrections)")]
    connection.execute("ALTER TABLE person_corrections RENAME TO person_corrections_old")
    connection.execute(
        """
        CREATE TABLE person_corrections (
            suggestion_id INTEGER PRIMARY KEY,
            source_sender TEXT NOT NULL,
            sender_key TEXT NOT NULL DEFAULT '',
            source_subject TEXT NOT NULL,
            event_title TEXT NOT NULL,
            previous_person TEXT,
            corrected_person TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    shared = [c for c in columns if c in {
        "suggestion_id", "source_sender", "sender_key", "source_subject",
        "event_title", "previous_person", "corrected_person", "created_at",
    }]
    names = ", ".join(shared)
    connection.execute(
        f"INSERT INTO person_corrections ({names}) SELECT {names} FROM person_corrections_old"
    )
    connection.execute("DROP TABLE person_corrections_old")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS person_corrections_sender ON person_corrections(source_sender)"
    )


def get_mailbox_state(database_path: str, mailbox_key: str) -> tuple[str | None, int]:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT uid_validity, last_uid FROM mailbox_state WHERE mailbox_key = ?",
            (mailbox_key,),
        ).fetchone()
    if row is None:
        return None, 0
    return row["uid_validity"], row["last_uid"]


def reset_mailbox_state(
    database_path: str, mailbox_key: str, uid_validity: str
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO mailbox_state(mailbox_key, uid_validity, last_uid)
            VALUES (?, ?, 0)
            ON CONFLICT(mailbox_key) DO UPDATE SET
                uid_validity = excluded.uid_validity,
                last_uid = 0
            """,
            (mailbox_key, uid_validity),
        )


def _normalize_message_id(value: str) -> str:
    return value.strip().strip("<>").strip().casefold()


def _normalized_start(value: str) -> str:
    try:
        if len(value) == 10:
            return value
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.replace(second=0, microsecond=0).isoformat()
    except ValueError:
        return value


def _find_possible_duplicate(
    connection: sqlite3.Connection, event: dict[str, Any]
) -> int | None:
    rows = connection.execute(
        "SELECT id, title, starts_at, person FROM suggestions "
        "WHERE cancelled_at IS NULL ORDER BY id DESC LIMIT 500"
    ).fetchall()
    matches = [
        row
        for row in rows
        if _normalized_start(str(row["starts_at"]))
        == _normalized_start(str(event["start"]))
        and (not row["person"] or not event.get("person") or row["person"] == event.get("person"))
        and title_similarity(str(row["title"]), str(event["title"])) >= 0.75
    ]
    return int(matches[0]["id"]) if matches else None


def _find_related_suggestion(
    connection: sqlite3.Connection,
    references: list[str],
    event_title: str,
) -> int | None:
    message_ids = [_normalize_message_id(value) for value in references if value.strip()]
    if not message_ids:
        return None
    placeholders = ",".join("?" for _ in message_ids)
    rows = connection.execute(
        f"""
        SELECT DISTINCT s.id, s.title
        FROM suggestions s
        JOIN processed_messages pm ON pm.source_key = s.source_key
        WHERE pm.message_id IN ({placeholders})
        """,
        message_ids,
    ).fetchall()
    if len(rows) == 1:
        return int(rows[0]["id"])
    scored = sorted(
        (
            (title_similarity(event_title, str(row["title"])), int(row["id"]))
            for row in rows
        ),
        reverse=True,
    )
    if scored and scored[0][0] >= 0.6 and (
        len(scored) == 1 or scored[0][0] > scored[1][0]
    ):
        return scored[0][1]
    return None


def _insert_suggestion(
    connection: sqlite3.Connection,
    source_key: str,
    index: int,
    event: dict[str, Any],
    source: dict[str, str],
    now: str,
    actor: str = "Automatische Erkennung",
) -> int:
    duplicate_of = _find_possible_duplicate(connection, event)
    inserted_suggestion = connection.execute(
        """
        INSERT INTO suggestions (
            source_key, event_index, title, starts_at, ends_at,
            location, person, notes, confidence, source_subject,
            source_sender, source_received_at, source_part, assignment_source,
            possible_duplicate_of, created_at, recurrence,
            source_quote, quote_source, hidden_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_key,
            index,
            event["title"],
            event["start"],
            event.get("end"),
            event.get("location"),
            event.get("person"),
            event.get("notes"),
            event["confidence"],
            source["subject"],
            source["sender"],
            source["received_at"],
            event.get("source_part"),
            event.get(
                "person_source",
                "ai" if event.get("person") else "unassigned",
            ),
            duplicate_of,
            now,
            _encode_recurrence(event.get("recurrence")),
            event.get("quote"),
            event.get("quote_source"),
            event.get("hidden_reason"),
        ),
    )
    connection.execute(
        """
        INSERT INTO suggestion_history
            (suggestion_id, action, actor, details, created_at)
        VALUES (?, 'created', ?, ?, ?)
        """,
        (
            inserted_suggestion.lastrowid,
            actor,
            json.dumps(
                {
                    "assignment_source": event.get(
                        "person_source",
                        "ai" if event.get("person") else "unassigned",
                    ),
                    "possible_duplicate_of": duplicate_of,
                }
            ),
            now,
        ),
    )
    return int(inserted_suggestion.lastrowid)


def save_processed_message(
    database_path: str,
    mailbox_key: str,
    uid_validity: str,
    uid: int,
    source_key: str,
    events: list[dict[str, Any]],
    source: dict[str, str],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        current = connection.execute(
            "SELECT uid_validity FROM mailbox_state WHERE mailbox_key = ?",
            (mailbox_key,),
        ).fetchone()
        if current is None or current["uid_validity"] != uid_validity:
            connection.execute(
                """
                INSERT INTO mailbox_state(mailbox_key, uid_validity, last_uid)
                VALUES (?, ?, ?)
                ON CONFLICT(mailbox_key) DO UPDATE SET
                    uid_validity = excluded.uid_validity,
                    last_uid = excluded.last_uid
                """,
                (mailbox_key, uid_validity, uid),
            )
        else:
            connection.execute(
                """
                UPDATE mailbox_state SET last_uid = MAX(last_uid, ?)
                WHERE mailbox_key = ?
                """,
                (uid, mailbox_key),
            )

        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO processed_messages
                (source_key, mailbox_key, uid, message_id, processed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                source_key,
                mailbox_key,
                uid,
                _normalize_message_id(source.get("message_id", "")) or None,
                now,
            ),
        ).rowcount
        if inserted:
            references = source.get("references", "").split()
            for index, event in enumerate(events):
                action = event.get("action", "new")
                if action in {"update", "cancel"}:
                    related_id = _find_related_suggestion(
                        connection, references, str(event["title"])
                    )
                    connection.execute(
                        """
                        INSERT INTO change_requests (
                            source_key, action, event_data, related_suggestion_id,
                            source_subject, source_sender, source_received_at,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_key,
                            action,
                            json.dumps(event, ensure_ascii=False),
                            related_id,
                            source["subject"],
                            source["sender"],
                            source["received_at"],
                            now,
                        ),
                    )
                    continue
                _insert_suggestion(connection, source_key, index, event, source, now)


def set_service_state(database_path: str, **values: str) -> None:
    with connect(database_path) as connection:
        connection.executemany(
            "INSERT INTO service_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )


def delete_service_state(database_path: str, *keys: str) -> None:
    with connect(database_path) as connection:
        connection.executemany("DELETE FROM service_state WHERE key = ?", [(key,) for key in keys])


def get_service_state(database_path: str) -> dict[str, str]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT key, value FROM service_state").fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_app_settings(database_path: str) -> dict[str, str]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def save_app_settings(database_path: str, values: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            [(key, str(value), now) for key, value in values.items()],
        )


def reset_app_settings(database_path: str) -> None:
    with connect(database_path) as connection:
        connection.execute("DELETE FROM app_settings")


def record_message_failure(
    database_path: str, source_key: str, error: str, source: dict[str, str]
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        row = connection.execute(
            """
            INSERT INTO message_failures (
                source_key, attempts, last_error, source_subject,
                source_sender, source_received_at, last_failed_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                attempts = attempts + 1,
                last_error = excluded.last_error,
                last_failed_at = excluded.last_failed_at
            RETURNING attempts
            """,
            (
                source_key,
                error[:500],
                source.get("subject", ""),
                source.get("sender", ""),
                source.get("received_at", ""),
                now,
            ),
        ).fetchone()
    return int(row["attempts"])


def mark_message_skipped(database_path: str, source_key: str) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE message_failures SET skipped_at = ? WHERE source_key = ?",
            (datetime.now(timezone.utc).isoformat(), source_key),
        )


def list_skipped_messages(database_path: str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT source_key, source_subject, source_sender, source_received_at,
                   attempts, last_error, skipped_at
            FROM message_failures
            WHERE skipped_at IS NOT NULL
            ORDER BY skipped_at DESC
            LIMIT 50
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _encode_recurrence(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value else None


def _decode_suggestion(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw = result.get("recurrence")
    try:
        result["recurrence"] = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        result["recurrence"] = None
    return result


def list_suggestions(database_path: str, include_dismissed: bool = False):
    with connect(database_path) as connection:
        if include_dismissed:
            rows = connection.execute(
                """
                SELECT s.*, duplicate.title AS duplicate_title,
                       duplicate.dismissed AS duplicate_dismissed
                FROM suggestions s
                LEFT JOIN suggestions duplicate
                    ON duplicate.id = s.possible_duplicate_of
                ORDER BY s.starts_at, s.id
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT s.*, duplicate.title AS duplicate_title,
                       duplicate.dismissed AS duplicate_dismissed
                FROM suggestions s
                LEFT JOIN suggestions duplicate
                    ON duplicate.id = s.possible_duplicate_of
                WHERE s.dismissed = 0
                ORDER BY s.starts_at, s.id
                """
            ).fetchall()
    return [_decode_suggestion(row) for row in rows]


def max_suggestion_id(database_path: str) -> int:
    with connect(database_path) as connection:
        return int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM suggestions").fetchone()[0])


def new_open_suggestions(database_path: str, after_id: int) -> list[dict[str, Any]]:
    """Seit after_id neu angelegte, sichtbare Vorschläge (für Benachrichtigungen)."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, title, starts_at FROM suggestions WHERE id > ? AND dismissed = 0 "
            "AND hidden_reason IS NULL ORDER BY starts_at, id",
            (after_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_suggestion_version(database_path: str) -> int:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT version FROM suggestion_meta WHERE singleton = 1"
        ).fetchone()
    return int(row["version"])


def get_suggestion(database_path: str, suggestion_id: int) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
    return _decode_suggestion(row) if row else None


def update_suggestion(
    database_path: str,
    suggestion_id: int,
    values: dict[str, Any],
    *,
    learn_person_correction: bool = False,
) -> bool:
    with connect(database_path) as connection:
        existing = connection.execute(
            "SELECT source_sender, source_subject, assignment_source "
            "FROM suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if existing is None:
            return False
        cursor = connection.execute(
            """
            UPDATE suggestions
            SET title = ?, starts_at = ?, ends_at = ?, location = ?,
                person = ?, notes = ?, assignment_source = ?,
                recurrence = CASE WHEN ? THEN ? ELSE recurrence END
            WHERE id = ?
            """,
            (
                values["title"],
                values["start"],
                values.get("end"),
                values.get("location"),
                values.get("person"),
                values.get("notes"),
                (
                    values.get("person_source", "manual")
                    if values.get("person") != values.get("previous_person")
                    else existing["assignment_source"]
                ),
                int("recurrence" in values),
                _encode_recurrence(values.get("recurrence")),
                suggestion_id,
            ),
        )
        if cursor.rowcount and learn_person_correction:
            connection.execute(
                """
                INSERT INTO person_corrections (
                    suggestion_id, source_sender, sender_key, source_subject,
                    event_title, previous_person, corrected_person, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(suggestion_id) DO UPDATE SET
                    source_sender = excluded.source_sender,
                    sender_key = excluded.sender_key,
                    source_subject = excluded.source_subject,
                    event_title = excluded.event_title,
                    previous_person = excluded.previous_person,
                    corrected_person = excluded.corrected_person,
                    created_at = excluded.created_at
                """,
                (
                    suggestion_id,
                    existing["source_sender"],
                    parseaddr(existing["source_sender"])[1].strip().casefold(),
                    existing["source_subject"],
                    values["title"],
                    values.get("previous_person"),
                    values["person"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    return cursor.rowcount > 0


def list_person_corrections(database_path: str, sender: str) -> list[dict[str, Any]]:
    sender_key = parseaddr(sender)[1].strip().casefold()
    if not sender_key:
        return []
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT source_sender, sender_key, source_subject, event_title,
                   corrected_person
            FROM person_corrections
            WHERE sender_key = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (sender_key,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_person_corrections(database_path: str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT suggestion_id, source_sender, source_subject, event_title,
                   previous_person, corrected_person, created_at
            FROM person_corrections
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def delete_person_correction(database_path: str, suggestion_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM person_corrections WHERE suggestion_id = ?",
            (suggestion_id,),
        )
    return cursor.rowcount > 0


def add_suggestion_history(
    database_path: str,
    suggestion_id: int,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    with connect(database_path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        if exists is None:
            return
        connection.execute(
            """
            INSERT INTO suggestion_history
                (suggestion_id, action, actor, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                suggestion_id,
                action,
                SHARED_ACTOR,
                json.dumps(details or {}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


ARCHIVE_ACTIONS = ("archived", "auto_archived", "exported", "cancelled")


def archive_events(database_path: str) -> dict[int, dict[str, str]]:
    """Je Vorschlag der letzte Archivierungsgrund und -zeitpunkt (eine Abfrage)."""
    placeholders = ",".join("?" for _ in ARCHIVE_ACTIONS)
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT h.suggestion_id, h.action, h.created_at
            FROM suggestion_history h
            JOIN (
                SELECT suggestion_id, MAX(id) AS last_id
                FROM suggestion_history
                WHERE action IN ({placeholders})
                GROUP BY suggestion_id
            ) last ON last.last_id = h.id
            """,
            ARCHIVE_ACTIONS,
        ).fetchall()
    return {
        int(row["suggestion_id"]): {"action": row["action"], "at": row["created_at"]}
        for row in rows
    }


def list_suggestion_history(
    database_path: str, suggestion_id: int
) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT action, actor, details, created_at
            FROM suggestion_history
            WHERE suggestion_id = ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (suggestion_id,),
        ).fetchall()
    return [
        {**dict(row), "details": json.loads(row["details"])}
        for row in rows
    ]


def list_change_requests(
    database_path: str, *, include_resolved: bool = False
) -> list[dict[str, Any]]:
    where_clause = "" if include_resolved else "WHERE cr.status = 'pending'"
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT cr.*, s.title AS current_title, s.starts_at AS current_start,
                   s.icloud_calendar, s.icloud_exported_at,
                   s.recurrence IS NOT NULL AS current_is_series
            FROM change_requests cr
            LEFT JOIN suggestions s ON s.id = cr.related_suggestion_id
            {where_clause}
            ORDER BY cr.created_at DESC
            """
        ).fetchall()
    return [
        {**dict(row), "event_data": json.loads(row["event_data"])}
        for row in rows
    ]


def get_change_request(database_path: str, request_id: int) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM change_requests WHERE id = ?", (request_id,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["event_data"] = json.loads(result["event_data"])
    return result


def reopen_change_request(database_path: str, request_id: int) -> bool:
    """Ablehnung rückgängig machen (nur abgelehnte, nicht übernommene)."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE change_requests SET status = 'pending', resolved_at = NULL "
            "WHERE id = ? AND status = 'rejected'",
            (request_id,),
        )
    return cursor.rowcount > 0


def resolve_change_request(
    database_path: str, request_id: int, status: str
) -> bool:
    if status not in {"applied", "rejected"}:
        raise ValueError("Ungültiger Status für Änderungsanfrage")
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE change_requests
            SET status = ?, resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, now, request_id),
        )
    return cursor.rowcount > 0


def link_change_request(
    database_path: str, request_id: int, suggestion_id: int
) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE change_requests
            SET related_suggestion_id = ?
            WHERE id = ? AND status = 'pending'
              AND EXISTS (
                  SELECT 1 FROM suggestions
                  WHERE id = ? AND cancelled_at IS NULL
              )
            """,
            (suggestion_id, request_id, suggestion_id),
        )
    return cursor.rowcount > 0


def mark_suggestion_cancelled(
    database_path: str, suggestion_id: int, *, icloud_deleted: bool
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE suggestions
            SET dismissed = 1, cancelled_at = ?, icloud_deleted_at =
                CASE WHEN ? THEN ? ELSE icloud_deleted_at END
            WHERE id = ? AND cancelled_at IS NULL
            """,
            (now, int(icloud_deleted), now, suggestion_id),
        )
    if cursor.rowcount:
        add_suggestion_history(
            database_path,
            suggestion_id,
            "cancelled",
            {"icloud_deleted": icloud_deleted},
        )
    return cursor.rowcount > 0


def dismiss_suggestion(database_path: str, suggestion_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE suggestions SET dismissed = 1 WHERE id = ?",
            (suggestion_id,),
        )
    if cursor.rowcount:
        add_suggestion_history(database_path, suggestion_id, "archived")
    return cursor.rowcount > 0


# Gründe beim Entfernen; die ersten beiden lernt die KI als Gegenbeispiele.
DISMISS_REASONS = ("kein_termin", "nicht_relevant", "doppelt", "falsch")
LEARNING_REASONS = ("kein_termin", "nicht_relevant")


def set_dismiss_reason(database_path: str, suggestion_id: int, reason: str) -> bool:
    if reason not in DISMISS_REASONS:
        raise ValueError(reason)
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE suggestions SET dismiss_reason = ? WHERE id = ? AND dismissed = 1",
            (reason, suggestion_id),
        )
    if cursor.rowcount:
        add_suggestion_history(database_path, suggestion_id, "dismiss_reason", {"reason": reason})
    return cursor.rowcount > 0


def recent_rejections(database_path: str, limit: int = 20, days: int = 180) -> list[dict[str, Any]]:
    """Zuletzt als „kein Termin“/„nicht für uns“ abgelehnte Vorschläge (für den KI-Prompt)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        return _recent_rejections(database_path, since, limit)
    except sqlite3.OperationalError:  # Datenbank noch nicht angelegt
        return []


def _recent_rejections(database_path: str, since: str, limit: int) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT title, source_subject, source_sender, dismiss_reason
            FROM suggestions
            WHERE dismissed = 1 AND dismiss_reason IN ({",".join("?" * len(LEARNING_REASONS))})
              AND created_at >= ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (*LEARNING_REASONS, since, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def statistics(database_path: str, since: str) -> dict[str, Any]:
    """Ergebnisse der Vorschläge je Monat (Erkennungsmonat), Gründe, Summen."""
    with connect(database_path) as connection:
        months = [dict(row) for row in connection.execute(
            """
            SELECT substr(s.created_at, 1, 7) AS month,
                   COUNT(*) AS total,
                   SUM(s.icloud_exported_at IS NOT NULL) AS exported,
                   SUM(s.icloud_exported_at IS NULL AND s.hidden_reason IS NOT NULL) AS hidden,
                   SUM(s.icloud_exported_at IS NULL AND s.hidden_reason IS NULL AND s.dismissed = 1
                       AND s.cancelled_at IS NULL AND EXISTS (
                           SELECT 1 FROM suggestion_history h
                           WHERE h.suggestion_id = s.id AND h.action = 'archived')) AS rejected,
                   SUM(s.icloud_exported_at IS NULL AND s.hidden_reason IS NULL AND s.dismissed = 0) AS open
            FROM suggestions s
            WHERE s.created_at >= ?
            GROUP BY month ORDER BY month DESC
            """,
            (since,),
        )]
        reasons = {row["dismiss_reason"]: row["n"] for row in connection.execute(
            "SELECT dismiss_reason, COUNT(*) AS n FROM suggestions "
            "WHERE dismiss_reason IS NOT NULL AND created_at >= ? GROUP BY dismiss_reason",
            (since,),
        )}
    for row in months:
        row["other"] = row["total"] - row["exported"] - row["hidden"] - row["rejected"] - row["open"]
    keys = ("total", "exported", "hidden", "rejected", "open", "other")
    totals = {key: sum(row[key] for row in months) for key in keys}
    return {"months": months, "reasons": reasons, "totals": totals}


def auto_archive_suggestions(database_path: str, suggestion_ids: list[int]) -> int:
    archived = 0
    for suggestion_id in suggestion_ids:
        with connect(database_path) as connection:
            cursor = connection.execute(
                "UPDATE suggestions SET dismissed = 1 "
                "WHERE id = ? AND dismissed = 0 AND keep_active = 0",
                (suggestion_id,),
            )
        if cursor.rowcount:
            add_suggestion_history(database_path, suggestion_id, "auto_archived")
            archived += 1
    return archived


def restore_suggestion(
    database_path: str, suggestion_id: int, *, keep_active: bool = True
) -> bool:
    """Aus dem Archiv zurückholen.

    keep_active=True (bewusst aus dem Archiv geholt): nicht erneut automatisch
    archivieren. False (Rückgängig direkt nach dem Entfernen): normaler Zustand.
    """
    if keep_active:
        query = (
            "UPDATE suggestions SET dismissed = 0, keep_active = 1, "
            "hidden_reason = NULL, dismiss_reason = NULL WHERE id = ?"
        )
    else:
        query = (
            "UPDATE suggestions SET dismissed = 0, dismiss_reason = NULL "
            "WHERE id = ? AND dismissed = 1 "
            "AND cancelled_at IS NULL AND icloud_exported_at IS NULL"
        )
    with connect(database_path) as connection:
        cursor = connection.execute(query, (suggestion_id,))
    if cursor.rowcount:
        add_suggestion_history(
            database_path, suggestion_id, "restored" if keep_active else "dismiss_undone"
        )
    return cursor.rowcount > 0


def mark_suggestion_exported(
    database_path: str,
    suggestion_id: int,
    uid: str,
    calendar_name: str,
) -> bool:
    exported_at = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE suggestions
            SET icloud_uid = ?, icloud_calendar = ?, icloud_exported_at = ?,
                dismissed = 1
            WHERE id = ? AND dismissed = 0 AND icloud_exported_at IS NULL
            """,
            (uid, calendar_name, exported_at, suggestion_id),
        )
    if cursor.rowcount:
        add_suggestion_history(
            database_path,
            suggestion_id,
            "exported",
            {"calendar": calendar_name},
        )
    return cursor.rowcount > 0


# ---------- Ausgeblendete Absender ----------

NEWSLETTER_REASON = "newsletter"
_MULTI_PART_SUFFIXES = {"co", "com", "org", "net", "ac", "gv", "or", "ne"}


def sender_address(sender: str) -> str:
    return parseaddr(sender or "")[1].strip().casefold()


def sender_domain(sender: str) -> str:
    """Registrierbare Domain, z. B. paketankuendigung.myhermes.de → myhermes.de."""
    address = sender_address(sender)
    host = address.rpartition("@")[2]
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return host
    keep = 3 if (
        len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _MULTI_PART_SUFFIXES
    ) else 2
    return ".".join(labels[-keep:])


def normalize_sender_pattern(value: str) -> str:
    value = (value or "").strip().casefold().strip("<>").lstrip("@")
    if "@" in value:
        value = sender_address(value)
    if not value or " " in value or "." not in value or len(value) > 254:
        raise ValueError(_("Bitte eine E-Mail-Adresse oder Domain angeben."))
    return value


def sender_matches(sender: str, pattern: str) -> bool:
    address = sender_address(sender)
    if not address:
        return False
    if "@" in pattern:
        return address == pattern
    host = address.rpartition("@")[2]
    return host == pattern or host.endswith("." + pattern)


AUTO_THRESHOLDS = (90, 95, 98)


def list_auto_senders(database_path: str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT pattern, min_confidence, created_at FROM auto_senders ORDER BY pattern"
        )]


def set_auto_sender(database_path: str, pattern: str, min_confidence: int) -> str:
    pattern = normalize_sender_pattern(pattern)
    if min_confidence not in AUTO_THRESHOLDS:
        raise ValueError(_("Bitte eine der angebotenen Schwellen wählen."))
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO auto_senders(pattern, min_confidence, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(pattern) DO UPDATE SET min_confidence = excluded.min_confidence",
            (pattern, min_confidence, datetime.now(timezone.utc).isoformat()),
        )
    return pattern


def remove_auto_sender(database_path: str, pattern: str) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute("DELETE FROM auto_senders WHERE pattern = ?", (pattern.strip().casefold(),))
    return cursor.rowcount > 0


def auto_export_candidates(database_path: str, after_id: int, now: str) -> list[dict[str, Any]]:
    """Neue Vorschläge, die eine Auto-Übernahme-Regel eindeutig erfüllen.

    Nur mit Zielkalender, ohne möglichen Doppeltermin, nicht ausgeblendet, nicht
    vorbei und mit ausreichender Sicherheit – im Zweifel bleibt es ein Vorschlag.
    """
    rules = list_auto_senders(database_path)
    if not rules:
        return []
    with connect(database_path) as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT id, title, person, confidence, source_sender, starts_at FROM suggestions "
            "WHERE id > ? AND dismissed = 0 AND hidden_reason IS NULL AND icloud_exported_at IS NULL "
            "AND cancelled_at IS NULL AND person IS NOT NULL AND person != '' "
            "AND possible_duplicate_of IS NULL AND substr(starts_at, 1, 10) >= substr(?, 1, 10) "
            "ORDER BY id",
            (after_id, now),
        )]
    found = []
    for row in rows:
        rule = next((r for r in rules if sender_matches(row["source_sender"], r["pattern"])), None)
        if rule and round(float(row["confidence"]) * 100) >= rule["min_confidence"]:
            found.append({**row, "rule": rule["pattern"], "min_confidence": rule["min_confidence"]})
    return found


def list_ignored_senders(database_path: str, mode: str = "hide") -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM ignored_senders WHERE mode = ? ORDER BY pattern", (mode,)
        ).fetchall()
    return [dict(row) for row in rows]


def matching_ignore_rule(
    database_path: str, sender: str, mode: str = "hide"
) -> str | None:
    for rule in list_ignored_senders(database_path, mode):
        if sender_matches(sender, rule["pattern"]):
            return rule["pattern"]
    return None


def add_trusted_sender(
    database_path: str, pattern: str, example_sender: str = "", example_subject: str = ""
) -> tuple[str, bool, list[int]]:
    """Regel „immer anzeigen“: keine Ausblende-Frage, kein Newsletter-Filter.

    Als Newsletter ausgeblendete Vorschläge dieses Absenders werden eingeblendet.
    """
    pattern = normalize_sender_pattern(pattern)
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        existing = connection.execute(
            "SELECT mode FROM ignored_senders WHERE pattern = ?", (pattern,)
        ).fetchone()
        if existing is not None and existing["mode"] == "hide":
            raise ValueError(_("{pattern} ist ausgeblendet. Bitte zuerst wieder zulassen.", pattern=pattern))
        created = connection.execute(
            """
            INSERT OR IGNORE INTO ignored_senders
                (pattern, example_sender, example_subject, created_at, mode)
            VALUES (?, ?, ?, ?, 'show')
            """,
            (pattern, example_sender or "", example_subject or "", now),
        ).rowcount
        rows = connection.execute(
            "SELECT id, source_sender FROM suggestions WHERE hidden_reason = ?",
            (NEWSLETTER_REASON,),
        ).fetchall()
        shown = [row["id"] for row in rows if sender_matches(row["source_sender"], pattern)]
        for suggestion_id in shown:
            connection.execute(
                "UPDATE suggestions SET hidden_reason = NULL WHERE id = ?", (suggestion_id,)
            )
    for suggestion_id in shown:
        add_suggestion_history(database_path, suggestion_id, "unhidden", {"rule": pattern})
    return pattern, bool(created), shown


def sender_is_trusted(database_path: str, sender: str) -> bool:
    """Absender mit Regel „immer anzeigen“ oder mit schon übertragenen Terminen."""
    if matching_ignore_rule(database_path, sender, "show"):
        return True
    domain = sender_domain(sender)
    if not domain:
        return False
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source_sender FROM suggestions WHERE icloud_exported_at IS NOT NULL"
        ).fetchall()
    return any(sender_domain(row["source_sender"]) == domain for row in rows)


def sender_stats(database_path: str) -> dict[str, dict[str, int]]:
    """Je Absender-Domain: Vorschläge gesamt, übertragen, von Hand entfernt."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT s.source_sender, s.icloud_exported_at, s.dismissed, s.cancelled_at,
                   EXISTS (
                       SELECT 1 FROM suggestion_history h
                       WHERE h.suggestion_id = s.id AND h.action = 'archived'
                   ) AS removed_by_hand
            FROM suggestions s
            """
        ).fetchall()
    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        domain = sender_domain(row["source_sender"])
        if not domain:
            continue
        entry = stats.setdefault(domain, {"total": 0, "exported": 0, "removed": 0})
        entry["total"] += 1
        if row["icloud_exported_at"]:
            entry["exported"] += 1
        elif row["dismissed"] and row["removed_by_hand"] and not row["cancelled_at"]:
            entry["removed"] += 1
    return stats


def _set_hidden_for_rule(
    connection: sqlite3.Connection, pattern: str, hidden: bool
) -> list[int]:
    if hidden:
        rows = connection.execute(
            "SELECT id, source_sender FROM suggestions "
            "WHERE dismissed = 0 AND hidden_reason IS NULL"
        ).fetchall()
        ids = [row["id"] for row in rows if sender_matches(row["source_sender"], pattern)]
        value: str | None = pattern
    else:
        ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM suggestions WHERE hidden_reason = ?", (pattern,)
            )
        ]
        value = None
    for suggestion_id in ids:
        connection.execute(
            "UPDATE suggestions SET hidden_reason = ? WHERE id = ?",
            (value, suggestion_id),
        )
    return ids


def add_ignored_sender(
    database_path: str, pattern: str, example_sender: str = "", example_subject: str = ""
) -> tuple[str, bool, list[int]]:
    """Regel anlegen und offene Vorschläge dieses Absenders ausblenden.

    Rückgabe: (Muster, neu angelegt?, IDs der ausgeblendeten Vorschläge).
    """
    pattern = normalize_sender_pattern(pattern)
    now = datetime.now(timezone.utc).isoformat()
    with connect(database_path) as connection:
        existing = connection.execute(
            "SELECT mode FROM ignored_senders WHERE pattern = ?", (pattern,)
        ).fetchone()
        created = existing is None or existing["mode"] != "hide"
        connection.execute(
            """
            INSERT INTO ignored_senders
                (pattern, example_sender, example_subject, created_at, mode)
            VALUES (?, ?, ?, ?, 'hide')
            ON CONFLICT(pattern) DO UPDATE SET
                mode = 'hide',
                example_sender = excluded.example_sender,
                example_subject = excluded.example_subject,
                created_at = excluded.created_at
            WHERE ignored_senders.mode != 'hide'
            """,
            (pattern, example_sender or "", example_subject or "", now),
        )
        hidden = _set_hidden_for_rule(connection, pattern, True)
    for suggestion_id in hidden:
        add_suggestion_history(
            database_path, suggestion_id, "hidden", {"rule": pattern}
        )
    return pattern, bool(created), hidden


def remove_ignored_sender(database_path: str, pattern: str) -> tuple[bool, list[int]]:
    """Regel löschen; davon ausgeblendete Vorschläge wieder einblenden."""
    with connect(database_path) as connection:
        removed = connection.execute(
            "DELETE FROM ignored_senders WHERE pattern = ?", (pattern,)
        ).rowcount
        shown = _set_hidden_for_rule(connection, pattern, False)
    for suggestion_id in shown:
        add_suggestion_history(
            database_path, suggestion_id, "unhidden", {"rule": pattern}
        )
    return bool(removed), shown


def unhide_suggestion(database_path: str, suggestion_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE suggestions SET hidden_reason = NULL "
            "WHERE id = ? AND hidden_reason IS NOT NULL",
            (suggestion_id,),
        )
    if cursor.rowcount:
        add_suggestion_history(database_path, suggestion_id, "unhidden")
    return cursor.rowcount > 0


# ---------- Erneute Auswertung einer Mail ----------

REANALYSIS_ACTOR = "Erneute Auswertung"


def source_known(database_path: str, source_key: str) -> bool:
    with connect(database_path) as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM suggestions WHERE source_key = ? "
                "UNION SELECT 1 FROM message_failures WHERE source_key = ? "
                "UNION SELECT 1 FROM processed_messages WHERE source_key = ?",
                (source_key, source_key, source_key),
            ).fetchone()
        )


def add_reanalysis_events(
    database_path: str, source_key: str, events: list[dict[str, Any]], source: dict[str, str]
) -> dict[str, Any]:
    """Neue Funde einer erneuten Auswertung ergänzen – Bestehendes bleibt unverändert.

    Übersprungen werden Funde, die es aus derselben Mail schon gibt (gleicher
    Beginn oder ähnlicher Titel am selben Tag), sowie Änderungen/Absagen.
    """
    now = datetime.now(timezone.utc).isoformat()
    added: list[int] = []
    known = 0
    changes = 0
    with connect(database_path) as connection:
        existing = connection.execute(
            "SELECT title, starts_at, event_index FROM suggestions WHERE source_key = ?",
            (source_key,),
        ).fetchall()
        next_index = max((row["event_index"] for row in existing), default=-1) + 1
        for event in events:
            if event.get("action", "new") != "new":
                changes += 1
                continue
            start = str(event["start"])
            if any(
                row["starts_at"][:16] == start[:16]
                or (
                    row["starts_at"][:10] == start[:10]
                    and title_similarity(str(row["title"]), str(event["title"])) >= 0.6
                )
                for row in existing
            ):
                known += 1
                continue
            added.append(
                _insert_suggestion(
                    connection, source_key, next_index, event, source, now, REANALYSIS_ACTOR
                )
            )
            next_index += 1
        connection.execute("DELETE FROM message_failures WHERE source_key = ?", (source_key,))
    return {"added": added, "known": known, "changes": changes}


def set_suggestion_reminder(
    database_path: str, suggestion_id: int, reminder: str | None
) -> bool:
    """None = Standard aus den Einstellungen, "none" = keine, sonst Minuten vorher."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE suggestions SET reminder = ? WHERE id = ? AND dismissed = 0",
            (reminder, suggestion_id),
        )
    if cursor.rowcount:
        add_suggestion_history(
            database_path, suggestion_id, "reminder_changed", {"reminder": reminder}
        )
    return cursor.rowcount > 0


# ---------- Kennzahlen für die Wartungs-Übersicht ----------

AI_RUNS_KEEP_DAYS = 400


def record_ai_run(database_path: str, seconds: float, ok: bool, model: str) -> None:
    now = datetime.now(timezone.utc)
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO ai_runs (started_at, seconds, ok, model) VALUES (?, ?, ?, ?)",
            (now.isoformat(), round(seconds, 1), int(ok), model[:200]),
        )
        connection.execute(
            "DELETE FROM ai_runs WHERE started_at < ?",
            ((now - timedelta(days=AI_RUNS_KEEP_DAYS)).isoformat(),),
        )


def maintenance_rows(database_path: str, since: str) -> dict[str, list[dict[str, Any]]]:
    """Rohdaten ab Zeitpunkt `since` (UTC-ISO); Auswertung in app.py."""
    with connect(database_path) as connection:
        return {
            "mails": [dict(r) for r in connection.execute(
                "SELECT processed_at AS at FROM processed_messages WHERE processed_at >= ?", (since,))],
            "suggestions": [dict(r) for r in connection.execute(
                "SELECT created_at AS at FROM suggestions WHERE created_at >= ?", (since,))],
            "exports": [dict(r) for r in connection.execute(
                "SELECT icloud_exported_at AS at FROM suggestions WHERE icloud_exported_at >= ?", (since,))],
            "ai_runs": [dict(r) for r in connection.execute(
                "SELECT started_at AS at, seconds, ok, model FROM ai_runs WHERE started_at >= ?", (since,))],
            "failures": [dict(r) for r in connection.execute(
                "SELECT last_failed_at AS at, skipped_at FROM message_failures WHERE last_failed_at >= ?", (since,))],
        }


def inventory(database_path: str) -> dict[str, int]:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT
                SUM(dismissed = 0 AND hidden_reason IS NULL) AS open,
                SUM(dismissed = 0 AND hidden_reason IS NOT NULL) AS hidden,
                SUM(dismissed = 1) AS archived,
                SUM(icloud_exported_at IS NOT NULL) AS exported,
                COUNT(*) AS total
            FROM suggestions
            """
        ).fetchone()
        rules = connection.execute(
            "SELECT COUNT(*) FROM ignored_senders"
        ).fetchone()[0]
        corrections = connection.execute("SELECT COUNT(*) FROM person_corrections").fetchone()[0]
        mails = connection.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]
        skipped = connection.execute(
            "SELECT COUNT(*) FROM message_failures WHERE skipped_at IS NOT NULL"
        ).fetchone()[0]
    return {
        **{key: int(row[key] or 0) for key in ("open", "hidden", "archived", "exported", "total")},
        "sender_rules": int(rules),
        "corrections": int(corrections),
        "mails": int(mails),
        "skipped": int(skipped),
    }


# ---------- Zielkalender ----------

_SHARED_HINTS = {"familie", "family", "gemeinsam", "haushalt", "household", "home", "zuhause"}


def list_target_calendars(database_path: str) -> list[dict[str, Any]]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name, description, shared FROM target_calendars ORDER BY position, name"
        ).fetchall()
    return [
        {"name": row["name"], "description": row["description"], "shared": bool(row["shared"])}
        for row in rows
    ]


def save_target_calendars(database_path: str, calendars: list[dict[str, Any]]) -> None:
    with connect(database_path) as connection:
        connection.execute("DELETE FROM target_calendars")
        connection.executemany(
            "INSERT INTO target_calendars (name, position, description, shared) VALUES (?, ?, ?, ?)",
            [
                (
                    str(item["name"]).strip()[:100],
                    position,
                    str(item.get("description") or "").strip()[:500],
                    int(bool(item.get("shared"))),
                )
                for position, item in enumerate(calendars)
                if str(item.get("name") or "").strip()
            ],
        )


def seed_target_calendars(
    database_path: str, known_calendars: list[str], preset: list[str] | None = None
) -> list[str]:
    """Erstbelegung der Zielkalender – nur, solange noch keine eingerichtet sind.

    Vorrang hat `preset` (TARGET_CALENDARS aus .env älterer Installationen), sonst die Kalender, die in
    den bisherigen Daten schon verwendet wurden. Gibt die übernommenen Namen zurück.
    """
    with connect(database_path) as connection:
        if connection.execute("SELECT 1 FROM target_calendars LIMIT 1").fetchone():
            return []
    names = [name.strip() for name in (preset or []) if name.strip()]
    if names:
        save_target_calendars(
            database_path,
            [{"name": name, "shared": name.casefold() in _SHARED_HINTS} for name in dict.fromkeys(names)],
        )
        return list(dict.fromkeys(names))
    with connect(database_path) as connection:
        used = {
            row[0]
            for row in connection.execute(
                "SELECT person FROM suggestions WHERE person IS NOT NULL "
                "UNION SELECT icloud_calendar FROM suggestions WHERE icloud_calendar IS NOT NULL "
                "UNION SELECT corrected_person FROM person_corrections"
            )
            if row[0]
        }
    if not used:
        return []
    order = {name: index for index, name in enumerate(known_calendars)}
    names = sorted(
        used,
        key=lambda name: (name.casefold() in _SHARED_HINTS, order.get(name, len(order)), name.casefold()),
    )
    save_target_calendars(
        database_path,
        [{"name": name, "shared": name.casefold() in _SHARED_HINTS} for name in names],
    )
    return names

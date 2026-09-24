import sqlite3
from pathlib import Path

import pytest

from app import db


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "test.db"
    db.init_db(path)
    with db.connect(path) as c:
        yield c


def test_insert_and_get_note(conn):
    note_id = db.insert_note(conn, source="import", type_="text", created_at="2026-01-01T00:00:00Z", raw_text="hello")
    note = db.get_note(conn, note_id)
    assert note["raw_text"] == "hello"
    assert note["status"] == "new"


def test_reject_parks_note_never_deletes(conn):
    note_id = db.insert_note(conn, source="import", type_="text", created_at="2026-01-01T00:00:00Z", raw_text="hello")
    db.set_note_status(conn, note_id, "parked", park_reason="not ready")
    note = db.get_note(conn, note_id)
    assert note["status"] == "parked"
    assert note["park_reason"] == "not ready"
    # still exists, still queryable - nothing is deleted
    parked = db.notes_with_status(conn, "parked")
    assert len(parked) == 1


def test_notes_with_status_filters_correctly(conn):
    a = db.insert_note(conn, source="import", type_="text", created_at="2026-01-01T00:00:00Z", raw_text="a")
    b = db.insert_note(conn, source="import", type_="text", created_at="2026-01-02T00:00:00Z", raw_text="b")
    db.set_note_status(conn, b, "used")
    new_only = db.notes_with_status(conn, "new")
    assert [n["id"] for n in new_only] == [a]


def test_pending_input_roundtrip(conn):
    note_id = db.insert_note(conn, source="import", type_="text", created_at="2026-01-01T00:00:00Z", raw_text="a")
    draft_id = db.insert_draft(
        conn, note_id=note_id, pick_id=None, version=0, parent_draft_id=None,
        text="draft text", news_item_id=None, verify_count=0, word_count=2,
        selfcheck_notes=None, lint_warnings=[],
    )
    db.set_pending_input(conn, chat_id=42, draft_id=draft_id, kind="revise")
    pending = db.get_pending_input(conn, 42)
    assert pending["draft_id"] == draft_id
    assert pending["kind"] == "revise"
    db.clear_pending_input(conn, 42)
    assert db.get_pending_input(conn, 42) is None


def test_draft_version_count(conn):
    note_id = db.insert_note(conn, source="import", type_="text", created_at="2026-01-01T00:00:00Z", raw_text="a")
    assert db.draft_version_count(conn, note_id) == 0
    db.insert_draft(
        conn, note_id=note_id, pick_id=None, version=0, parent_draft_id=None,
        text="v0", news_item_id=None, verify_count=0, word_count=1,
        selfcheck_notes=None, lint_warnings=[],
    )
    assert db.draft_version_count(conn, note_id) == 1


def test_score_accepts_full_0_to_10_range(conn):
    note_id = db.insert_note(conn, source="import", type_="text", created_at="2026-01-01T00:00:00Z", raw_text="a")
    batch_id = db.start_batch(conn)
    db.record_score(conn, note_id=note_id, batch_id=batch_id, score=0, reason="thin", model="x")
    db.record_score(conn, note_id=note_id, batch_id=batch_id, score=10, reason="great", model="x")
    scores = {r["score"] for r in conn.execute("SELECT score FROM scores WHERE note_id = ?", (note_id,))}
    assert scores == {0, 10}


def test_score_rejects_out_of_range(conn):
    note_id = db.insert_note(conn, source="import", type_="text", created_at="2026-01-01T00:00:00Z", raw_text="a")
    batch_id = db.start_batch(conn)
    with pytest.raises(sqlite3.IntegrityError):
        db.record_score(conn, note_id=note_id, batch_id=batch_id, score=11, reason="x", model="x")


def test_migration_preserves_scores_from_old_1_to_5_schema(tmp_path):
    """Simulates a DB created before the 0-10 rescale (see
    db._migrate_score_range_to_0_10) and checks init_db migrates it in
    place without losing data.
    """
    path = tmp_path / "old.db"
    with db.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, type TEXT,
                tg_message_id INTEGER, raw_text TEXT, audio_path TEXT,
                transcript TEXT, clean_text TEXT, tags TEXT, piece_type TEXT,
                group_id INTEGER, status TEXT, park_reason TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT,
                finished_at TEXT, status TEXT, skipped INTEGER, message_id INTEGER
            );
            CREATE TABLE scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id INTEGER NOT NULL REFERENCES notes(id),
                batch_id INTEGER NOT NULL REFERENCES batches(id),
                score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
                reason TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO notes (id, source, type, status, created_at, updated_at) "
            "VALUES (1, 'import', 'text', 'new', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        conn.execute("INSERT INTO batches (id, started_at, status, skipped) VALUES (1, '2026-01-01T00:00:00Z', 'sent', 0)")
        conn.execute(
            "INSERT INTO scores (note_id, batch_id, score, reason, model, created_at) "
            "VALUES (1, 1, 4, 'old-scale score', 'gemini-old', '2026-01-01T00:00:00Z')"
        )

    db.init_db(path)  # should migrate the old CHECK constraint in place

    with db.connect(path) as conn:
        row = conn.execute("SELECT * FROM scores WHERE note_id = 1").fetchone()
        assert row["score"] == 4  # old value preserved as-is
        assert row["reason"] == "old-scale score"
        # new constraint now allows values the old one would have rejected
        db.record_score(conn, note_id=1, batch_id=1, score=9, reason="new-scale score", model="gpt-4o")
        latest = db.latest_score_for_note(conn, 1)
        assert latest["score"] == 9

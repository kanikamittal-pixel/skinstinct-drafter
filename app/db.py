"""SQLite schema and query helpers.

One connection per call (SQLite handles this fine at this scale; WAL mode is
enabled so the Telegram handlers and the twice-weekly batch job don't block
each other). Nothing here ever deletes a note - "deleting" a note means
setting its status to 'parked'.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL CHECK (source IN ('telegram', 'import')),
    type            TEXT NOT NULL CHECK (type IN ('text', 'voice')),
    tg_message_id   INTEGER,
    raw_text        TEXT,
    audio_path      TEXT,
    transcript      TEXT,
    clean_text      TEXT,
    tags            TEXT,               -- JSON array of 1-3 topic tags
    piece_type      TEXT,               -- best-matching LI category, for example selection
    group_id        INTEGER REFERENCES note_groups(id),
    status          TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'shortlisted', 'used', 'parked')),
    park_reason     TEXT,
    created_at      TEXT NOT NULL,      -- ISO 8601, note's own timestamp
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rationale   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'sent', 'skipped', 'failed')),
    skipped     INTEGER NOT NULL DEFAULT 0,
    message_id  INTEGER
);

CREATE TABLE IF NOT EXISTS scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     INTEGER NOT NULL REFERENCES notes(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    -- 0-10 publishability triage score. Notes scoring below REJECT_THRESHOLD
    -- (app/batch.py) are auto-parked and never reach Meera's shortlist, but
    -- are never deleted - see app/batch.py's docstring for the full policy.
    score       INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10),
    reason      TEXT NOT NULL,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS picks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    note_id     INTEGER NOT NULL REFERENCES notes(id),
    picked_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id         INTEGER NOT NULL REFERENCES notes(id),
    title           TEXT NOT NULL,
    source          TEXT,
    published_at    TEXT,
    link            TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id                 INTEGER NOT NULL REFERENCES notes(id),
    pick_id                 INTEGER REFERENCES picks(id),
    version                 INTEGER NOT NULL DEFAULT 0,   -- 0 = original, 1-3 = revisions
    parent_draft_id         INTEGER REFERENCES drafts(id),
    text                    TEXT NOT NULL,
    news_item_id            INTEGER REFERENCES news_items(id),
    verify_count            INTEGER NOT NULL DEFAULT 0,
    word_count              INTEGER NOT NULL,
    selfcheck_notes         TEXT,
    lint_warnings           TEXT,       -- JSON array of deterministic-check warnings still open
    revision_instruction    TEXT,       -- Meera's instruction that produced this version
    created_at              TEXT NOT NULL,
    sent_at                 TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES drafts(id),
    action      TEXT NOT NULL CHECK (action IN ('approve', 'revise', 'reject')),
    reason      TEXT,
    decided_at  TEXT NOT NULL
);

-- Tracks which draft a Meera reply (a revise instruction or reject reason) belongs to.
CREATE TABLE IF NOT EXISTS pending_input (
    chat_id     INTEGER PRIMARY KEY,
    draft_id    INTEGER NOT NULL REFERENCES drafts(id),
    kind        TEXT NOT NULL CHECK (kind IN ('revise', 'reject_reason')),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,
    component   TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT               -- JSON; never contains API keys or tokens
);

-- Dedupes Telegram's webhook retries: the webhook handler (api/webhook.py)
-- records every update_id it has handled, and skips (still returning 200)
-- any update_id it has already seen, so a Telegram retry can never trigger
-- a second draft/decision for the same tap or message.
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id       INTEGER PRIMARY KEY,
    processed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);
CREATE INDEX IF NOT EXISTS idx_scores_note ON scores(note_id);
CREATE INDEX IF NOT EXISTS idx_drafts_note ON drafts(note_id);
CREATE INDEX IF NOT EXISTS idx_decisions_draft ON decisions(draft_id);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Short-lived connection for one-off scripts: commits and closes on exit."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _configure(sqlite3.connect(db_path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def open_connection(db_path: Path) -> sqlite3.Connection:
    """Long-lived connection for the running bot process (stored in
    bot_data). Callers are responsible for conn.commit() after each write
    and for closing it on shutdown.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return _configure(sqlite3.connect(db_path, check_same_thread=False))


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_score_range_to_0_10(conn)


def _migrate_score_range_to_0_10(conn: sqlite3.Connection) -> None:
    """One-time migration for a DB file created before the score scale
    changed from 1-5 to 0-10 (see app/batch.py's REJECT_THRESHOLD). SQLite
    can't ALTER a CHECK constraint in place, so this rebuilds the table when
    it detects the old constraint text; a no-op on a fresh or already-
    migrated DB. Existing score values (1-5) are kept as-is under the wider
    0-10 constraint - they remain valid, just on the old scale until the
    next batch re-scores those notes.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='scores'"
    ).fetchone()
    sql = row[0] if row else None
    if not sql or "BETWEEN 1 AND 5" not in sql:
        return
    conn.executescript(
        """
        ALTER TABLE scores RENAME TO scores_old_1_5;
        CREATE TABLE scores (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id     INTEGER NOT NULL REFERENCES notes(id),
            batch_id    INTEGER NOT NULL REFERENCES batches(id),
            score       INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10),
            reason      TEXT NOT NULL,
            model       TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        INSERT INTO scores (note_id, batch_id, score, reason, model, created_at)
            SELECT note_id, batch_id, score, reason, model, created_at FROM scores_old_1_5;
        DROP TABLE scores_old_1_5;
        """
    )


def log(conn: sqlite3.Connection, level: str, component: str, event: str, **detail: Any) -> None:
    conn.execute(
        "INSERT INTO logs (ts, level, component, event, detail) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), level, component, event, json.dumps(detail, default=str)),
    )


# --- notes -----------------------------------------------------------------

def insert_note(
    conn: sqlite3.Connection,
    *,
    source: str,
    type_: str,
    created_at: str,
    tg_message_id: Optional[int] = None,
    raw_text: Optional[str] = None,
    audio_path: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notes (source, type, tg_message_id, raw_text, audio_path,
                            status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
        """,
        (source, type_, tg_message_id, raw_text, audio_path, created_at, now_iso()),
    )
    return cur.lastrowid


def get_note(conn: sqlite3.Connection, note_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def notes_with_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM notes WHERE status IN ({placeholders}) ORDER BY created_at",
        statuses,
    ).fetchall()


def update_note_processing(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    transcript: Optional[str] = None,
    clean_text: Optional[str] = None,
    tags: Optional[list[str]] = None,
    piece_type: Optional[str] = None,
    group_id: Optional[int] = None,
) -> None:
    fields, values = [], []
    if transcript is not None:
        fields.append("transcript = ?")
        values.append(transcript)
    if clean_text is not None:
        fields.append("clean_text = ?")
        values.append(clean_text)
    if tags is not None:
        fields.append("tags = ?")
        values.append(json.dumps(tags))
    if piece_type is not None:
        fields.append("piece_type = ?")
        values.append(piece_type)
    if group_id is not None:
        fields.append("group_id = ?")
        values.append(group_id)
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(now_iso())
    values.append(note_id)
    conn.execute(f"UPDATE notes SET {', '.join(fields)} WHERE id = ?", values)


def set_note_status(
    conn: sqlite3.Connection, note_id: int, status: str, *, park_reason: Optional[str] = None
) -> None:
    conn.execute(
        "UPDATE notes SET status = ?, park_reason = ?, updated_at = ? WHERE id = ?",
        (status, park_reason, now_iso(), note_id),
    )


def create_group(conn: sqlite3.Connection, rationale: str, note_ids: list[int]) -> int:
    cur = conn.execute(
        "INSERT INTO note_groups (rationale, created_at) VALUES (?, ?)",
        (rationale, now_iso()),
    )
    group_id = cur.lastrowid
    conn.executemany(
        "UPDATE notes SET group_id = ?, updated_at = ? WHERE id = ?",
        [(group_id, now_iso(), nid) for nid in note_ids],
    )
    return group_id


# --- batches / scores / picks -----------------------------------------------

def start_batch(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO batches (started_at, status) VALUES (?, 'running')", (now_iso(),)
    )
    return cur.lastrowid


def finish_batch(conn: sqlite3.Connection, batch_id: int, status: str, *, message_id: Optional[int] = None) -> None:
    conn.execute(
        "UPDATE batches SET status = ?, finished_at = ?, message_id = ? WHERE id = ?",
        (status, now_iso(), message_id, batch_id),
    )


def record_score(
    conn: sqlite3.Connection, *, note_id: int, batch_id: int, score: int, reason: str, model: str
) -> int:
    cur = conn.execute(
        """
        INSERT INTO scores (note_id, batch_id, score, reason, model, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (note_id, batch_id, score, reason, model, now_iso()),
    )
    return cur.lastrowid


def latest_score_for_note(conn: sqlite3.Connection, note_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM scores WHERE note_id = ? ORDER BY created_at DESC LIMIT 1", (note_id,)
    ).fetchone()


def record_pick(conn: sqlite3.Connection, *, batch_id: int, note_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO picks (batch_id, note_id, picked_at) VALUES (?, ?, ?)",
        (batch_id, note_id, now_iso()),
    )
    return cur.lastrowid


# --- news --------------------------------------------------------------------

def insert_news_item(
    conn: sqlite3.Connection, *, note_id: int, title: str, source: str, published_at: str, link: str
) -> int:
    cur = conn.execute(
        """
        INSERT INTO news_items (note_id, title, source, published_at, link, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (note_id, title, source, published_at, link, now_iso()),
    )
    return cur.lastrowid


def news_items_for_note(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM news_items WHERE note_id = ? ORDER BY id", (note_id,)
    ).fetchall()


# --- drafts / decisions --------------------------------------------------------

def insert_draft(
    conn: sqlite3.Connection,
    *,
    note_id: int,
    pick_id: Optional[int],
    version: int,
    parent_draft_id: Optional[int],
    text: str,
    news_item_id: Optional[int],
    verify_count: int,
    word_count: int,
    selfcheck_notes: Optional[str],
    lint_warnings: list[str],
    revision_instruction: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO drafts (note_id, pick_id, version, parent_draft_id, text, news_item_id,
                             verify_count, word_count, selfcheck_notes, lint_warnings,
                             revision_instruction, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            note_id, pick_id, version, parent_draft_id, text, news_item_id,
            verify_count, word_count, selfcheck_notes, json.dumps(lint_warnings),
            revision_instruction, now_iso(),
        ),
    )
    return cur.lastrowid


def mark_draft_sent(conn: sqlite3.Connection, draft_id: int) -> None:
    conn.execute("UPDATE drafts SET sent_at = ? WHERE id = ?", (now_iso(), draft_id))


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def draft_version_count(conn: sqlite3.Connection, note_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM drafts WHERE note_id = ?", (note_id,)
    ).fetchone()
    return row["n"] if row else 0


def record_decision(conn: sqlite3.Connection, *, draft_id: int, action: str, reason: Optional[str] = None) -> int:
    cur = conn.execute(
        "INSERT INTO decisions (draft_id, action, reason, decided_at) VALUES (?, ?, ?, ?)",
        (draft_id, action, reason, now_iso()),
    )
    return cur.lastrowid


# --- pending input (revise / reject-reason free-text replies) ------------------

def set_pending_input(conn: sqlite3.Connection, *, chat_id: int, draft_id: int, kind: str) -> None:
    conn.execute(
        """
        INSERT INTO pending_input (chat_id, draft_id, kind, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET draft_id = excluded.draft_id,
            kind = excluded.kind, created_at = excluded.created_at
        """,
        (chat_id, draft_id, kind, now_iso()),
    )


def get_pending_input(conn: sqlite3.Connection, chat_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM pending_input WHERE chat_id = ?", (chat_id,)).fetchone()


def clear_pending_input(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM pending_input WHERE chat_id = ?", (chat_id,))


# --- webhook update dedup ------------------------------------------------------

def is_update_processed(conn: sqlite3.Connection, update_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_updates WHERE update_id = ?", (update_id,)
    ).fetchone()
    return row is not None


def mark_update_processed(conn: sqlite3.Connection, update_id: int) -> None:
    # INSERT OR IGNORE: two concurrent invocations racing on the same
    # update_id should not raise - the first one to commit wins, and that's
    # exactly the "already processed" outcome we want.
    conn.execute(
        "INSERT OR IGNORE INTO processed_updates (update_id, processed_at) VALUES (?, ?)",
        (update_id, now_iso()),
    )

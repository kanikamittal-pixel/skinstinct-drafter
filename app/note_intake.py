"""Shared note-storage logic for both places a note can arrive from:
app/collector.py (the notes channel) and app/review.py (Meera's own private
DM with the bot, when she's not replying to a pending Revise/Reject prompt).

Kept in its own module, rather than in collector.py or review.py, so that
neither of those needs to import the other - app/instant.py already imports
app/review.py (to send the finished draft back), and collector.py needs
app/instant.py, so collector importing review directly would create an
import cycle.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from telegram import Message
from telegram.ext import ContextTypes

from app import db

AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "audio"


async def store_note(message: Message, context: ContextTypes.DEFAULT_TYPE, conn: sqlite3.Connection) -> int | None:
    """Stores a text or voice message as a note. Returns the new note id, or
    None if the message had nothing to store (e.g. a sticker, a photo with
    no caption).
    """
    created_at = message.date.isoformat() if message.date else db.now_iso()

    if message.voice is not None or message.audio is not None:
        media = message.voice or message.audio
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        file = await context.bot.get_file(media.file_id)
        dest = AUDIO_DIR / f"{message.message_id}_{media.file_unique_id}.ogg"
        await file.download_to_drive(custom_path=str(dest))
        note_id = db.insert_note(
            conn,
            source="telegram",
            type_="voice",
            created_at=created_at,
            tg_message_id=message.message_id,
            audio_path=str(dest.relative_to(AUDIO_DIR.parent.parent)),
        )
        db.log(conn, "info", "collector", "voice_note_stored", note_id=note_id)
        conn.commit()
        return note_id

    text = message.text or message.caption
    if text:
        note_id = db.insert_note(
            conn,
            source="telegram",
            type_="text",
            created_at=created_at,
            tg_message_id=message.message_id,
            raw_text=text,
        )
        db.log(conn, "info", "collector", "text_note_stored", note_id=note_id)
        conn.commit()
        return note_id

    return None

"""Silently collects every note Meera drops into her private Telegram
channel, then immediately runs it through the transcribe -> score -> draft
-> news -> send pipeline (app/instant.py): score the raw note first against
the same 0-10 cutoff the twice-weekly batch uses, and only if it passes,
draft it with a news hook and send Meera the final draft right away. A note
that doesn't pass is auto-parked, with a short note back to Meera saying
why (score + reason) - it still surfaces later via "Show all", nothing is
deleted.

Meera can also drop a note directly into her private DM with the bot,
instead of the channel - that path is handled in app/review.py
(handle_private_message), which has to first check whether the message is
actually a reply to a pending Revise/Reject prompt before treating it as a
new note. Both paths share the storage logic in app/note_intake.py.

Voice notes are transcribed immediately (via app/instant.py), since instant
processing needs the text right away rather than waiting for the batch.
Anything from a chat other than the configured notes channel is ignored
(see app/auth.py) and logged as such.
"""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app import auth, db, instant
from app.config import Config
from app.note_intake import store_note


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    conn = context.bot_data["conn"]

    if not auth.is_from_notes_channel(update, config):
        db.log(
            conn, "info", "collector", "ignored_chat",
            chat_id=update.effective_chat.id if update.effective_chat else None,
        )
        conn.commit()
        return

    message = update.effective_message
    if message is None:
        return

    note_id = await store_note(message, context, conn)
    if note_id is not None:
        await instant.process_new_note(update, context, note_id)

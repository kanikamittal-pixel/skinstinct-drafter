"""Update processing for the Vercel webhook path (api/webhook.py).

Scope of this pass: validated, deduped intake for "message" and
"channel_post" updates (text + voice note storage, same as
app/collector.py's job for the long-polling bot), plus a callback-query
acknowledgement so button taps never look broken to Meera. Wiring the full
pick -> draft -> Approve/Revise/Reject flow (app/review.py, app/pipeline.py)
into this synchronous handler is the next step, once the news lookup lands -
see the README's "Vercel webhook" section for why that's called out
separately (duration-budget risk: the self-check retry pipeline can run
well past a webhook's time limit).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from app import db
from app.telegram_sync import answer_callback_query, download_file, get_file_path, send_message
from app.webhook_config import WebhookConfig

AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "audio"


def handle_update(conn: sqlite3.Connection, config: WebhookConfig, update: dict) -> None:
    if "callback_query" in update:
        _handle_callback_query(conn, config, update["callback_query"])
        return

    message = update.get("message") or update.get("channel_post")
    if message is None:
        return  # nothing we handle (e.g. edited_message, my_chat_member, ...)

    created_at = _iso_from_unix(message.get("date"))

    if "voice" in message or "audio" in message:
        _store_voice(conn, config, message, created_at)
        return

    text = message.get("text") or message.get("caption")
    if text:
        note_id = db.insert_note(
            conn, source="telegram", type_="text", created_at=created_at,
            tg_message_id=message.get("message_id"), raw_text=text,
        )
        db.log(conn, "info", "webhook", "text_note_stored", note_id=note_id)


def _store_voice(conn: sqlite3.Connection, config: WebhookConfig, message: dict, created_at: str) -> None:
    media = message.get("voice") or message.get("audio")
    file_id = media["file_id"]
    file_unique_id = media["file_unique_id"]

    file_path = get_file_path(config.bot_token, file_id=file_id, timeout=config.outbound_timeout_seconds)
    dest = AUDIO_DIR / f"{message.get('message_id')}_{file_unique_id}.ogg"
    download_file(config.bot_token, file_path=file_path, dest=dest, timeout=config.outbound_timeout_seconds)

    note_id = db.insert_note(
        conn, source="telegram", type_="voice", created_at=created_at,
        tg_message_id=message.get("message_id"),
        audio_path=str(dest.relative_to(AUDIO_DIR.parent.parent)),
    )
    db.log(conn, "info", "webhook", "voice_note_stored", note_id=note_id)


def _handle_callback_query(conn: sqlite3.Connection, config: WebhookConfig, callback_query: dict) -> None:
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    db.log(conn, "info", "webhook", "callback_query_received", data=callback_query.get("data"), chat_id=chat_id)

    # Acknowledge immediately so Telegram's "loading" spinner on the button
    # clears - the pick/draft/approve/revise/reject actions themselves are
    # wired up in the next pass (see module docstring).
    answer_callback_query(
        config.bot_token,
        callback_query_id=callback_query["id"],
        text="Got it - button actions aren't wired up on the webhook yet.",
        timeout=config.outbound_timeout_seconds,
    )
    if chat_id is not None:
        send_message(
            config.bot_token, chat_id=chat_id,
            text="This button isn't connected on the webhook deployment yet - "
                 "that's the next piece we're adding.",
            timeout=config.outbound_timeout_seconds,
        )


def _iso_from_unix(unix_ts) -> str:
    if unix_ts is None:
        return db.now_iso()
    return dt.datetime.fromtimestamp(int(unix_ts), tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

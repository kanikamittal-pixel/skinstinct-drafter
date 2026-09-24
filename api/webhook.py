"""Vercel serverless entrypoint for Telegram's webhook: POST /api/webhook.

Covers, in order:
  1. Reads text from either update type: "message" (Meera's private chat)
     or "channel_post" (her notes channel).
  2. Only processes updates whose chat id is in ALLOWED_CHAT_IDS; anything
     else is ignored, silently to the sender, but logged.
  3. Ignores anything sent by the bot itself, so a draft it posts can never
     trigger another run.
  4. Verifies the X-Telegram-Bot-Api-Secret-Token header against
     TELEGRAM_WEBHOOK_SECRET before doing anything else; a mismatch is a 401
     and nothing is read from the body.
  5. De-dupes by update_id (app/db.py's processed_updates table), so a
     Telegram retry of an update we already handled is a no-op.
  6. Every outside call (OpenAI, Google News, Telegram itself) carries the
     configured timeout - see app/webhook_config.py and the README's
     "Vercel webhook" section for the duration-budget discussion.
  7. Always returns 200 for anything handled, ignored, or deduped, so
     Telegram stops retrying. Only a bad secret token gets a non-200 (401) -
     that's a request we want Telegram (or whoever else) to stop sending,
     not retry.
"""
from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.webhook_config import WebhookConfigError, load_webhook_config  # noqa: E402
from app.webhook_handlers import handle_update  # noqa: E402

app = Flask(__name__)


def _extract_ids(update: dict) -> tuple[int | None, dict | None]:
    """Returns (chat_id, sender) for whichever update shape this is."""
    message = update.get("message") or update.get("channel_post")
    if message is not None:
        return message.get("chat", {}).get("id"), message.get("from")

    callback_query = update.get("callback_query")
    if callback_query is not None:
        chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
        return chat_id, callback_query.get("from")

    return None, None


@app.post("/api/webhook")
def webhook():
    try:
        config = load_webhook_config()
    except WebhookConfigError as exc:
        # A misconfigured deployment isn't something Telegram retrying will
        # fix - log it loudly, but still 200 so it doesn't hammer us.
        app.logger.error("Webhook misconfigured: %s", exc)
        return jsonify(ok=False, error="misconfigured"), 200

    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret != config.webhook_secret:
        return jsonify(ok=False, error="unauthorized"), 401

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return jsonify(ok=False, error="bad_json"), 200

    update_id = update.get("update_id")
    chat_id, sender = _extract_ids(update)

    db_path = Path(config.db_path)
    db.init_db(db_path)  # idempotent; only does real work on a cold start against a fresh DB file
    conn = db.open_connection(db_path)
    try:
        if update_id is not None and db.is_update_processed(conn, update_id):
            return jsonify(ok=True, status="duplicate"), 200

        if chat_id is None or chat_id not in config.allowed_chat_ids:
            db.log(conn, "info", "webhook", "ignored_chat", chat_id=chat_id, update_id=update_id)
            _mark_and_commit(conn, update_id)
            return jsonify(ok=True, status="ignored_chat"), 200

        if sender is not None and sender.get("is_bot"):
            db.log(conn, "info", "webhook", "ignored_self", update_id=update_id)
            _mark_and_commit(conn, update_id)
            return jsonify(ok=True, status="ignored_self"), 200

        handle_update(conn, config, update)
        _mark_and_commit(conn, update_id)
        return jsonify(ok=True), 200

    except Exception as exc:  # noqa: BLE001 - Telegram must still get a 200
        app.logger.exception("Unhandled error processing update %s", update_id)
        db.log(conn, "error", "webhook", "unhandled_exception", update_id=update_id, error=str(exc))
        conn.commit()
        return jsonify(ok=False, error="internal_error"), 200
    finally:
        conn.close()


def _mark_and_commit(conn, update_id: int | None) -> None:
    if update_id is not None:
        db.mark_update_processed(conn, update_id)
    conn.commit()

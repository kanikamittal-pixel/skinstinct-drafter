"""Handles Meera's two review steps in her private chat:

1. Picking notes from the shortlist (or "show all" / "skip this batch").
2. Reviewing a draft: Approve / Revise / Reject, with up to 3 revisions.

Every button handler checks auth.is_meera_private_chat first; anything else
is ignored. Nothing here ever deletes a note or a draft - rejecting a note
parks it (status='parked') with an optional reason, and it returns in a
future batch.
"""
from __future__ import annotations

import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app import auth, batch, db, lint
from app.config import Config
from app.llm import LLMClient
from app.note_intake import store_note
from app.pipeline import build_fresh_draft, build_revision

MAX_REVISIONS = 3


def _note_display_text(note: sqlite3.Row) -> str:
    return (note["clean_text"] or note["transcript"] or note["raw_text"] or "").strip()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    if not auth.is_meera_private_chat(update, config):
        return

    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

    if action == "pick":
        await _handle_pick(update, context, note_id=int(parts[1]))
    elif action == "showall":
        await batch.show_all_notes(update, context, batch_id=int(parts[1]), page=int(parts[2]))
    elif action == "skip":
        await _handle_skip(update, context, batch_id=int(parts[1]))
    elif action == "appr":
        await _handle_approve(update, context, draft_id=int(parts[1]))
    elif action == "rev":
        await _handle_revise_request(update, context, draft_id=int(parts[1]))
    elif action == "rej":
        await _handle_reject_request(update, context, draft_id=int(parts[1]))
    else:
        await query.answer()


async def _handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE, *, batch_id: int) -> None:
    conn: sqlite3.Connection = context.bot_data["conn"]
    db.log(conn, "info", "review", "batch_skipped", batch_id=batch_id)
    conn.commit()
    await update.callback_query.answer("Batch skipped - see you next time.")
    await update.callback_query.message.reply_text(
        "Skipped. Nothing was picked; all notes stay in the backlog."
    )


async def _handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE, *, note_id: int) -> None:
    conn: sqlite3.Connection = context.bot_data["conn"]
    client: LLMClient = context.bot_data["llm"]

    note = db.get_note(conn, note_id)
    if note is None:
        await update.callback_query.answer("Couldn't find that note.")
        return

    await update.callback_query.answer("Drafting...")
    db.set_note_status(conn, note_id, "shortlisted")
    conn.commit()

    # Find the pick's batch via the most recent batch row (batches are
    # sequential and short-lived, so "most recent" is unambiguous in practice).
    batch_row = conn.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()
    pick_id = db.record_pick(conn, batch_id=batch_row["id"] if batch_row else 0, note_id=note_id)
    conn.commit()

    try:
        result = build_fresh_draft(
            conn,
            client,
            note_id=note_id,
            pick_id=pick_id,
            note_text=_note_display_text(note),
            piece_type=note["piece_type"] or "explainer",
        )
    except Exception as exc:  # noqa: BLE001 - a drafting failure must not crash the bot
        db.log(conn, "error", "review", "draft_failed", note_id=note_id, error=str(exc))
        db.set_note_status(conn, note_id, "new")  # give it back, don't leave it stuck
        conn.commit()
        await update.callback_query.message.reply_text(
            f"Couldn't draft note #{note_id} - OpenAI's API failed after retries: {exc}\n\n"
            "The note is back in the backlog, nothing was lost. Try again in a bit, "
            "or pick it from \"Show all\" once things calm down."
        )
        return

    conn.commit()
    db.set_note_status(conn, note_id, "used")
    conn.commit()

    await send_draft_message(update, context, note, result)


async def send_draft_message(update, context, note, result) -> None:
    config: Config = context.bot_data["config"]

    news_line = (
        f"News hook: \"{result.news_used.title}\" - {result.news_used.source} ({result.news_used.link})"
        if result.news_used else "News hook: none"
    )
    warnings_line = (
        "\n\n_Still open after checks: " + "; ".join(result.warnings) + "_"
        if result.warnings else ""
    )

    text = (
        f"*Draft for note #{note['id']}*\n\n"
        f"{result.text}\n\n"
        f"---\n"
        f"Original note: {_note_display_text(note)[:300]}\n"
        f"{news_line}\n"
        f"[VERIFY] flags: {result.verify_count}"
        f"{warnings_line}"
    )

    buttons = [[
        InlineKeyboardButton("Approve", callback_data=f"appr:{result.draft_id}"),
        InlineKeyboardButton("Revise", callback_data=f"rev:{result.draft_id}"),
        InlineKeyboardButton("Reject", callback_data=f"rej:{result.draft_id}"),
    ]]

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await context.bot.send_message(
            chat_id=config.meera_user_id, text=text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    conn: sqlite3.Connection = context.bot_data["conn"]
    db.mark_draft_sent(conn, result.draft_id)
    conn.commit()


async def _handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, *, draft_id: int) -> None:
    conn: sqlite3.Connection = context.bot_data["conn"]
    draft = db.get_draft(conn, draft_id)
    if draft is None:
        await update.callback_query.answer("Couldn't find that draft.")
        return

    db.record_decision(conn, draft_id=draft_id, action="approve")
    conn.commit()

    clean_copy = lint.strip_verify_tags(draft["text"])
    await update.callback_query.answer("Approved.")
    await update.callback_query.message.reply_text(
        "Clean copy, ready to paste into LinkedIn:\n\n" + clean_copy
    )


async def _handle_revise_request(update: Update, context: ContextTypes.DEFAULT_TYPE, *, draft_id: int) -> None:
    conn: sqlite3.Connection = context.bot_data["conn"]
    config: Config = context.bot_data["config"]
    draft = db.get_draft(conn, draft_id)
    if draft is None:
        await update.callback_query.answer("Couldn't find that draft.")
        return

    version_count = db.draft_version_count(conn, draft["note_id"])
    if version_count > MAX_REVISIONS:
        await update.callback_query.answer("Revision limit reached.")
        await update.callback_query.message.reply_text(
            f"That's {MAX_REVISIONS} revisions already - approve, reject, or "
            "start fresh with a new note."
        )
        return

    db.set_pending_input(conn, chat_id=config.meera_user_id, draft_id=draft_id, kind="revise")
    conn.commit()
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "What should change? Reply with an instruction, e.g. \"shorter, drop the news bit\"."
    )


async def _handle_reject_request(update: Update, context: ContextTypes.DEFAULT_TYPE, *, draft_id: int) -> None:
    conn: sqlite3.Connection = context.bot_data["conn"]
    config: Config = context.bot_data["config"]
    draft = db.get_draft(conn, draft_id)
    if draft is None:
        await update.callback_query.answer("Couldn't find that draft.")
        return

    db.set_pending_input(conn, chat_id=config.meera_user_id, draft_id=draft_id, kind="reject_reason")
    conn.commit()
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Reply with a reason if you want (optional), or just send \"-\" to skip. "
        "The note goes back into the backlog as parked."
    )


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles Meera's free-text/voice messages in her private DM with the
    bot. Two cases:
      1. It's a reply to a pending Revise/Reject prompt (db.pending_input) -
         handle that as before.
      2. Otherwise, treat it as a new note dropped directly in the DM
         (an alternative to the notes channel) and run the same instant
         pipeline app/collector.py runs for channel notes.
    """
    config: Config = context.bot_data["config"]
    if not auth.is_meera_private_chat(update, config):
        return

    conn: sqlite3.Connection = context.bot_data["conn"]
    pending = db.get_pending_input(conn, config.meera_user_id)
    if pending is None:
        message = update.effective_message
        if message is None:
            return
        note_id = await store_note(message, context, conn)
        if note_id is not None:
            # Deferred import: app.instant imports send_draft_message from
            # this module, so importing it at module load time here would
            # be a circular import. By call time both modules are already
            # fully loaded, so this is safe.
            from app.instant import process_new_note

            await process_new_note(update, context, note_id)
        return

    message = update.effective_message
    reply_text = (message.text or "").strip()
    db.clear_pending_input(conn, config.meera_user_id)
    conn.commit()

    draft = db.get_draft(conn, pending["draft_id"])
    if draft is None:
        return

    if pending["kind"] == "reject_reason":
        reason = None if reply_text in ("-", "") else reply_text
        db.record_decision(conn, draft_id=draft["id"], action="reject", reason=reason)
        db.set_note_status(conn, draft["note_id"], "parked", park_reason=reason)
        conn.commit()
        await message.reply_text("Parked. It'll come back in a future batch.")
        return

    # kind == "revise"
    client: LLMClient = context.bot_data["llm"]
    note = db.get_note(conn, draft["note_id"])
    db.record_decision(conn, draft_id=draft["id"], action="revise", reason=reply_text)
    conn.commit()

    try:
        result = build_revision(
            conn, client,
            note_id=draft["note_id"], pick_id=draft["pick_id"], parent_draft=draft,
            instruction=reply_text, piece_type=note["piece_type"] or "explainer",
        )
    except Exception as exc:  # noqa: BLE001 - a revision failure must not crash the bot
        db.log(conn, "error", "review", "revision_failed", draft_id=draft["id"], error=str(exc))
        conn.commit()
        await message.reply_text(
            f"Couldn't revise that draft - OpenAI's API failed after retries: {exc}\n\n"
            "Your instruction wasn't lost, but nothing new was generated. Try Revise again "
            "in a bit."
        )
        return
    conn.commit()

    await _send_revised_message(message, context, note, result)


async def _send_revised_message(message, context, note, result) -> None:
    news_line = (
        f"News hook: \"{result.news_used.title}\" - {result.news_used.source} ({result.news_used.link})"
        if result.news_used else "News hook: none"
    )
    warnings_line = (
        "\n\n_Still open after checks: " + "; ".join(result.warnings) + "_"
        if result.warnings else ""
    )
    text = (
        f"*Revised draft for note #{note['id']}*\n\n"
        f"{result.text}\n\n"
        f"---\n"
        f"{news_line}\n"
        f"[VERIFY] flags: {result.verify_count}"
        f"{warnings_line}"
    )
    buttons = [[
        InlineKeyboardButton("Approve", callback_data=f"appr:{result.draft_id}"),
        InlineKeyboardButton("Revise", callback_data=f"rev:{result.draft_id}"),
        InlineKeyboardButton("Reject", callback_data=f"rej:{result.draft_id}"),
    ]]
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
    conn: sqlite3.Connection = context.bot_data["conn"]
    db.mark_draft_sent(conn, result.draft_id)
    conn.commit()

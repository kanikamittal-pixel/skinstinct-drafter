"""The twice-weekly batch: transcribe -> clean/tag/group -> score -> triage -> shortlist.

Runs Monday and Thursday at 08:00 IST by default (app/main.py schedules
this via JobQueue.run_daily), and can also be triggered manually with
scripts/run_batch_now.py. Every unused note (status 'new' or 'parked') is
eligible for scoring - parked notes are re-scored on every batch, so a
note that was auto-rejected for being thin can resurface later if you add
more material to it.

Publishability triage: each note gets a 0-10 score (app/prompts/score.txt).
Anything scoring below REJECT_THRESHOLD is auto-parked right here in the
batch, with the score as its park reason, and never reaches Meera's
shortlist. This is a deliberate change from "the AI ranks but never
discards" - low-scoring notes are now filtered out automatically rather
than just ranked low. They are still never deleted: an auto-rejected note
stays visible via "Show all" from any batch message, same as a manually
rejected one.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app import auth, db
from app.config import ROOT, Config
from app.drafting import clean_and_tag_batch, score_batch
from app.llm import LLMClient

TOP_N = 5
SHORTLIST_PAGE_SIZE = 10
# 0-10 scale (app/prompts/score.txt). Notes scoring below this are
# auto-parked and never reach the shortlist - see module docstring.
REJECT_THRESHOLD = 5


def _note_display_text(note: sqlite3.Row) -> str:
    return (note["clean_text"] or note["transcript"] or note["raw_text"] or "").strip()


async def run_batch(context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.bot_data["config"]
    conn: sqlite3.Connection = context.bot_data["conn"]
    client: LLMClient = context.bot_data["llm"]

    batch_id = db.start_batch(conn)
    conn.commit()
    db.log(conn, "info", "batch", "started", batch_id=batch_id)
    conn.commit()

    try:
        pending = db.notes_with_status(conn, "new", "parked")

        # Transcribe any pending voice notes.
        for note in pending:
            if note["type"] == "voice" and not note["transcript"]:
                audio_path = ROOT / note["audio_path"]
                transcript = client.transcribe_audio(audio_path)
                db.update_note_processing(conn, note["id"], transcript=transcript)
                conn.commit()

        pending = db.notes_with_status(conn, "new", "parked")
        untagged = [n for n in pending if not n["clean_text"]]

        if untagged:
            notes_payload = [
                {"note_id": n["id"], "text": (n["transcript"] or n["raw_text"] or "")}
                for n in untagged
            ]
            cleaned, groups = clean_and_tag_batch(client, notes_payload)
            for c in cleaned:
                db.update_note_processing(
                    conn, c.note_id, clean_text=c.clean_text, tags=c.tags, piece_type=c.piece_type
                )
            for g in groups:
                db.create_group(conn, g.rationale, g.note_ids)
            conn.commit()

        pending = db.notes_with_status(conn, "new", "parked")
        if not pending:
            db.finish_batch(conn, batch_id, "sent")
            conn.commit()
            await context.bot.send_message(
                chat_id=config.meera_user_id,
                text="No unused notes for this batch - the backlog is empty. "
                "Drop a few notes into the channel and I'll catch them next time.",
            )
            return

        score_payload = [
            {"note_id": n["id"], "text": _note_display_text(n), "tags": _tags(n)}
            for n in pending
        ]
        scores = score_batch(client, score_payload)
        for s in scores:
            db.record_score(conn, note_id=s.note_id, batch_id=batch_id, score=s.score, reason=s.reason, model=config.openai_model)
        conn.commit()

        by_note = {n["id"]: n for n in pending}

        # Publishability triage: auto-park anything below the cutoff before
        # it's ever ranked or shown, per REJECT_THRESHOLD (see module docstring).
        auto_rejected = [s for s in scores if s.score < REJECT_THRESHOLD]
        for s in auto_rejected:
            db.set_note_status(
                conn, s.note_id, "parked",
                park_reason=f"auto-rejected: scored {s.score}/10 (below the {REJECT_THRESHOLD} cutoff) - {s.reason}",
            )
        conn.commit()

        eligible = [s for s in scores if s.score >= REJECT_THRESHOLD]
        ranked = sorted(eligible, key=lambda s: s.score, reverse=True)

        message_id = await _send_shortlist(
            context, config, batch_id, ranked, by_note, page=0, auto_rejected_count=len(auto_rejected)
        )
        db.finish_batch(conn, batch_id, "sent", message_id=message_id)
        conn.commit()

    except Exception as exc:  # noqa: BLE001 - batch failures must not crash the bot process
        db.log(conn, "error", "batch", "failed", batch_id=batch_id, error=str(exc))
        db.finish_batch(conn, batch_id, "failed")
        conn.commit()
        await context.bot.send_message(
            chat_id=config.meera_user_id,
            text=f"This batch hit an error and stopped: {exc}\n\nNothing was lost - "
            "your notes are all still in the backlog for next time.",
        )
        raise


def _tags(note: sqlite3.Row) -> list[str]:
    raw = note["tags"]
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _shortlist_line(note: sqlite3.Row, score) -> str:
    text = _note_display_text(note)
    first_line = text.splitlines()[0][:120] if text else "(empty note)"
    park_note = f" [parked: {note['park_reason']}]" if note["status"] == "parked" and note["park_reason"] else ""
    return f"*{score.score}/10* - {score.reason}{park_note}\n_{first_line}_"


async def _send_shortlist(context, config, batch_id, ranked, by_note, *, page: int, auto_rejected_count: int = 0) -> int:
    top = ranked[:TOP_N]

    reject_note = (
        f" ({auto_rejected_count} other note{'s' if auto_rejected_count != 1 else ''} scored below "
        f"{REJECT_THRESHOLD}/10 and were parked automatically - still reachable via \"Show all\".)"
        if auto_rejected_count else ""
    )

    if not top:
        lines = [
            f"No notes scored {REJECT_THRESHOLD}/10 or above this batch.{reject_note}\n"
            "Nothing was deleted - use \"Show all\" below to see everything, including "
            "the auto-parked ones, or drop more notes into the channel."
        ]
    else:
        lines = [f"Batch shortlist - top {len(top)} of {len(ranked)} notes at or above {REJECT_THRESHOLD}/10.{reject_note}\n"]

    buttons = []
    for s in top:
        note = by_note[s.note_id]
        lines.append(f"\n*Note #{note['id']}* {_shortlist_line(note, s)}")
        buttons.append([InlineKeyboardButton(f"Develop #{note['id']} ({s.score}/10)", callback_data=f"pick:{note['id']}")])

    buttons.append([
        InlineKeyboardButton("Show all", callback_data=f"showall:{batch_id}:0"),
        InlineKeyboardButton("Skip this batch", callback_data=f"skip:{batch_id}"),
    ])

    sent = await context.bot.send_message(
        chat_id=config.meera_user_id,
        text="\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return sent.message_id


async def show_all_notes(update: Update, context: ContextTypes.DEFAULT_TYPE, batch_id: int, page: int) -> None:
    config: Config = context.bot_data["config"]
    conn: sqlite3.Connection = context.bot_data["conn"]

    if not auth.is_meera_private_chat(update, config):
        return

    pending = db.notes_with_status(conn, "new", "parked")
    scores_by_note = {}
    for n in pending:
        row = db.latest_score_for_note(conn, n["id"])
        if row:
            scores_by_note[n["id"]] = row

    start = page * SHORTLIST_PAGE_SIZE
    chunk = pending[start:start + SHORTLIST_PAGE_SIZE]
    if not chunk:
        await update.callback_query.answer("No more notes.")
        return

    lines = [f"All unused notes ({start + 1}-{start + len(chunk)} of {len(pending)}):\n"]
    buttons = []
    for note in chunk:
        score_row = scores_by_note.get(note["id"])
        score_txt = f" ({score_row['score']}/10)" if score_row else ""
        park_txt = f" [{note['park_reason']}]" if note["status"] == "parked" and note["park_reason"] else ""
        text = _note_display_text(note)
        first_line = text.splitlines()[0][:100] if text else "(empty note)"
        lines.append(f"\n*#{note['id']}*{score_txt}{park_txt}: _{first_line}_")
        buttons.append([InlineKeyboardButton(f"Develop #{note['id']}", callback_data=f"pick:{note['id']}")])

    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton("< Prev", callback_data=f"showall:{batch_id}:{page - 1}"))
    if start + SHORTLIST_PAGE_SIZE < len(pending):
        nav.append(InlineKeyboardButton("Next >", callback_data=f"showall:{batch_id}:{page + 1}"))
    if nav:
        buttons.append(nav)

    await update.callback_query.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons)
    )
    await update.callback_query.answer()

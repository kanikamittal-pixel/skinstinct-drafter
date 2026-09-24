"""Instant per-note pipeline: runs the moment Meera drops a note into the
channel, rather than waiting for the twice-weekly batch.

Sequence: transcribe (if voice) -> clean/tag -> score the RAW note 0-10 ->
reject low scores -> if it passes, draft it (with a Google News hook) ->
send it back to Meera for review.

This scores the raw note, not a drafted post, deliberately - an earlier
version of this pipeline drafted first and scored the draft, and testing
caught a real failure mode: a good drafter can pad a thin note (e.g. "need
to restock boxes") into something that reads specific and polished, which
then fools a scorer looking at the polished prose rather than the
underlying material. Scoring the raw note first - the same order the
twice-weekly batch uses (app/batch.py, app/prompts/score.txt) - means a
note with nothing in it can't fake substance it doesn't have. This also
means only notes that pass ever cost a drafting call, instead of every note
costing one regardless of outcome.

Below REJECT_THRESHOLD (shared with app/batch.py): the note is auto-parked
with the score as its reason, and Meera gets a short message saying so (the
score and reason, not silence - an earlier version said nothing at all,
which just looked broken). Nothing is deleted - the note is still reachable
via "Show all" from any batch message, and gets re-scored on the next batch
or the next time material is added to it.

The twice-weekly batch (app/batch.py) still runs independently and will
re-score anything still sitting as 'new' or 'parked' - this instant path
doesn't replace it, it's a second entry point into the same scoring/draft
machinery.
"""
from __future__ import annotations

import sqlite3

from telegram import Update
from telegram.ext import ContextTypes

from app import db
from app.batch import REJECT_THRESHOLD
from app.config import ROOT, Config
from app.drafting import clean_and_tag_batch, score_batch
from app.pipeline import build_fresh_draft
from app.review import send_draft_message


def _note_text(note: sqlite3.Row) -> str:
    return (note["transcript"] or note["raw_text"] or "").strip()


async def process_new_note(update: Update, context: ContextTypes.DEFAULT_TYPE, note_id: int) -> None:
    """Runs transcribe -> score -> (draft + news + send) for one freshly-
    collected note. Any failure is caught, logged, and the note is left in a
    recoverable state - it must never vanish silently.
    """
    conn: sqlite3.Connection = context.bot_data["conn"]
    client = context.bot_data["llm"]
    config: Config = context.bot_data["config"]

    note = db.get_note(conn, note_id)
    if note is None:
        return

    batch_id = db.start_batch(conn)
    conn.commit()

    try:
        if note["type"] == "voice" and not note["transcript"]:
            transcript = client.transcribe_audio(ROOT / note["audio_path"])
            db.update_note_processing(conn, note_id, transcript=transcript)
            conn.commit()
            note = db.get_note(conn, note_id)

        text = _note_text(note)
        if not text:
            db.finish_batch(conn, batch_id, "failed")
            conn.commit()
            return

        cleaned, groups = clean_and_tag_batch(client, [{"note_id": note_id, "text": text}])
        c = cleaned[0] if cleaned else None
        if c is not None:
            db.update_note_processing(
                conn, note_id, clean_text=c.clean_text, tags=c.tags, piece_type=c.piece_type
            )
        for g in groups:
            db.create_group(conn, g.rationale, g.note_ids)
        conn.commit()
        note = db.get_note(conn, note_id)

        note_text = note["clean_text"] or text
        piece_type = note["piece_type"] or "explainer"
        tags = list(c.tags) if c is not None else []

        # Score the raw (cleaned) note - not a draft - so thin material
        # can't be papered over by good prose before it's ever judged.
        scores = score_batch(client, [{"note_id": note_id, "text": note_text, "tags": tags}])
        if not scores:
            db.finish_batch(conn, batch_id, "failed")
            conn.commit()
            return
        score, reason = scores[0].score, scores[0].reason
        db.record_score(conn, note_id=note_id, batch_id=batch_id, score=score, reason=reason, model=config.openai_model)
        conn.commit()

        if score < REJECT_THRESHOLD:
            db.set_note_status(
                conn, note_id, "parked",
                park_reason=f"auto-rejected: scored {score}/10 (below the {REJECT_THRESHOLD} cutoff) - {reason}",
            )
            conn.commit()
            db.finish_batch(conn, batch_id, "sent")
            conn.commit()
            # Was silent before - a rejected note should still tell Meera
            # something happened, not vanish with no trace at all.
            await context.bot.send_message(
                chat_id=config.meera_user_id,
                text=f"Noted, but parked it - scored {score}/10: {reason}\n\n"
                "Not deleted. It'll be re-scored next batch, or add more detail "
                "and drop it again. Find it anytime via \"Show all\" on any batch message.",
            )
            return

        # Passed - draft it, with a real Google News hook if one's relevant.
        result = build_fresh_draft(
            conn, client,
            note_id=note_id, pick_id=None,
            note_text=note_text, tags=tags, piece_type=piece_type,
        )
        db.set_note_status(conn, note_id, "used")
        conn.commit()
        db.finish_batch(conn, batch_id, "sent")
        conn.commit()

        # send_draft_message checks update.callback_query, which is None for
        # a channel_post - it sends directly via context.bot.send_message.
        await send_draft_message(update, context, note, result)

    except Exception as exc:  # noqa: BLE001 - must never crash the collector
        db.log(conn, "error", "instant", "pipeline_failed", note_id=note_id, error=str(exc))
        db.finish_batch(conn, batch_id, "failed")
        conn.commit()
        try:
            await context.bot.send_message(
                chat_id=config.meera_user_id,
                text=f"Couldn't process a new note (#{note_id}) automatically: {exc}\n\n"
                "It's still in the backlog - it'll be picked up in the next batch, or "
                "try \"Show all\" from any batch message.",
            )
        except Exception:  # noqa: BLE001 - never let the notification itself crash this
            pass

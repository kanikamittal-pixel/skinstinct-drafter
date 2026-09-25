"""Ties together news lookup, drafting, the OpenAI self-check pass, and the
deterministic lint checks (app/lint.py) into one call that produces a
persisted draft row.

Flow for a fresh draft (version 0):
  drafting.extract_news_keywords (classify + specific/broader query)
    -> news.fetch_ranked_news (specific -> broader -> beat, only if
       newsworthy - see app/news.py and app/prompts/news_keywords.txt)
    -> drafting.generate_draft -> lint.autofix
    -> drafting.selfcheck_draft (one revision pass) -> lint.autofix
    -> lint.check (remaining warnings shown to Meera, never hidden)
    -> db.insert_draft

Flow for a revision (version 1-3):
  drafting.revise_draft -> lint.autofix -> lint.check -> db.insert_draft
(no second self-check pass on revisions - the checklist already ran once,
and re-running it after every manual revision would fight Meera's edits)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from app import db, lint
from app.drafting import extract_news_keywords, generate_draft, revise_draft, selfcheck_draft
from app.llm import LLMClient
from app.news import NewsItem, fetch_ranked_news
from app.news_config import INDIA_CATEGORIES


@dataclass
class PipelineResult:
    draft_id: int
    text: str  # with [VERIFY] tags, for Meera's review copy
    word_count: int
    verify_count: int
    warnings: list[str]
    news_item_id: Optional[int]
    news_used: Optional[NewsItem]


def build_fresh_draft(
    conn: sqlite3.Connection,
    client: LLMClient,
    *,
    note_id: int,
    pick_id: Optional[int],
    note_text: str,
    piece_type: str,
) -> PipelineResult:
    news_item_id: Optional[int] = None
    news_used: Optional[NewsItem] = None

    keywords_result = extract_news_keywords(client, note_text=note_text)
    db.log(
        conn, "info", "news", "keywords",
        note_id=note_id, newsworthy=keywords_result.newsworthy,
        category=keywords_result.category, keywords=keywords_result.keywords,
        specific_query=keywords_result.specific_query, broader_query=keywords_result.broader_query,
    )

    news_items: list[NewsItem] = []
    if keywords_result.newsworthy:
        add_india = keywords_result.category in INDIA_CATEGORIES
        news_items, fetch_log = fetch_ranked_news(
            specific_query=keywords_result.specific_query,
            broader_query=keywords_result.broader_query,
            category=keywords_result.category,
            keywords=keywords_result.keywords,
            add_india=add_india,
        )
        db.log(
            conn, "info", "news", "fetch",
            note_id=note_id, queries_tried=fetch_log.queries_tried, urls=fetch_log.urls,
            items_kept_per_step=fetch_log.items_kept_per_step, stopped_at=fetch_log.stopped_at,
            elapsed_seconds=round(fetch_log.elapsed_seconds, 2), items_returned=len(news_items),
        )
    else:
        db.log(conn, "info", "news", "skipped_not_newsworthy", note_id=note_id)

    draft = generate_draft(client, note_text=note_text, piece_type=piece_type, news_items=news_items)
    text = lint.autofix(draft.text)

    check = selfcheck_draft(client, draft_text=text, piece_type=piece_type)
    if not check.get("passed", True) and check.get("revised_text"):
        text = lint.autofix(check["revised_text"])

    result = lint.check(text)
    text = result.text

    if draft.used_news_item_index is not None:
        news_used = news_items[draft.used_news_item_index]
        news_item_id = db.insert_news_item(
            conn,
            note_id=note_id,
            title=news_used.title,
            source=news_used.source,
            published_at=news_used.published_at,
            link=news_used.link,
        )
        db.log(conn, "info", "news", "used", note_id=note_id, title=news_used.title, source=news_used.source)
    else:
        db.log(conn, "info", "news", "not_used", note_id=note_id, candidates=len(news_items))

    draft_id = db.insert_draft(
        conn,
        note_id=note_id,
        pick_id=pick_id,
        version=0,
        parent_draft_id=None,
        text=text,
        news_item_id=news_item_id,
        verify_count=lint.count_verify_tags(text),
        word_count=result.word_count,
        selfcheck_notes="; ".join(check.get("failed_items", [])) or None,
        lint_warnings=result.warnings,
    )

    return PipelineResult(
        draft_id=draft_id,
        text=text,
        word_count=result.word_count,
        verify_count=lint.count_verify_tags(text),
        warnings=result.warnings,
        news_item_id=news_item_id,
        news_used=news_used,
    )


def build_revision(
    conn: sqlite3.Connection,
    client: LLMClient,
    *,
    note_id: int,
    pick_id: Optional[int],
    parent_draft: sqlite3.Row,
    instruction: str,
    piece_type: str,
) -> PipelineResult:
    revised = revise_draft(
        client, draft_text=parent_draft["text"], instruction=instruction, piece_type=piece_type
    )
    result = lint.check(lint.autofix(revised["draft_text"]))

    draft_id = db.insert_draft(
        conn,
        note_id=note_id,
        pick_id=pick_id,
        version=parent_draft["version"] + 1,
        parent_draft_id=parent_draft["id"],
        text=result.text,
        news_item_id=parent_draft["news_item_id"],
        verify_count=lint.count_verify_tags(result.text),
        word_count=result.word_count,
        selfcheck_notes=None,
        lint_warnings=result.warnings,
        revision_instruction=instruction,
    )

    news_used = None
    if parent_draft["news_item_id"] is not None:
        row = conn.execute(
            "SELECT * FROM news_items WHERE id = ?", (parent_draft["news_item_id"],)
        ).fetchone()
        if row:
            news_used = NewsItem(
                title=row["title"], source=row["source"] or "",
                published_at=row["published_at"] or "", link=row["link"],
            )

    return PipelineResult(
        draft_id=draft_id,
        text=result.text,
        word_count=result.word_count,
        verify_count=lint.count_verify_tags(result.text),
        warnings=result.warnings,
        news_item_id=parent_draft["news_item_id"],
        news_used=news_used,
    )

"""Orchestrates the OpenAI calls for cleaning/tagging, scoring, drafting and
self-checking. Loads prompt templates from app/prompts/ and combines them
with the voice rules (app/voice.py) and example posts (app/published.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import lint, news_config, published, voice
from app.llm import LLMClient
from app.news import NewsItem

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# --- JSON schemas for structured OpenAI output ------------------------------

CLEAN_TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "clean_text": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "piece_type": {
                        "type": "string",
                        "enum": list(published.PIECE_TYPES),
                    },
                },
                "required": ["note_id", "clean_text", "tags", "piece_type"],
            },
        },
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_ids": {"type": "array", "items": {"type": "integer"}},
                    "rationale": {"type": "string"},
                },
                "required": ["note_ids", "rationale"],
            },
        },
    },
    "required": ["notes", "groups"],
}

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "integer"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["note_id", "score", "reason"],
            },
        }
    },
    "required": ["scores"],
}

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "post": {"type": "string"},
        "used_news": {"type": "boolean"},
        "news_index": {"type": "integer", "nullable": True},
    },
    "required": ["post", "used_news"],
}

NEWS_KEYWORDS_SCHEMA = {
    "type": "object",
    "properties": {
        "newsworthy": {"type": "boolean"},
        "category": {"type": "string", "enum": list(news_config.CATEGORIES)},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "specific_query": {"type": "string"},
        "broader_query": {"type": "string"},
    },
    "required": ["newsworthy"],
}

SELFCHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "failed_items": {"type": "array", "items": {"type": "string"}},
        "revised_text": {"type": "string"},
        "word_count": {"type": "integer"},
    },
    "required": ["passed", "failed_items", "revised_text", "word_count"],
}

REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "draft_text": {"type": "string"},
        "word_count": {"type": "integer"},
    },
    "required": ["draft_text", "word_count"],
}


# --- clean + tag + group -----------------------------------------------------

@dataclass
class CleanedNote:
    note_id: int
    clean_text: str
    tags: list[str]
    piece_type: str


@dataclass
class NoteGroup:
    note_ids: list[int]
    rationale: str


def clean_and_tag_batch(
    client: LLMClient, notes: list[dict]
) -> tuple[list[CleanedNote], list[NoteGroup]]:
    """notes: list of {"note_id": int, "text": str}."""
    prompt = _load_prompt("clean_tag.txt").format(
        notes_json=json.dumps(notes, ensure_ascii=False, indent=2)
    )
    result = client.generate_json(prompt, response_schema=CLEAN_TAG_SCHEMA)
    cleaned = [
        CleanedNote(
            note_id=n["note_id"],
            clean_text=n["clean_text"],
            tags=n.get("tags", [])[:3],
            piece_type=n.get("piece_type", "explainer"),
        )
        for n in result.get("notes", [])
    ]
    groups = [
        NoteGroup(note_ids=g["note_ids"], rationale=g["rationale"])
        for g in result.get("groups", [])
        if len(g.get("note_ids", [])) >= 2
    ]
    return cleaned, groups


# --- scoring -----------------------------------------------------------------

@dataclass
class NoteScore:
    note_id: int
    score: int
    reason: str


def score_batch(client: LLMClient, notes: list[dict]) -> list[NoteScore]:
    """notes: list of {"note_id": int, "text": str, "tags": list[str]}."""
    prompt = _load_prompt("score.txt").format(
        notes_json=json.dumps(notes, ensure_ascii=False, indent=2)
    )
    result = client.generate_json(prompt, response_schema=SCORE_SCHEMA)
    return [
        NoteScore(note_id=s["note_id"], score=max(0, min(10, int(s["score"]))), reason=s["reason"])
        for s in result.get("scores", [])
    ]




# --- news keywords -------------------------------------------------------------

@dataclass
class NewsKeywords:
    newsworthy: bool
    category: Optional[str] = None
    keywords: list[str] = field(default_factory=list)
    specific_query: Optional[str] = None
    broader_query: Optional[str] = None


def extract_news_keywords(client: LLMClient, *, note_text: str) -> NewsKeywords:
    """Step 1 of the news pipeline (see app/pipeline.py): classifies the note
    as newsworthy or not, picks a category, and produces the specific/broader
    query strings app/news.py tries in order. The "beat" query isn't part of
    this call - it's built deterministically from config/news_topics.yaml
    (app/news_config.beat_query_for), so the category terms stay
    config-editable rather than re-guessed by the model on every call.

    Gets only one retry (not the usual 5) - per spec, invalid JSON here means
    skip the news step entirely rather than hold up drafting over it.
    """
    prompt = _load_prompt("news_keywords.txt").format(note_text=note_text)
    try:
        result = client.generate_json(
            prompt, response_schema=NEWS_KEYWORDS_SCHEMA, schema_name="news_keywords", max_retries=2
        )
    except Exception:  # noqa: BLE001 - skip news, don't block drafting
        return NewsKeywords(newsworthy=False)

    if not result.get("newsworthy"):
        return NewsKeywords(newsworthy=False)

    return NewsKeywords(
        newsworthy=True,
        category=result.get("category"),
        keywords=list(result.get("keywords") or [])[:5],
        specific_query=result.get("specific_query"),
        broader_query=result.get("broader_query"),
    )


# --- drafting ------------------------------------------------------------------

def _build_news_block(news_items: list[NewsItem]) -> str:
    if not news_items:
        return "NEWS CANDIDATES: none found in the last 7 days for this topic."
    lines = ["NEWS CANDIDATES (from Google News, last 7 days - reference at most one, or none):"]
    for i, item in enumerate(news_items):
        lines.append(f"  [{i}] \"{item.title}\" - {item.source or 'unknown source'} ({item.published_at[:10]})")
    return "\n".join(lines)


def _voice_rules_with_examples(piece_type: str) -> str:
    rules = voice.build_voice_prompt()
    examples = published.examples_for(piece_type)
    example_blocks = []
    for post in examples:
        text = published.load_post_text(post)
        example_blocks.append(f"### Example ({post.id}, {post.piece_type})\n{text}")
    return rules + "\n\n## Example published LinkedIn posts\n\n" + "\n\n".join(example_blocks)


@dataclass
class DraftResult:
    text: str
    word_count: int
    used_news_item_index: Optional[int]


def generate_draft(
    client: LLMClient,
    *,
    note_text: str,
    piece_type: str,
    news_items: list[NewsItem],
) -> DraftResult:
    voice_rules = _voice_rules_with_examples(piece_type)
    prompt = _load_prompt("draft.txt").format(
        voice_rules=voice_rules,
        min_words=lint.MIN_WORDS,
        max_words=lint.MAX_WORDS,
        note_text=note_text,
        news_block=_build_news_block(news_items),
    )
    result = client.generate_json(prompt, response_schema=DRAFT_SCHEMA, schema_name="draft")
    idx = result.get("news_index") if result.get("used_news") else None
    # Guard against a hallucinated index or reference not actually in the feed.
    if idx is not None and (not isinstance(idx, int) or idx < 0 or idx >= len(news_items)):
        idx = None
    text = result["post"]
    if idx is not None:
        idx = _verify_news_reference_or_drop(text, news_items, idx)
    return DraftResult(text=text, word_count=len(text.split()), used_news_item_index=idx)


def _verify_news_reference_or_drop(text: str, news_items: list[NewsItem], idx: int) -> Optional[int]:
    """Belt-and-braces check: if the draft doesn't actually contain the
    referenced item's title/source, treat it as not using a news hook rather
    than trust the model's self-reported index.
    """
    item = news_items[idx]
    lower_text = text.lower()
    title_words = [w for w in item.title.lower().split() if len(w) > 4]
    overlap = sum(1 for w in title_words if w in lower_text)
    if overlap >= 2 or (item.source and item.source.lower() in lower_text):
        return idx
    return None


def selfcheck_draft(client: LLMClient, *, draft_text: str, piece_type: str) -> dict:
    voice_rules = _voice_rules_with_examples(piece_type)
    prompt = _load_prompt("selfcheck.txt").format(voice_rules=voice_rules, draft_text=draft_text)
    return client.generate_json(prompt, response_schema=SELFCHECK_SCHEMA)


def revise_draft(client: LLMClient, *, draft_text: str, instruction: str, piece_type: str) -> dict:
    voice_rules = _voice_rules_with_examples(piece_type)
    prompt = _load_prompt("revise.txt").format(
        voice_rules=voice_rules, draft_text=draft_text, instruction=instruction
    )
    return client.generate_json(prompt, response_schema=REVISE_SCHEMA)

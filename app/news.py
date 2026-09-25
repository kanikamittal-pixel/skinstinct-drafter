"""Google News RSS lookup, scoped to Meera's actual topics: skincare
ingredients, formulation, label claims and regulation, with an India focus
where relevant (app/news_config.py, config/news_topics.yaml).

Pipeline (see app/pipeline.py for the full orchestration):
  1. app/drafting.extract_news_keywords classifies the note and produces a
     specific_query and broader_query (an LLM call, not this module).
  2. fetch_ranked_news (here) tries, in order: the specific query, then the
     broader query, then a "beat" query built from config category terms -
     stopping at the first one that returns items surviving the filter.
     Max 3 requests, 4s per request, 8s total budget.
  3. Surviving items are ranked and the top 3 are returned for drafting to
     choose from (or none).

Nothing here fabricates or embellishes an item - only title, source, date
and link, exactly as the feed returned them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import httpx

from app.news_config import NewsTopicsConfig, load_news_topics

RSS_BASE = "https://news.google.com/rss/search"
LOOKBACK_DAYS = 7
RECENCY_BONUS_DAYS = 3
PER_REQUEST_TIMEOUT_SECONDS = 4.0
TOTAL_BUDGET_SECONDS = 8.0
MAX_REQUESTS = 3
MAX_RESULTS = 3
# Applied to every query, on top of when:7d - cuts down on the celebrity-
# skincare-routine and coupon-site noise a bare ingredient/claim term
# otherwise pulls in.
EXCLUDE_TERMS = "-celebrity -actress -discount -offers"


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published_at: str  # ISO 8601
    link: str


@dataclass
class NewsFetchLog:
    """What actually happened during a fetch, for app.db.log and the
    `python -m app news-test` CLI - requirement 6/7 of the news spec.
    """
    queries_tried: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    items_kept_per_step: list[int] = field(default_factory=list)
    stopped_at: Optional[str] = None  # "specific" | "broader" | "beat" | "budget_exhausted" | None
    elapsed_seconds: float = 0.0


def _feed_url(query: str) -> str:
    q = quote_plus(f"{query} when:{LOOKBACK_DAYS}d {EXCLUDE_TERMS}")
    return f"{RSS_BASE}?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def build_queries(
    *,
    specific_query: Optional[str],
    broader_query: Optional[str],
    category: Optional[str],
    add_india: bool,
    config: NewsTopicsConfig,
) -> list[tuple[str, str]]:
    """Returns [(label, query), ...] in try order: specific, broader, beat.
    India is appended only when add_india is True (regulation/claims/
    industry/climate categories - see app.news_config.INDIA_CATEGORIES).
    """
    suffix = " India" if add_india else ""
    queries: list[tuple[str, str]] = []
    if specific_query:
        queries.append(("specific", specific_query + suffix))
    if broader_query:
        queries.append(("broader", broader_query + suffix))
    if category:
        beat = config.beat_query_for(category)
        if beat:
            queries.append(("beat", beat + suffix))
    return queries


def _parse_entries(parsed, cutoff: datetime) -> list[NewsItem]:
    items: list[NewsItem] = []
    seen_links: set[str] = set()
    for entry in getattr(parsed, "entries", []):
        link = getattr(entry, "link", None)
        title = getattr(entry, "title", None)
        if not link or not title or link in seen_links:
            continue
        published_dt = None
        if getattr(entry, "published", None):
            try:
                published_dt = parsedate_to_datetime(entry.published)
                if published_dt.tzinfo is None:
                    published_dt = published_dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published_dt = None
        if published_dt is not None and published_dt < cutoff:
            continue
        source = ""
        if getattr(entry, "source", None) is not None:
            source = getattr(entry.source, "title", "") or ""
        seen_links.add(link)
        items.append(
            NewsItem(
                title=title, source=source,
                published_at=(published_dt or datetime.now(timezone.utc)).isoformat(),
                link=link,
            )
        )
    return items


def _fetch_query(query: str, *, timeout: float) -> list[NewsItem]:
    """Never raises - a failed/timed-out request is treated as zero results,
    not an error (the caller falls through to the next query).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(_feed_url(query))
            resp.raise_for_status()
            parsed = feedparser.parse(resp.text)
    except Exception:
        return []
    return _parse_entries(parsed, cutoff)


def _passes_filter(item: NewsItem, *, config: NewsTopicsConfig, keywords: list[str], category: Optional[str]) -> bool:
    if config.is_blocked(item.source):
        return False
    headline_lower = item.title.lower()
    keyword_hit = any(kw.lower() in headline_lower for kw in keywords)
    category_hit = bool(category) and config.matches_category_term(item.title, category)
    return keyword_hit or category_hit


def _rank_score(item: NewsItem, *, config: NewsTopicsConfig, keywords: list[str], category: Optional[str]) -> int:
    score = 0
    headline_lower = item.title.lower()
    if any(kw.lower() in headline_lower for kw in keywords):
        score += 3
    if category and config.matches_category_term(item.title, category):
        score += 1
    if config.is_trusted(item.source):
        score += 1
    try:
        published = datetime.fromisoformat(item.published_at)
        if datetime.now(timezone.utc) - published <= timedelta(days=RECENCY_BONUS_DAYS):
            score += 1
    except ValueError:
        pass
    return score


def fetch_ranked_news(
    *,
    specific_query: Optional[str],
    broader_query: Optional[str],
    category: Optional[str],
    keywords: list[str],
    add_india: bool,
    config: Optional[NewsTopicsConfig] = None,
) -> tuple[list[NewsItem], NewsFetchLog]:
    """Tries specific -> broader -> beat, stopping at the first query whose
    results survive the keyword/category-term filter. Max 3 requests total,
    4s per request, 8s total time budget. Returns the top MAX_RESULTS items
    (by _rank_score) from whichever query stopped it, or ([], log) if
    nothing survived any of the three.
    """
    cfg = config or load_news_topics()
    log = NewsFetchLog()
    start = time.monotonic()

    queries = build_queries(
        specific_query=specific_query, broader_query=broader_query,
        category=category, add_india=add_india, config=cfg,
    )

    for i, (label, query) in enumerate(queries):
        if i >= MAX_REQUESTS:
            break
        elapsed = time.monotonic() - start
        remaining = TOTAL_BUDGET_SECONDS - elapsed
        if remaining <= 0:
            log.stopped_at = "budget_exhausted"
            break

        log.queries_tried.append(f"{label}:{query}")
        log.urls.append(_feed_url(query))

        raw_items = _fetch_query(query, timeout=min(PER_REQUEST_TIMEOUT_SECONDS, remaining))
        filtered = [it for it in raw_items if _passes_filter(it, config=cfg, keywords=keywords, category=category)]
        log.items_kept_per_step.append(len(filtered))

        if filtered:
            log.stopped_at = label
            log.elapsed_seconds = time.monotonic() - start
            ranked = sorted(
                filtered,
                key=lambda it: _rank_score(it, config=cfg, keywords=keywords, category=category),
                reverse=True,
            )
            return ranked[:MAX_RESULTS], log

    log.elapsed_seconds = time.monotonic() - start
    return [], log

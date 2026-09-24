"""Google News RSS lookup for a note's topic tags.

Returns at most 3 candidates from the last 7 days: title, source, date, and
link only. Nothing here fabricates or embellishes an item - only what the
feed actually returned is ever passed on to the drafting prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import httpx

RSS_BASE = "https://news.google.com/rss/search"
LOOKBACK_DAYS = 7
MAX_ITEMS = 3
TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    published_at: str  # ISO 8601
    link: str


def _feed_url(query: str) -> str:
    q = quote_plus(f"{query} when:{LOOKBACK_DAYS}d")
    return f"{RSS_BASE}?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def _normalize_tag(tag: str) -> str:
    # "ph-testing" -> "pH testing": Google News matches natural-language
    # phrases, not hyphenated compound tokens nobody actually writes.
    return tag.replace("-", " ").strip()


def _query_once(query: str, *, max_items: int, timeout_seconds: float) -> list[NewsItem]:
    """Runs a single Google News RSS query and returns up to max_items
    results from the last LOOKBACK_DAYS, newest first. Never raises - a
    failed or empty query is a normal, expected outcome here.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    seen_links: set[str] = set()
    items: list[NewsItem] = []

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            resp = client.get(_feed_url(query))
            resp.raise_for_status()
            parsed = feedparser.parse(resp.text)
    except Exception:
        return []

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
                title=title,
                source=source,
                published_at=(published_dt or datetime.now(timezone.utc)).isoformat(),
                link=link,
            )
        )

    items.sort(key=lambda it: it.published_at, reverse=True)
    return items[:max_items]


def fetch_news_for_tags(
    tags: list[str], *, max_items: int = MAX_ITEMS, timeout_seconds: float = TIMEOUT_SECONDS
) -> list[NewsItem]:
    """Queries Google News RSS for the note's tags and returns up to
    max_items newest-first. Never raises on network/parse failure - a note
    with no news hook available is expected, not an error.

    Google News RSS's matching is loose enough that jamming all 3 tags into
    one query - which this used to always do - almost never returns zero
    results, which makes "stop at the first non-empty query" useless as a
    relevance check: a 3-tag query reliably returns *something*, but that
    something is frequently word-salad matches with nothing to do with the
    actual topic (verified live: "contract-manufacturer quality-control
    stability-testing" returned LED face mask and gel-powder-market
    listicles - it matched "manufacturer" in isolation, not the combined
    meaning). A single specific tag on its own consistently returned
    genuinely on-topic results in the same testing, and reliably returns
    *fewer* items when the topic really is narrow - so this tries the
    single most specific tag first, then widens to 2 and then 3 tags only
    if the narrower query came back empty. Narrow-but-relevant beats
    wide-but-spurious.

    timeout_seconds is overridable so a caller with its own hard deadline
    (the Vercel webhook - see app/webhook_config.py) can pass a tighter
    budget than the long-polling bot's default.
    """
    normalized = [_normalize_tag(t) for t in tags if t.strip()]
    if not normalized:
        return []

    for width in (1, 2, 3):
        query = " ".join(normalized[:width])
        if not query:
            continue
        items = _query_once(query, max_items=max_items, timeout_seconds=timeout_seconds)
        if items:
            return items
    return []

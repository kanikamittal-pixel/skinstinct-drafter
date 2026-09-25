"""Loads config/news_topics.yaml - the editable category terms, trusted
sources and blocked sources used by the news step (app/news.py). See that
file's own comments for what each field means.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "news_topics.yaml"

CATEGORIES = ("ingredients", "formulation", "regulation", "claims", "industry", "climate")
# Categories where an India-specific query term gets added (see app/drafting.py
# extract_news_keywords) - ingredient/formulation news is often global, but
# regulation, claims, industry and climate are where the India angle matters.
INDIA_CATEGORIES = ("regulation", "claims", "industry", "climate")


@dataclass(frozen=True)
class NewsTopicsConfig:
    category_terms: dict[str, list[str]]
    trusted_sources: list[str]
    blocked_sources: list[str]

    def beat_query_for(self, category: str) -> str:
        terms = self.category_terms.get(category, [])
        return " OR ".join(f'"{t}"' for t in terms)

    def is_trusted(self, source: str) -> bool:
        source_lower = source.lower()
        return any(t.lower() in source_lower for t in self.trusted_sources)

    def is_blocked(self, source: str) -> bool:
        source_lower = source.lower()
        return any(b.lower() in source_lower for b in self.blocked_sources)

    def matches_category_term(self, headline: str, category: str) -> bool:
        headline_lower = headline.lower()
        return any(t.lower() in headline_lower for t in self.category_terms.get(category, []))


_cache: NewsTopicsConfig | None = None


def load_news_topics(path: Path | None = None) -> NewsTopicsConfig:
    global _cache
    if path is None and _cache is not None:
        return _cache

    p = path or CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    category_terms = {
        name: list(cfg.get("terms", []))
        for name, cfg in (raw.get("categories") or {}).items()
    }
    config = NewsTopicsConfig(
        category_terms=category_terms,
        trusted_sources=list(raw.get("trusted_sources") or []),
        blocked_sources=list(raw.get("blocked_sources") or []),
    )
    if path is None:
        _cache = config
    return config

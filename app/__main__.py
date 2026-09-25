"""CLI entrypoint: python -m app <command>.

Currently one command:
  news-test --note "<text>"   Runs just the news step (keyword extraction +
                               ranked fetch) against a note, without
                               drafting or touching the database - prints
                               the keyword JSON, the queries/URLs tried, how
                               many items survived the filter at each step,
                               and the final top 3 (or none).
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import load_config
from app.drafting import extract_news_keywords
from app.llm import LLMClient
from app.news import fetch_ranked_news
from app.news_config import INDIA_CATEGORIES


def _news_test(note_text: str) -> None:
    config = load_config()
    client = LLMClient(config.openai_api_key, config.openai_model, timeout_seconds=15)

    keywords = extract_news_keywords(client, note_text=note_text)
    print("=== keyword extraction ===")
    print(json.dumps(
        {
            "newsworthy": keywords.newsworthy,
            "category": keywords.category,
            "keywords": keywords.keywords,
            "specific_query": keywords.specific_query,
            "broader_query": keywords.broader_query,
        },
        indent=2,
    ))

    if not keywords.newsworthy:
        print("\nnot newsworthy - no requests made")
        return

    add_india = keywords.category in INDIA_CATEGORIES
    items, log = fetch_ranked_news(
        specific_query=keywords.specific_query,
        broader_query=keywords.broader_query,
        category=keywords.category,
        keywords=keywords.keywords,
        add_india=add_india,
    )

    print(f"\n=== fetch (add_india={add_india}) ===")
    for i, (query, url) in enumerate(zip(log.queries_tried, log.urls)):
        kept = log.items_kept_per_step[i] if i < len(log.items_kept_per_step) else 0
        print(f"  [{i}] {query}")
        print(f"      url: {url}")
        print(f"      kept: {kept}")
    print(f"  stopped_at: {log.stopped_at}")
    print(f"  elapsed_seconds: {log.elapsed_seconds:.2f}")

    print(f"\n=== top {len(items)} (of up to 3) ===")
    for i, item in enumerate(items):
        print(f"  [{i}] \"{item.title}\" - {item.source} ({item.published_at[:10]})")
        print(f"      {item.link}")
    if not items:
        print("  none")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    subparsers = parser.add_subparsers(dest="command", required=True)

    news_test = subparsers.add_parser("news-test", help="Run just the news step against a note")
    news_test.add_argument("--note", required=True, help="The note text to test")

    args = parser.parse_args(argv)

    if args.command == "news-test":
        _news_test(args.note)


if __name__ == "__main__":
    main(sys.argv[1:])

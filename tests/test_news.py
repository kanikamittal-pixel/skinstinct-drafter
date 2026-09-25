from app.news import NewsItem, _passes_filter, _rank_score, build_queries, fetch_ranked_news
from app.news_config import load_news_topics


def test_no_queries_returns_empty_without_network(monkeypatch):
    import httpx

    def boom(*args, **kwargs):
        raise AssertionError("should not make any network call with no queries")

    monkeypatch.setattr(httpx, "Client", boom)
    items, log = fetch_ranked_news(
        specific_query=None, broader_query=None, category=None, keywords=[], add_india=False
    )
    assert items == []
    assert log.queries_tried == []


def test_network_failure_returns_empty_not_raises(monkeypatch):
    import httpx

    def boom(*args, **kwargs):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(httpx, "Client", boom)
    items, log = fetch_ranked_news(
        specific_query="niacinamide serum pH", broader_query="niacinamide skincare",
        category="ingredients", keywords=["niacinamide"], add_india=False,
    )
    assert items == []
    assert log.stopped_at is None


def test_build_queries_order_and_india_suffix():
    config = load_news_topics()
    queries = build_queries(
        specific_query="clinically tested claim expo", broader_query="cosmetic claims India",
        category="claims", add_india=True, config=config,
    )
    labels = [label for label, _ in queries]
    assert labels == ["specific", "broader", "beat"]
    assert all(q.endswith(" India") for _, q in queries)


def test_build_queries_no_india_suffix_when_not_flagged():
    config = load_news_topics()
    queries = build_queries(
        specific_query="niacinamide serum pH", broader_query="niacinamide skincare",
        category="ingredients", add_india=False, config=config,
    )
    assert all(not q.endswith(" India") for _, q in queries)


def test_build_queries_beat_uses_category_terms():
    config = load_news_topics()
    queries = build_queries(
        specific_query=None, broader_query=None, category="ingredients", add_india=False, config=config,
    )
    assert len(queries) == 1
    label, query = queries[0]
    assert label == "beat"
    assert "skincare ingredient" in query


def test_passes_filter_blocks_press_release_wires():
    config = load_news_topics()
    item = NewsItem(title="New niacinamide serum launches", source="PR Newswire", published_at="", link="x")
    assert _passes_filter(item, config=config, keywords=["niacinamide"], category=None) is False


def test_passes_filter_requires_keyword_or_category_match():
    config = load_news_topics()
    on_topic = NewsItem(title="Niacinamide market grows in 2026", source="Reuters", published_at="", link="a")
    off_topic = NewsItem(title="Local team wins championship", source="Reuters", published_at="", link="b")
    assert _passes_filter(on_topic, config=config, keywords=["niacinamide"], category=None) is True
    assert _passes_filter(off_topic, config=config, keywords=["niacinamide"], category=None) is False


def test_rank_score_rewards_keyword_trust_and_recency():
    import datetime as dt

    config = load_news_topics()
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    strong = NewsItem(title="Niacinamide regulation update", source="Reuters", published_at=now_iso, link="a")
    weak = NewsItem(title="Skincare industry roundup", source="Unknown Blog", published_at="2000-01-01T00:00:00+00:00", link="b")
    config_score_strong = _rank_score(strong, config=config, keywords=["niacinamide"], category="regulation")
    config_score_weak = _rank_score(weak, config=config, keywords=["niacinamide"], category="regulation")
    assert config_score_strong > config_score_weak

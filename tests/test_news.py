from app.news import fetch_news_for_tags


def test_empty_tags_returns_empty_list():
    assert fetch_news_for_tags([]) == []


def test_network_failure_returns_empty_not_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("no network in tests")

    import httpx

    monkeypatch.setattr(httpx, "Client", boom)
    assert fetch_news_for_tags(["niacinamide"]) == []

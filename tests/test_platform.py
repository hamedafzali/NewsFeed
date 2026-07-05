import os

import pytest

from news_platform.app import create_app
from news_platform.config import Settings
from news_platform.models import RawArticle, Source
from news_platform.pipeline.enrichment import EnrichmentEngine
from news_platform.pipeline.extraction import ContentExtractor
from news_platform.pipeline.ranking import RankingEngine
from news_platform.storage import SQLiteStore


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("API_KEY", raising=False)
    app = create_app()
    app.config.update(TESTING=True)
    return app


def test_health_contract(app):
    response = app.test_client().get("/api/v1/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "healthy"
    assert "request_id" in payload["meta"]


def test_auth_when_api_key_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "secret")
    app = create_app()
    client = app.test_client()
    assert client.get("/api/v1/stats").status_code == 401
    assert client.get("/api/v1/stats", headers={"X-API-Key": "secret"}).status_code == 200


def test_storage_upserts_sources_and_lists_articles(tmp_path):
    store = SQLiteStore(str(tmp_path))
    source_id = store.upsert_source(Source(url="https://example.com/rss", name="Example"))
    assert source_id > 0
    assert any(source["name"] == "Example" for source in store.list_sources())
    assert store.stats()["total_articles"] == 0


def test_extraction_removes_boilerplate():
    extractor = ContentExtractor(Settings())
    raw = RawArticle(
        title="Important science discovery",
        url="https://example.com/a",
        source_name="Example",
        source_url="https://example.com/rss",
        summary_html="<article><p>Scientists found a useful result for patients.</p><p>Subscribe to our newsletter.</p></article>",
    )
    article = extractor.extract(raw, skip_web=True)
    assert "newsletter" not in article.text.lower()
    assert article.content_hash


def test_enrichment_outputs_topic_summary_keywords():
    raw = RawArticle(
        title="AI startup launches new cloud software",
        url="https://example.com/a",
        source_name="Example",
        source_url="https://example.com/rss",
        summary_html="AI startup launches new cloud software. The technology helps developers ship faster. Investors said the market is growing.",
    )
    article = ContentExtractor(Settings()).extract(raw, skip_web=True)
    enrichment = EnrichmentEngine()
    assert enrichment.classify_topic(article) == "tech"
    assert enrichment.summarize(article).tldr
    assert "startup" in enrichment.keywords(article)


def test_ranking_returns_bounded_scores():
    raw = RawArticle(
        title="Markets rally after bank earnings",
        url="https://example.com/a",
        source_name="Example",
        source_url="https://example.com/rss",
        summary_html="Markets rally after bank earnings. Investors watched inflation data and central bank signals.",
    )
    article = ContentExtractor(Settings()).extract(raw, skip_web=True)
    relevance, quality = RankingEngine().score(article, source_reliability=0.9)
    assert 0 <= relevance <= 1
    assert 0 <= quality <= 1


def test_refresh_with_mocked_ingestor(app, monkeypatch):
    raw = RawArticle(
        title="AI startup launches new cloud software",
        url="https://example.com/article",
        source_name="Example",
        source_url="https://example.com/rss",
        summary_html="AI startup launches new cloud software. The technology helps developers ship faster. Investors said the market is growing.",
    )
    pipeline = app.extensions["pipeline"]
    monkeypatch.setattr(pipeline.ingestor, "fetch", lambda *args, **kwargs: [raw])
    response = app.test_client().post("/api/v1/refresh", json={"query": "AI", "limit": 1})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["inserted"] == 1
    assert payload["data"]["articles"][0]["topic"] == "tech"

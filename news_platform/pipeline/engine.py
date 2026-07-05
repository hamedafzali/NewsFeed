import time
from typing import Any, Dict, List

from news_platform.config import Settings
from news_platform.models import IntelligenceArticle, PipelineResult
from news_platform.pipeline.enrichment import EnrichmentEngine
from news_platform.pipeline.extraction import ContentExtractor
from news_platform.pipeline.ingestion import FeedIngestor
from news_platform.pipeline.ranking import RankingEngine
from news_platform.storage.sqlite_store import SQLiteStore
from news_platform.utils.http import normalize_url


class NewsPipeline:
    def __init__(self, settings: Settings, store: SQLiteStore):
        self.settings = settings
        self.store = store
        self.ingestor = FeedIngestor(settings)
        self.extractor = ContentExtractor(settings)
        self.enrichment = EnrichmentEngine()
        self.ranking = RankingEngine()

    def refresh(self, query: str = "", language: str = "en", country: str = "US", limit: int = 25, skip_web: bool = True, translate: bool = False, request_id: str = "", sources: List[Dict[str, Any]] | None = None) -> PipelineResult:
        started = time.time()
        result = PipelineResult()
        if sources is None:
            sources = self.store.list_sources(active_only=True)
        raw_articles = self.ingestor.fetch(sources, query=query, language=language, country=country)
        result.fetched = len(raw_articles)
        for raw in raw_articles[:limit]:
            source_meta = self._source_meta(raw.source_url, sources)
            try:
                normalized_url = normalize_url(raw.url)
                extracted = self.extractor.extract(raw, skip_web=skip_web)
                if self.store.article_exists(normalized_url, extracted.content_hash):
                    result.skipped_duplicates += 1
                    continue
                topic = self.enrichment.classify_topic(extracted)
                keywords = self.enrichment.keywords(extracted)
                entities = self.enrichment.entities(extracted)
                summary = self.enrichment.summarize(extracted, translate=translate)
                relevance, quality = self.ranking.score(extracted, source_meta.get("reliability", 0.7), topic_match=True)
                article = IntelligenceArticle(
                    id=None,
                    title=extracted.title,
                    url=extracted.url,
                    source_name=extracted.source_name or source_meta.get("name", "Unknown"),
                    source_url=extracted.source_url,
                    text=extracted.text,
                    published_at=extracted.published_at,
                    author=extracted.author,
                    language=extracted.language,
                    topic=topic,
                    keywords=keywords,
                    people=entities["people"],
                    organizations=entities["organizations"],
                    locations=entities["locations"],
                    content_hash=extracted.content_hash,
                    relevance_score=relevance,
                    quality_score=quality,
                    extraction_quality=extracted.extraction_quality,
                    summary=summary,
                    image_url=extracted.image_url,
                )
                article_id, inserted = self.store.upsert_article(article, normalized_url)
                article.id = article_id
                result.articles.append(article)
                result.processed += 1
                if inserted:
                    result.inserted += 1
                else:
                    result.updated += 1
            except Exception as exc:
                result.errors += 1
                self.store.error("pipeline", exc, request_id=request_id, source_url=raw.source_url, article_url=raw.url)
        result.duration_ms = int((time.time() - started) * 1000)
        self.store.log("pipeline", "success" if result.errors == 0 else "partial", request_id=request_id, duration_ms=result.duration_ms, message=f"fetched={result.fetched} processed={result.processed}")
        return result

    def process_article(self, article_id: int, request_id: str = "") -> Dict[str, Any]:
        article = self.store.get_article(article_id)
        if not article:
            raise ValueError("article not found")
        self.store.log("process", "success", request_id=request_id, article_url=article["url"], message="article already processed")
        return article

    def _source_meta(self, source_url: str, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
        for source in sources:
            if source["url"] == source_url:
                return source
        return {"name": "Unknown", "reliability": 0.7}

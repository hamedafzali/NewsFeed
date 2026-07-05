from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import exp

from news_platform.models import ExtractedArticle


class RankingEngine:
    def score(self, article: ExtractedArticle, source_reliability: float = 0.7, topic_match: bool = True) -> tuple[float, float]:
        freshness = self._freshness(article.published_at)
        richness = min(len(article.text) / 1600, 1.0)
        extraction = article.extraction_quality
        topic = 1.0 if topic_match else 0.55
        quality = round((0.45 * richness) + (0.35 * extraction) + (0.20 * source_reliability), 3)
        relevance = round((0.30 * freshness) + (0.25 * source_reliability) + (0.25 * quality) + (0.20 * topic), 3)
        return min(relevance, 1.0), min(quality, 1.0)

    def _freshness(self, published_at: str | None) -> float:
        if not published_at:
            return 0.55
        try:
            dt = parsedate_to_datetime(published_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
            return max(0.05, exp(-hours / 72))
        except Exception:
            return 0.55

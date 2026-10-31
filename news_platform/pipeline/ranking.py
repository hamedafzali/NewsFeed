from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import exp
from typing import Optional

from news_platform.models import ExtractedArticle


def parse_published(value: Optional[str]) -> Optional[datetime]:
    """Parse a feed date: RFC 2822 (RSS) or ISO 8601 (Atom/RDF, e.g. DW). None if unparseable."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_hours(published_at: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    dt = parse_published(published_at)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600)


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
        hours = age_hours(published_at)
        if hours is None:
            return 0.55
        return max(0.05, exp(-hours / 72))

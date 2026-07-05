from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from news_platform.config import get_settings
from news_platform.models import IntelligenceArticle
from news_platform.pipeline import NewsPipeline
from news_platform.storage import SQLiteStore


@dataclass
class NewsResult:
    processed: int = 0
    posted: int = 0
    duration: float = 0.0
    status: str = "success"
    error_message: Optional[str] = None
    articles: List[Dict[str, Any]] = field(default_factory=list)


class NewsProcessor:
    """Compatibility adapter over the v3 NewsPipeline."""

    def __init__(self, bot_config: Dict[str, Any]):
        self.config = bot_config
        self.settings = get_settings()
        self.store = SQLiteStore(self.settings.data_dir)
        self.pipeline = NewsPipeline(self.settings, self.store)

    def process_news(self) -> NewsResult:
        try:
            result = self.pipeline.refresh(
                query=self.config.get("city_name", ""),
                language=self.config.get("news_language", "en"),
                country=self.config.get("country_code", "US"),
                limit=int(self.config.get("max_posts_per_run", 10)),
                skip_web=bool(self.config.get("skip_scraping", True)),
                translate=bool(self.config.get("translate", False)),
            )
            return NewsResult(
                processed=result.processed,
                posted=0,
                duration=result.duration_ms / 1000,
                status="success",
                articles=[article.to_dict() for article in result.articles],
            )
        except Exception as exc:
            return NewsResult(status="error", error_message=str(exc))

    def get_stats(self) -> Dict[str, Any]:
        stats = self.store.stats()
        return {
            "total_articles": stats["total_articles"],
            "posted_articles": 0,
            "unposted_articles": stats["total_articles"],
            "last_posted_at": stats.get("last_pipeline_event"),
        }

    def test_connection(self) -> bool:
        return True

    def get_sentiment(self, titles: List[str], finbert_url: str) -> Dict[str, Any]:
        return {}

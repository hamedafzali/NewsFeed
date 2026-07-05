from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Source:
    url: str
    name: str = ""
    reliability: float = 0.7
    active: bool = True
    topic_hint: str = ""


@dataclass
class RawArticle:
    title: str
    url: str
    source_name: str
    source_url: str
    published_at: Optional[str] = None
    author: Optional[str] = None
    summary_html: str = ""
    language: str = "en"
    image_url: Optional[str] = None


@dataclass
class ExtractedArticle:
    title: str
    url: str
    source_name: str
    source_url: str
    text: str
    published_at: Optional[str] = None
    author: Optional[str] = None
    language: str = "en"
    image_url: Optional[str] = None
    content_hash: str = ""
    extraction_quality: float = 0.0


@dataclass
class SummaryBundle:
    tldr: str
    bullets: List[str] = field(default_factory=list)
    full: str = ""
    translated: Optional[str] = None


@dataclass
class IntelligenceArticle:
    id: Optional[int]
    title: str
    url: str
    source_name: str
    source_url: str
    text: str
    published_at: Optional[str]
    author: Optional[str]
    language: str
    topic: str
    keywords: List[str]
    people: List[str]
    organizations: List[str]
    locations: List[str]
    content_hash: str
    relevance_score: float
    quality_score: float
    extraction_quality: float
    summary: SummaryBundle
    image_url: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["summary"] = asdict(self.summary)
        return data


@dataclass
class PipelineResult:
    fetched: int = 0
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_duplicates: int = 0
    errors: int = 0
    duration_ms: int = 0
    articles: List[IntelligenceArticle] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fetched": self.fetched,
            "processed": self.processed,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_duplicates": self.skipped_duplicates,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "articles": [article.to_dict() for article in self.articles],
        }

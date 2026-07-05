import logging
import re
import sys
import types
from typing import Any, Dict, Iterable, List

import requests
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from news_platform.config import Settings
from news_platform.models import RawArticle
from news_platform.utils.http import normalize_url

USER_AGENT = "NewsIntelligencePlatform/3.0 (+https://localhost)"

if "cgi" not in sys.modules:
    cgi_compat = types.ModuleType("cgi")

    def parse_header(value: str):
        parts = [part.strip() for part in (value or "").split(";")]
        key = parts[0].lower() if parts else ""
        params = {}
        for part in parts[1:]:
            if "=" in part:
                name, raw = part.split("=", 1)
                params[name.strip().lower()] = raw.strip().strip('"')
        return key, params

    cgi_compat.parse_header = parse_header
    sys.modules["cgi"] = cgi_compat

import feedparser


class FeedIngestor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = logging.getLogger(__name__)

    def fetch(self, sources: Iterable[Dict[str, Any]], query: str = "", language: str = "en", country: str = "US") -> List[RawArticle]:
        articles: List[RawArticle] = []
        for source in sources:
            try:
                articles.extend(self.fetch_source(source, query=query, language=language, country=country))
            except Exception as exc:
                self.logger.warning("source fetch failed: %s", exc)
        return self._dedupe(articles)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.3, min=0.3, max=2))
    def fetch_source(self, source: Dict[str, Any], query: str = "", language: str = "en", country: str = "US") -> List[RawArticle]:
        url = source["url"].format(
            query=requests.utils.quote(query or source.get("topic_hint") or "news"),
            city=requests.utils.quote(query or "news"),
            language=language,
            country=country,
        )
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.settings.request_timeout_seconds)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        entries = feed.entries[: self.settings.max_feed_entries]
        source_name = source.get("name") or feed.feed.get("title") or url
        return [self._entry_to_article(entry, source, source_name, url, feed.feed.get("language", language)) for entry in entries if entry.get("link")]

    def _entry_to_article(self, entry: Any, source: Dict[str, Any], source_name: str, source_url: str, feed_language: str) -> RawArticle:
        summary_html = entry.get("summary") or entry.get("description") or ""
        image_url = self._extract_image(entry, summary_html)
        title = BeautifulSoup(entry.get("title", "").strip(), "html.parser").get_text(" ").strip()
        language = (feed_language or source.get("language") or "en").split("-")[0].lower()
        publisher = entry.get("source", {}).get("title") or source_name
        return RawArticle(
            title=title,
            url=entry.get("link", ""),
            source_name=publisher,
            source_url=source.get("url", source_url),
            published_at=entry.get("published") or entry.get("updated"),
            author=entry.get("author"),
            summary_html=summary_html,
            language=language,
            image_url=image_url,
        )

    def _extract_image(self, entry: Any, summary_html: str) -> str | None:
        if entry.get("media_content"):
            return entry.media_content[0].get("url")
        if entry.get("media_thumbnail"):
            return entry.media_thumbnail[0].get("url")
        for enclosure in entry.get("enclosures", []) or []:
            if enclosure.get("type", "").startswith("image"):
                return enclosure.get("href") or enclosure.get("url")
        match = re.search(r'src=["\']([^"\']+\.(?:jpg|jpeg|png|webp|gif))', summary_html or "", re.I)
        return match.group(1) if match else None

    def _dedupe(self, articles: List[RawArticle]) -> List[RawArticle]:
        seen = set()
        unique = []
        for article in articles:
            key = normalize_url(article.url)
            if key and key not in seen:
                seen.add(key)
                unique.append(article)
        return unique

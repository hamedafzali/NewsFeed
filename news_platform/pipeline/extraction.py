import hashlib
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from news_platform.config import Settings
from news_platform.models import ExtractedArticle, RawArticle
from news_platform.pipeline.ingestion import USER_AGENT


BOILERPLATE_PATTERNS = [
    r"subscribe to our newsletter",
    r"all rights reserved",
    r"cookie policy",
    r"advertisement",
    r"related articles",
]


class ContentExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def extract(self, raw: RawArticle, skip_web: bool = False) -> ExtractedArticle:
        html = "" if skip_web else self._fetch_html(raw.url)
        text = self._html_to_text(html) if html else ""
        if len(text) < 240:
            text = self._html_to_text(raw.summary_html)
        if len(text) < 80:
            text = raw.title
        text = self._clean_text(text)
        content_hash = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()
        quality = self._quality_score(text, html)
        return ExtractedArticle(
            title=raw.title,
            url=raw.url,
            source_name=raw.source_name,
            source_url=raw.source_url,
            text=text,
            published_at=raw.published_at,
            author=raw.author,
            language=raw.language,
            image_url=raw.image_url,
            content_hash=content_hash,
            extraction_quality=quality,
        )

    def _fetch_html(self, url: str) -> str:
        if not url or "news.google.com" in url:
            return ""
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=self.settings.request_timeout_seconds,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            return ""
        return response.text

    def _html_to_text(self, html: str) -> str:
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        candidates = soup.select("article, main, [role='main']")
        container = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=soup)
        return container.get_text(" ", strip=True)

    def _clean_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        sentences = []
        seen = set()
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            normalized = sentence.lower().strip()
            if not normalized or normalized in seen:
                continue
            if any(re.search(pattern, normalized) for pattern in BOILERPLATE_PATTERNS):
                continue
            seen.add(normalized)
            sentences.append(sentence.strip())
        return " ".join(sentences).strip()

    def _quality_score(self, text: str, html: str) -> float:
        length_score = min(len(text) / 1800, 1.0)
        structure_bonus = 0.15 if html and ("<article" in html.lower() or "<main" in html.lower()) else 0.0
        return round(min(1.0, length_score + structure_bonus), 3)

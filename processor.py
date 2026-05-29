"""
News Processor - Core News Processing Logic
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import feedparser
import requests
import telegram
from newspaper import Article
from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_fixed,
)

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
RELEVANCE_THRESHOLD = 0.3
TELEGRAM_POST_DELAY = 1.0  # seconds between posts to avoid flood limits


@dataclass
class NewsResult:
    processed: int = 0
    posted: int = 0
    duration: float = 0.0
    status: str = "success"
    error_message: Optional[str] = None
    articles: List[Dict[str, Any]] = field(default_factory=list)


def _run_async(coro):
    """Run an async coroutine from sync code using a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class NewsProcessor:
    """Processes news for a single bot configuration."""

    def __init__(self, bot_config: Dict[str, Any]):
        self.config = bot_config
        self.logger = logging.getLogger(__name__)
        self.telegram_bot = telegram.Bot(token=bot_config["bot_token"])

        self.openai_client: Optional[OpenAI] = None
        self.model = bot_config.get("model", "gpt-4o-mini")
        if bot_config.get("openai_api_key") or bot_config.get("ollama_base_url"):
            self.openai_client = OpenAI(
                api_key=bot_config.get("openai_api_key", "ollama"),
                base_url=bot_config.get("ollama_base_url"),  # None = default OpenAI
            )

        os.makedirs(DATA_DIR, exist_ok=True)
        self.db_path = os.path.join(DATA_DIR, f"bot_{bot_config.get('bot_id', 'default')}.db")
        self._init_bot_database()

    def _init_bot_database(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    url TEXT UNIQUE NOT NULL,
                    source TEXT,
                    published_at TIMESTAMP,
                    summary_en TEXT,
                    summary_fa TEXT,
                    relevance_score REAL,
                    posted BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def process_news(self) -> NewsResult:
        start_time = datetime.now()
        result = NewsResult()
        try:
            self.logger.info(f"Starting news processing for {self.config['city_name']}")
            articles = self._fetch_articles()
            result.processed = len(articles)

            max_posts = self.config.get("max_posts_per_run", 5)
            for article in articles[:max_posts]:
                if self._process_and_post_article(article):
                    result.posted += 1
                    result.articles.append(article)
                    time.sleep(TELEGRAM_POST_DELAY)

            result.status = "success"
            self.logger.info(f"Completed: {result.processed} fetched, {result.posted} posted")
        except Exception as e:
            result.status = "error"
            result.error_message = str(e)
            self.logger.error(f"Processing error: {e}")
        finally:
            result.duration = (datetime.now() - start_time).total_seconds()
        return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_articles(self) -> List[Dict[str, Any]]:
        articles = []
        city = self.config["city_name"]
        lang = self.config["news_language"]
        country = self.config["country_code"]

        rss_url = f"https://news.google.com/rss/search?q={city}&hl={lang}&gl={country}"
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:20]:
            articles.append({
                "title": entry.title,
                "url": entry.link,
                "source": entry.get("source", {}).get("title", "Unknown"),
                "published_at": entry.get("published"),
            })

        newsapi_key = self.config.get("newsapi_key", "")
        if newsapi_key and newsapi_key != "your_newsapi_key_here":
            articles.extend(self._fetch_from_newsapi())

        self.logger.info(f"Fetched {len(articles)} articles")
        return articles

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _fetch_from_newsapi(self) -> List[Dict[str, Any]]:
        response = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": self.config["city_name"],
                "language": self.config["news_language"],
                "sortBy": "publishedAt",
                "pageSize": 20,
                "apiKey": self.config["newsapi_key"],
            },
            timeout=10,
        )
        if response.status_code == 200:
            return [
                {
                    "title": a["title"],
                    "url": a["url"],
                    "source": a["source"]["name"],
                    "published_at": a["publishedAt"],
                }
                for a in response.json().get("articles", [])
            ]
        return []

    def _process_and_post_article(self, article: Dict[str, Any]) -> bool:
        try:
            if self._article_exists(article["url"]):
                return False

            content = self._extract_content(article["url"])
            if not content or len(content) < 200:
                self.logger.info(f"Skipping short/empty content: {article['url']}")
                return False

            relevance_score = self._calculate_relevance(article["title"], content)
            if relevance_score < RELEVANCE_THRESHOLD:
                self.logger.info(f"Not relevant ({relevance_score:.2f}): {article['title']}")
                return False

            summaries = self._summarize_and_translate(content)
            if not summaries:
                self.logger.warning(f"Summarization failed: {article['title']}")
                return False

            success = self._post_to_telegram(
                title=article["title"],
                summary=summaries["summary_fa"],
                url=article["url"],
                source=article.get("source", "Unknown"),
            )
            self._save_article(article, summaries["summary_en"], summaries["summary_fa"],
                               relevance_score, posted=success)
            return success
        except Exception as e:
            self.logger.error(f"Error processing '{article.get('title', '')}': {e}")
            return False

    def _article_exists(self, url: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT 1 FROM articles WHERE url = ?", (url,)
            ).fetchone() is not None

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(2),
           retry=retry_if_exception_type(Exception))
    def _extract_content(self, url: str) -> Optional[str]:
        try:
            article = Article(url)
            article.download()
            article.parse()
            return article.text
        except Exception as e:
            self.logger.warning(f"Content extraction failed for {url}: {e}")
            return None

    def _calculate_relevance(self, title: str, content: str) -> float:
        """LLM relevance scoring when OpenAI is available, keyword fallback otherwise."""
        if self.openai_client:
            try:
                city = self.config["city_name"]
                response = self.openai_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                f"Determine if this news article is specifically about {city}. "
                                f"Reply with only valid JSON: "
                                f'{{\"relevant\": true/false, \"score\": 0.0-1.0}}'
                            ),
                        },
                        {"role": "user", "content": f"Title: {title}\n\nContent: {content[:500]}"},
                    ],
                    max_tokens=40,
                    temperature=0,
                )
                data = json.loads(response.choices[0].message.content.strip())
                return float(data.get("score", 0.0))
            except Exception as e:
                self.logger.warning(f"LLM relevance check failed, using keyword fallback: {e}")

        # Keyword fallback (no OpenAI key or LLM call failed)
        city = self.config["city_name"].lower()
        country = self.config["country_code"].lower()
        title_l = title.lower()
        content_l = content.lower()
        score = 0.0
        if city in title_l:
            score += 0.5
        if city in content_l:
            score += 0.3
        if country in title_l:
            score += 0.2
        return min(score, 1.0)

    def _summarize_and_translate(self, content: str) -> Optional[Dict[str, str]]:
        """Single OpenAI call producing both English summary and Persian translation."""
        if self.openai_client:
            try:
                response = self.openai_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Summarize the following news article in 2-3 sentences. "
                                "Return valid JSON with exactly two fields: "
                                "'summary_en' (English summary) and "
                                "'summary_fa' (Persian translation of the summary)."
                            ),
                        },
                        {"role": "user", "content": content[:2000]},
                    ],
                    max_tokens=400,
                    temperature=0.3,
                )
                return json.loads(response.choices[0].message.content.strip())
            except Exception as e:
                self.logger.error(f"Summarization error: {e}")
                return None

        # Fallback when no OpenAI key
        excerpt = content[:200] + ("..." if len(content) > 200 else "")
        return {"summary_en": excerpt, "summary_fa": excerpt}

    def _post_to_telegram(self, title: str, summary: str, url: str, source: str) -> bool:
        message = (
            f"<b>{title}</b>\n\n"
            f"{summary}\n\n"
            f'<a href="{url}">متن کامل</a> | {source}'
        )

        async def _send():
            async with self.telegram_bot:
                await self.telegram_bot.send_message(
                    chat_id=self.config["telegram_chat_id"],
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )

        try:
            _run_async(_send())
            return True
        except telegram.error.RetryAfter as e:
            self.logger.warning(f"Telegram rate limit hit, waiting {e.retry_after}s")
            time.sleep(e.retry_after)
            try:
                _run_async(_send())
                return True
            except Exception as retry_err:
                self.logger.error(f"Telegram retry failed: {retry_err}")
                return False
        except Exception as e:
            self.logger.error(f"Telegram posting error: {e}")
            return False

    def _save_article(self, article: Dict[str, Any], summary_en: str, summary_fa: str,
                      relevance_score: float, posted: bool):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO articles
                (title, url, source, published_at, summary_en, summary_fa, relevance_score, posted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article["title"], article["url"], article.get("source"),
                    article.get("published_at"), summary_en, summary_fa,
                    relevance_score, posted,
                ),
            )

    def test_connection(self) -> bool:
        async def _test():
            async with self.telegram_bot:
                return await self.telegram_bot.get_me()
        try:
            _run_async(_test())
            return True
        except Exception as e:
            self.logger.error(f"Connection test failed: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
                posted = conn.execute(
                    "SELECT COUNT(*) FROM articles WHERE posted = 1"
                ).fetchone()[0]
                last_posted = conn.execute(
                    "SELECT created_at FROM articles WHERE posted = 1 ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                return {
                    "total_articles": total,
                    "posted_articles": posted,
                    "unposted_articles": total - posted,
                    "last_posted_at": last_posted[0] if last_posted else None,
                }
        except Exception as e:
            self.logger.error(f"Stats error: {e}")
            return {"total_articles": 0, "posted_articles": 0, "unposted_articles": 0,
                    "last_posted_at": None}

"""
News Processor - Core News Processing Logic
"""
import asyncio
import html as html_lib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests
import telegram
from bs4 import BeautifulSoup
from newspaper import Article
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
RELEVANCE_THRESHOLD = 0.3
TELEGRAM_POST_DELAY = 1.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Content quality levels (best → worst)
QUALITY_FULL = "full_article"      # scraped body ≥300 chars
QUALITY_RSS  = "rss_description"   # RSS description/summary field
QUALITY_TITLE = "title_only"       # nothing but the headline


@dataclass
class NewsResult:
    processed: int = 0
    posted: int = 0
    duration: float = 0.0
    status: str = "success"
    error_message: Optional[str] = None
    articles: List[Dict[str, Any]] = field(default_factory=list)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _clean_html(raw: str) -> str:
    """Strip HTML tags, decode entities, normalise whitespace."""
    text = BeautifulSoup(raw, "html.parser").get_text(separator=" ")
    text = html_lib.unescape(text)
    return " ".join(text.split()).strip()


class NewsProcessor:
    """Processes news for a single bot configuration."""

    def __init__(self, bot_config: Dict[str, Any]):
        self.config = bot_config
        self.logger = logging.getLogger(__name__)
        self.telegram_bot = telegram.Bot(token=bot_config["bot_token"])

        # LLM client — OpenAI key takes priority; fall back to Ollama env vars
        self.openai_client: Optional[OpenAI] = None
        ollama_base_url = bot_config.get("ollama_base_url") or os.getenv("OLLAMA_BASE_URL")
        self.model = (
            bot_config.get("model") or os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
            if ollama_base_url
            else bot_config.get("model", "gpt-4o-mini")
        )
        if bot_config.get("openai_api_key"):
            self.openai_client = OpenAI(api_key=bot_config["openai_api_key"])
        elif ollama_base_url:
            self.openai_client = OpenAI(api_key="ollama", base_url=ollama_base_url)

        os.makedirs(DATA_DIR, exist_ok=True)
        self.db_path = os.path.join(DATA_DIR, f"bot_{bot_config.get('bot_id', 'default')}.db")
        self._init_bot_database()

    # ── Database ──────────────────────────────────────────────────────────────

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
                    content_quality TEXT,
                    posted BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # migration: add content_quality if old schema
            try:
                conn.execute("ALTER TABLE articles ADD COLUMN content_quality TEXT")
            except Exception:
                pass

    # ── Main pipeline ─────────────────────────────────────────────────────────

    def process_news(self) -> NewsResult:
        start_time = datetime.now()
        result = NewsResult()
        try:
            self.logger.info(f"Starting for {self.config['city_name']}")
            articles = self._fetch_articles()
            result.processed = len(articles)

            max_posts = self.config.get("max_posts_per_run", 5)
            for article in articles:
                if result.posted >= max_posts:
                    break
                if self._process_and_post_article(article):
                    result.posted += 1
                    result.articles.append(article)
                    time.sleep(TELEGRAM_POST_DELAY)

            result.status = "success"
            self.logger.info(f"Done: {result.processed} fetched, {result.posted} posted")
        except Exception as e:
            result.status = "error"
            result.error_message = str(e)
            self.logger.error(f"Processing error: {e}")
        finally:
            result.duration = (datetime.now() - start_time).total_seconds()
        return result

    # ── Fetching ──────────────────────────────────────────────────────────────

    def _fetch_articles(self) -> List[Dict[str, Any]]:
        articles = []
        sources = self.config.get("sources") or []
        for source in sources:
            feed_articles = self._fetch_from_rss(source["url"])
            if source.get("bypass_relevance"):
                for a in feed_articles:
                    a["_bypass_relevance"] = True
            articles.extend(feed_articles)

        # Deduplicate by URL
        seen: set = set()
        unique = []
        for a in articles:
            if a["url"] not in seen:
                seen.add(a["url"])
                unique.append(a)

        self.logger.info(f"Fetched {len(unique)} unique articles from {len(sources)} sources")
        return unique

    def _fetch_from_rss(self, feed_url: str) -> List[Dict[str, Any]]:
        """Parse an RSS/Atom feed. Extracts description/content when available."""
        try:
            resolved_url = feed_url.format(
                city=self.config.get("city_name", ""),
                language=self.config.get("news_language", "en"),
                country=self.config.get("country_code", ""),
            )
            feed = feedparser.parse(resolved_url)
            is_google_news = "news.google.com" in resolved_url
            feed_language = feed.feed.get("language", "").split("-")[0].lower() or "en"
            articles = []
            for entry in feed.entries[:25]:
                rss_description = None

                # entry.content — some feeds provide full article body
                if entry.get("content"):
                    raw = entry.content[0].get("value", "")
                    if raw:
                        rss_description = _clean_html(raw)

                # entry.summary / entry.description — snippet
                if not rss_description:
                    raw = entry.get("summary") or entry.get("description") or ""
                    if raw:
                        cleaned = _clean_html(raw)
                        # Google News summary is just the title repeated — discard it
                        title = entry.get("title", "")
                        if not is_google_news or (cleaned and cleaned not in title):
                            rss_description = cleaned

                # For Google News, capture publisher domain as extra context for LLM
                source_href = entry.get("source", {}).get("href", "")
                publisher = entry.get("source", {}).get("title", "") or feed.feed.get("title", "")

                # Extract image from RSS media fields or summary HTML
                image_url = None
                if entry.get("media_content"):
                    image_url = entry.media_content[0].get("url")
                elif entry.get("media_thumbnail"):
                    image_url = entry.media_thumbnail[0].get("url")
                elif entry.get("enclosures"):
                    for enc in entry.enclosures:
                        if enc.get("type", "").startswith("image"):
                            image_url = enc.get("href") or enc.get("url")
                            break
                if not image_url and rss_description:
                    m = re.search(r'src=["\']([^"\']+\.(?:jpg|jpeg|png|webp|gif))', rss_description, re.I)
                    if m:
                        image_url = m.group(1)

                articles.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "source": publisher,
                    "source_href": source_href,
                    "published_at": entry.get("published"),
                    "rss_description": rss_description,
                    "is_google_news": is_google_news,
                    "feed_language": feed_language,
                    "author": entry.get("author"),
                    "image_url": image_url,
                })

            with_desc = sum(1 for a in articles if a.get("rss_description"))
            self.logger.info(f"  {resolved_url[:60]}… → {len(articles)} articles, {with_desc} with descriptions")
            return articles
        except Exception as e:
            self.logger.warning(f"RSS fetch failed {feed_url}: {e}")
            return []

    # ── Content extraction ────────────────────────────────────────────────────

    def _resolve_url(self, url: str) -> str:
        """Follow redirects (Google News, t.co, etc.) to get the real article URL."""
        if not any(d in url for d in ("news.google.com", "t.co", "bit.ly", "ow.ly")):
            return url
        try:
            resp = requests.head(
                url, allow_redirects=True, timeout=8,
                headers={"User-Agent": USER_AGENT},
            )
            return resp.url
        except Exception:
            return url

    def _extract_content_with_quality(self, article: Dict[str, Any],
                                       skip_scraping: bool = False) -> Tuple[str, str]:
        """
        Return (content, quality) using the best available source:
          full_article    — scraped body ≥ 300 chars
          rss_description — description from the RSS feed
          title_only      — nothing but the headline (LLM will expand using its knowledge)
        """
        # 1. Try to scrape full article (skip if caller requested fast mode or Google News)
        if not skip_scraping and not article.get("is_google_news"):
            real_url = self._resolve_url(article["url"])
            scraped = self._scrape_article(real_url)
            if scraped and len(scraped) >= 300:
                return scraped, QUALITY_FULL

        # 2. RSS description
        rss_desc = (article.get("rss_description") or "").strip()
        if len(rss_desc) >= 60:
            return rss_desc, QUALITY_RSS

        # 3. Title only
        return article.get("title", ""), QUALITY_TITLE

    def _fetch_og_image(self, url: str) -> Optional[str]:
        """Fetch only the og:image meta tag from an article page — fast, lightweight."""
        if not url or "news.google.com" in url:
            return None
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": USER_AGENT}, stream=True)
            # Read only first 8KB to find og:image without downloading the whole page
            chunk = resp.raw.read(8192).decode("utf-8", errors="ignore")
            resp.close()
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', chunk, re.I)
            if not m:
                m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', chunk, re.I)
            return m.group(1) if m else None
        except Exception:
            return None

    def _scrape_article(self, url: str) -> Optional[str]:
        """Fetch and parse article body with browser headers."""
        try:
            resp = requests.get(
                url, timeout=6,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code != 200:
                return None
            art = Article(url)
            art.set_html(resp.text)
            art.parse()
            return art.text if art.text and len(art.text) >= 100 else None
        except Exception as e:
            self.logger.debug(f"Scrape failed {url}: {e}")
            return None

    # ── Article pipeline ──────────────────────────────────────────────────────

    def _process_and_post_article(self, article: Dict[str, Any]) -> bool:
        try:
            if self._article_exists(article["url"]):
                return False

            content, quality = self._extract_content_with_quality(article)

            if not content:
                return False

            # Relevance check
            if not article.get("_bypass_relevance"):
                score = self._calculate_relevance(article["title"], content)
                if score < RELEVANCE_THRESHOLD:
                    self.logger.info(f"Irrelevant ({score:.2f}): {article['title'][:60]}")
                    return False
            else:
                score = 1.0

            summaries = self._summarize_and_translate(
                content, article["title"], article.get("source", ""), quality,
                source_lang=article.get("feed_language", "en"),
            )
            if not summaries:
                return False

            success = self._post_to_telegram(
                title=article["title"],
                summary=summaries["summary_fa"],
                url=article["url"],
                source=article.get("source", "Unknown"),
                quality=quality,
            )
            self._save_article(article, summaries["summary_en"], summaries["summary_fa"],
                               score, quality, posted=success)
            return success
        except Exception as e:
            self.logger.error(f"Error processing '{article.get('title', '')}': {e}")
            return False

    def _article_exists(self, url: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT 1 FROM articles WHERE url = ?", (url,)
            ).fetchone() is not None

    # ── Relevance ─────────────────────────────────────────────────────────────

    def _calculate_relevance(self, title: str, content: str) -> float:
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
                                f'Reply with only valid JSON: {{"relevant": true/false, "score": 0.0-1.0}}'
                            ),
                        },
                        {"role": "user", "content": f"Title: {title}\n\nContent: {content[:500]}"},
                    ],
                    max_tokens=40, temperature=0,
                )
                data = json.loads(response.choices[0].message.content.strip())
                return float(data.get("score", 0.0))
            except Exception as e:
                self.logger.warning(f"LLM relevance failed, using keywords: {e}")

        # Keyword fallback
        city = self.config["city_name"].lower()
        country = self.config["country_code"].lower()
        tl, cl = title.lower(), content.lower()
        score = 0.0
        if city in tl: score += 0.5
        if city in cl: score += 0.3
        if country in tl: score += 0.2
        return min(score, 1.0)

    # ── Summarise + translate (single LLM call) ───────────────────────────────

    def _summarize_and_translate(self, content: str, title: str,
                                  source: str, quality: str,
                                  source_lang: str = "en") -> Optional[Dict[str, str]]:
        """
        One LLM call that produces both an English summary and a Persian translation.
        The prompt adapts to the available content quality.
        Falls back to MyMemory → LibreTranslate when no LLM is configured.
        """
        libretranslate_url = self.config.get("libretranslate_url") or os.getenv("LIBRETRANSLATE_URL")
        lang_note = f" The content is in {source_lang.upper()}." if source_lang != "en" else ""

        if self.openai_client:
            try:
                if quality == QUALITY_FULL:
                    # Full article: always worth an LLM summarization call
                    instruction = (
                        f"Summarise this news article in 3 concise sentences.{lang_note} "
                        "Cover: what happened, who is involved, and why it matters."
                    )
                    content_block = f"Content:\n{content[:3000]}"
                elif quality == QUALITY_RSS:
                    # RSS snippet: translate to English first, then English→Persian
                    summary_en = self._translate_to_english(content, source_lang)
                    # Always translate from English to Persian (avoids needing direct de→fa)
                    en_for_fa = summary_en if summary_en != content else content
                    en_source = "en" if summary_en != content else source_lang
                    summary_fa = self._translate_to_persian(en_for_fa, libretranslate_url, en_source)
                    return {"summary_en": summary_en, "summary_fa": summary_fa}
                else:
                    # Title only: same two-step approach
                    summary_en = self._translate_to_english(content, source_lang)
                    en_for_fa = summary_en if summary_en != content else content
                    en_source = "en" if summary_en != content else source_lang
                    summary_fa = self._translate_to_persian(en_for_fa, libretranslate_url, en_source)
                    return {"summary_en": summary_en, "summary_fa": summary_fa}

                user_msg = f"Publisher: {source}\nTitle: {title}"
                if content_block:
                    user_msg += f"\n\n{content_block}"

                response = self.openai_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                f"You are a news editor. {instruction} "
                                "Return valid JSON with exactly two fields: "
                                "'summary_en' (English summary) and "
                                "'summary_fa' (Persian translation of the summary)."
                            ),
                        },
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=400, temperature=0.3,
                )
                raw = response.choices[0].message.content.strip()
                # Strip markdown code fences
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                # Fix trailing commas (common qwen output issue)
                raw = re.sub(r",\s*([}\]])", r"\1", raw)
                # Extract first JSON object if model added extra text
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if m:
                    raw = m.group(0)
                return json.loads(raw)
            except Exception as e:
                self.logger.error(f"LLM summarise+translate failed: {e}")
                # Fall through to non-LLM path

        # No LLM — use content excerpt and translate separately
        summary_en = content[:300] + ("..." if len(content) > 300 else "")
        summary_fa = self._translate_to_persian(summary_en, libretranslate_url, source_lang)
        return {"summary_en": summary_en, "summary_fa": summary_fa}

    # ── Translation ───────────────────────────────────────────────────────────

    def _translate_to_persian(self, text: str, libretranslate_url: Optional[str] = None,
                               source_lang: str = "en") -> str:
        """Translate to Persian. Priority: MyMemory → LibreTranslate → original."""
        translated = self._translate_mymemory(text, source_lang)
        if translated:
            return translated

        # LibreTranslate: local, offline
        if libretranslate_url:
            try:
                resp = requests.post(
                    f"{libretranslate_url.rstrip('/')}/translate",
                    json={"q": text, "source": source_lang, "target": "fa", "format": "text"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    return resp.json().get("translatedText", text)
                self.logger.warning(f"LibreTranslate {resp.status_code}")
            except Exception as e:
                self.logger.warning(f"LibreTranslate failed: {e}")

        return text

    def _translate_to_english(self, text: str, source_lang: str = "en") -> str:
        """Translate text to English if it's not already English."""
        if source_lang == "en" or not text:
            return text
        libretranslate_url = self.config.get("libretranslate_url") or os.getenv("LIBRETRANSLATE_URL")

        # Try LibreTranslate first (local, no limits)
        if libretranslate_url:
            try:
                resp = requests.post(
                    f"{libretranslate_url.rstrip('/')}/translate",
                    json={"q": text, "source": source_lang, "target": "en", "format": "text"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    return resp.json().get("translatedText", text)
            except Exception as e:
                self.logger.warning(f"LibreTranslate de→en failed: {e}")

        # MyMemory fallback
        translated = self._translate_mymemory(text, source_lang, target="en")
        return translated if translated else text

    def _translate_mymemory(self, text: str, source_lang: str = "en",
                             target: str = "fa") -> Optional[str]:
        """MyMemory free translation API — up to 500 chars, no key needed."""
        if not text or not text.strip():
            return None
        if source_lang == target:
            return text
        try:
            resp = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text[:500], "langpair": f"{source_lang}|{target}"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("responseStatus") == 200:
                    translated = data["responseData"]["translatedText"]
                    if translated and "PLEASE SELECT" not in translated and len(translated) > 5:
                        return translated
        except Exception as e:
            self.logger.debug(f"MyMemory failed: {e}")
        return None

    # ── Telegram ──────────────────────────────────────────────────────────────

    def _post_to_telegram(self, title: str, summary: str, url: str,
                           source: str, quality: str = "") -> bool:
        quality_icon = {QUALITY_FULL: "📰", QUALITY_RSS: "📋", QUALITY_TITLE: "🔖"}.get(quality, "")
        message = (
            f"<b>{title}</b>\n\n"
            f"{summary}\n\n"
            f'{quality_icon} <a href="{url}">متن کامل</a> | {source}'
        )

        async def _send():
            async with self.telegram_bot:
                await self.telegram_bot.send_message(
                    chat_id=self.config["telegram_chat_id"],
                    text=message, parse_mode="HTML",
                    disable_web_page_preview=False,
                )

        try:
            _run_async(_send())
            return True
        except telegram.error.RetryAfter as e:
            self.logger.warning(f"Telegram rate limit, waiting {e.retry_after}s")
            time.sleep(e.retry_after)
            try:
                _run_async(_send())
                return True
            except Exception as err:
                self.logger.error(f"Telegram retry failed: {err}")
                return False
        except Exception as e:
            self.logger.error(f"Telegram posting error: {e}")
            return False

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_article(self, article: Dict[str, Any], summary_en: str, summary_fa: str,
                      relevance_score: float, content_quality: str, posted: bool):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO articles
                (title, url, source, published_at, summary_en, summary_fa,
                 relevance_score, content_quality, posted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article["title"], article["url"], article.get("source"),
                    article.get("published_at"), summary_en, summary_fa,
                    relevance_score, content_quality, posted,
                ),
            )

    # ── Misc ──────────────────────────────────────────────────────────────────

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

    def get_sentiment(self, titles: List[str], finbert_url: str) -> Dict[str, Any]:
        try:
            resp = requests.post(
                f"{finbert_url.rstrip('/')}/sentiment",
                json={"texts": titles}, timeout=15,
            )
            if resp.status_code == 200:
                return {d["text"][:120]: d for d in resp.json().get("details", [])}
        except Exception as e:
            self.logger.warning(f"FinBERT call failed: {e}")
        return {}

    def get_stats(self) -> Dict[str, Any]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
                posted = conn.execute("SELECT COUNT(*) FROM articles WHERE posted = 1").fetchone()[0]
                last = conn.execute(
                    "SELECT created_at FROM articles WHERE posted=1 ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                return {
                    "total_articles": total,
                    "posted_articles": posted,
                    "unposted_articles": total - posted,
                    "last_posted_at": last[0] if last else None,
                }
        except Exception as e:
            self.logger.error(f"Stats error: {e}")
            return {"total_articles": 0, "posted_articles": 0, "unposted_articles": 0, "last_posted_at": None}

"""News processor: fetches articles via the v3 pipeline, writes posts in the channel's
language, and sends them to Telegram.

This is the delivery layer the panel and scheduler drive — one bot = one Telegram
channel with its own token, chat id, language, and feed list.
"""
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import exp
from typing import Any, Dict, List, Optional

import database as db
from news_platform.config import get_settings
from news_platform.pipeline import NewsPipeline
from news_platform.pipeline.ingestion import strip_publisher_suffix
from news_platform.pipeline.llm import PostWriter
from news_platform.pipeline.ranking import age_hours
from news_platform.pipeline.translation import Translator
from news_platform.publishing import TelegramPublisher
from news_platform.storage import SQLiteStore

# Pages that aren't articles: aggregator redirects (their "summary" is a list of
# other outlets' headlines), videos, audio, and live tickers.
NOT_POSTABLE_URL = re.compile(
    r"news\.google\.com|/(videos?|live|av|audio|podcasts?|multimedia)/|/(video|live)-\d", re.I
)
# Below this the RSS description is just a teaser; fetch the page for the post writer.
MIN_TEXT_FOR_SUMMARY = 600


@dataclass
class NewsResult:
    processed: int = 0
    posted: int = 0
    duration: float = 0.0
    status: str = "success"
    error_message: Optional[str] = None
    articles: List[Dict[str, Any]] = field(default_factory=list)


class NewsProcessor:
    """Runs the fetch → write → post flow for a single bot/channel."""

    def __init__(self, bot_config: Dict[str, Any]):
        self.config = bot_config
        self.settings = get_settings()
        self.store = SQLiteStore(self.settings.data_dir)
        self.pipeline = NewsPipeline(self.settings, self.store)
        self.translator = Translator()
        self.writer = PostWriter()
        self.max_age_hours = float(os.getenv("MAX_ARTICLE_AGE_HOURS", "36"))
        self.logger = logging.getLogger(__name__)
        self.publisher = None
        if bot_config.get("bot_token") and bot_config.get("telegram_chat_id"):
            self.publisher = TelegramPublisher(bot_config["bot_token"], str(bot_config["telegram_chat_id"]))

    def process_news(self) -> NewsResult:
        started = time.time()
        max_posts = int(self.config.get("max_posts_per_run", 2))
        try:
            result = self.pipeline.refresh(
                query=self.config.get("city_name", ""),
                language=self.config.get("news_language", "en"),
                country=self.config.get("country_code", "US"),
                limit=60,
                skip_web=bool(self.config.get("skip_scraping", True)),
                translate=False,
                sources=self._sources(),
            )
            bot_id = self._bot_id()
            candidates = self._candidates(result, self._recent_titles(bot_id))
            posted = 0
            attempts = 0
            consecutive_failures = 0
            out_articles: List[Dict[str, Any]] = []

            for article in candidates:
                if self.publisher and consecutive_failures >= 3:
                    # Channel is misconfigured (bad chat id/token) — don't burn
                    # translation quota on articles that can't be delivered.
                    break
                if self.publisher and posted < max_posts:
                    if bot_id is not None and db.has_posted(bot_id, article["url"]):
                        continue
                    if attempts >= max_posts * 3:
                        break
                    attempts += 1
                    data = self._enrich_summaries(article)
                    if data is None:
                        continue  # post writer judged it not a news story
                    send = self.publisher.send_article(
                        title=data["post_title"],
                        summary=data["post_summary"],
                        url=article["url"],
                        source=article["source_name"],
                        language=self.config.get("target_language", "fa"),
                        image_url=article.get("image_url"),
                    )
                    data["posted"] = send.ok
                    data["post_error"] = send.error
                    if bot_id is not None:
                        db.log_post(
                            bot_id=bot_id,
                            title=article["title"],
                            url=article["url"],
                            summary_en=data["summary_en"],
                            summary_fa=data["summary_target"],
                            relevance_score=article.get("relevance_score"),
                            posted=send.ok,
                            error=send.error,
                        )
                    if send.ok:
                        posted += 1
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                else:
                    data = dict(article)
                    data.setdefault("summary_en", (article.get("summary") or {}).get("tldr", ""))
                    data.setdefault("summary_fa", "")
                    data.setdefault("posted", False)
                out_articles.append(data)

            return NewsResult(
                processed=result.processed,
                posted=posted,
                duration=time.time() - started,
                status="success",
                articles=out_articles,
            )
        except Exception as exc:
            self.logger.exception("process_news failed")
            return NewsResult(status="error", error_message=str(exc), duration=time.time() - started)

    def _candidates(self, result, recent_titles: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Recent, postable articles (fresh from this run plus stored ones, which
        covers retries of failed sends), newest and richest first, one per story."""
        now = datetime.now(timezone.utc)
        articles = {a.url: a.to_dict() for a in result.articles}
        since = (now - timedelta(hours=self.max_age_hours + 24)).strftime("%Y-%m-%d %H:%M:%S")
        for stored in self.store.list_articles({"limit": 500, "created_after": since}):
            articles.setdefault(stored["url"], stored)

        scored = []
        for article in articles.values():
            if NOT_POSTABLE_URL.search(article.get("url") or ""):
                continue
            hours = age_hours(article.get("published_at"), now)
            # Undated items can't be proven fresh; skipping them avoids reposting old news.
            if hours is None or hours > self.max_age_hours:
                continue
            article = dict(article)
            article["title"] = strip_publisher_suffix(article["title"], article.get("source_name"))
            score = 0.65 * exp(-hours / 12) + 0.35 * (article.get("quality_score") or 0)
            scored.append((score, article))
        scored.sort(key=lambda item: item[0], reverse=True)
        # Each further article from the same outlet counts for less, so a feed that
        # updates every few minutes doesn't fill the channel on its own.
        per_source: Counter = Counter()
        diversified = []
        for score, article in scored:
            diversified.append((score * 0.8 ** per_source[article.get("source_name")], article))
            per_source[article.get("source_name")] += 1
        diversified.sort(key=lambda item: item[0], reverse=True)
        return drop_same_story([a for _, a in diversified], recent_titles or [])

    def _recent_titles(self, bot_id: Optional[int]) -> List[str]:
        if bot_id is None:
            return []
        return [p["title"] for p in db.get_posts(bot_id=bot_id, limit=200) if p.get("posted")]

    def _enrich_summaries(self, article: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build English + target-language title/summary for one article.

        Uses the Mistral post writer when MISTRAL_API_KEY is set, otherwise (or if
        it fails) machine-translates the RSS teaser. Returns None for non-news.
        """
        target = self.config.get("target_language", "fa")
        source_lang = article.get("language") or "en"
        title = article["title"]
        base_summary = (article.get("summary") or {}).get("tldr") or title

        if self.writer.enabled:
            text = article.get("text") or base_summary
            if len(text) < MIN_TEXT_FOR_SUMMARY:
                page_text = self.pipeline.extractor.fetch_text(article["url"])
                if len(page_text) > len(text):
                    text = page_text
            written = self.writer.write(title, text, source_lang, target)
            if written and written.get("skip"):
                return None
            if written:
                data = dict(article)
                data["summary_en"] = written["summary_en"] or (base_summary if source_lang == "en" else "")
                data["summary_target"] = written["summary"]
                data["summary_fa"] = written["summary"] if target == "fa" else ""
                data["post_title"] = written["title"]
                data["post_summary"] = written["summary"]
                data["posted"] = False
                return data

        summary_en = base_summary if source_lang == "en" else self.translator.translate(base_summary, source_lang, "en")
        if target == "en":
            summary_target = summary_en
            post_title = title if source_lang == "en" else self.translator.translate(title, source_lang, "en")
        elif target == source_lang:
            summary_target = base_summary
            post_title = title
        else:
            # Translate from English when we already have it (pivot done once).
            summary_target = self.translator.translate(summary_en, "en", target)
            post_title = self.translator.translate(title, source_lang, target)

        data = dict(article)
        data["summary_en"] = summary_en
        data["summary_fa"] = summary_target if target == "fa" else ""
        data["summary_target"] = summary_target
        data["post_title"] = post_title or title
        data["post_summary"] = summary_target or summary_en
        data["posted"] = False
        return data

    def _sources(self) -> Optional[List[Dict[str, Any]]]:
        sources = self.config.get("sources")
        return sources if sources else None

    def _bot_id(self) -> Optional[int]:
        try:
            return int(self.config.get("bot_id"))
        except (TypeError, ValueError):
            return None

    def get_stats(self) -> Dict[str, Any]:
        bot_id = self._bot_id()
        posts = db.get_posts(bot_id=bot_id, limit=1) if bot_id is not None else []
        stats = self.store.stats()
        posted_count = len(db.get_posts(bot_id=bot_id, limit=100000)) if bot_id is not None else 0
        return {
            "total_articles": stats["total_articles"],
            "posted_articles": posted_count,
            "unposted_articles": max(stats["total_articles"] - posted_count, 0),
            "last_posted_at": posts[0]["posted_at"] if posts else None,
        }

    def test_connection(self) -> bool:
        if not self.publisher:
            return False
        return self.publisher.test_token()

    def get_sentiment(self, titles: List[str], finbert_url: str) -> Dict[str, Any]:
        return {}


def _title_tokens(title: str) -> set:
    return set(re.findall(r"\w{3,}", (title or "").lower()))


def drop_same_story(articles: List[Dict[str, Any]], recent_titles: List[str], threshold: float = 0.5) -> List[Dict[str, Any]]:
    """Keep the first (best-ranked) article per story; drop ones whose title overlaps
    an earlier pick or a recent post by at least `threshold` (Jaccard of title words)."""
    kept: List[Dict[str, Any]] = []
    seen = [t for t in (_title_tokens(title) for title in recent_titles) if t]
    for article in articles:
        tokens = _title_tokens(article["title"])
        if tokens and any(len(tokens & other) / len(tokens | other) >= threshold for other in seen):
            continue
        kept.append(article)
        if tokens:
            seen.append(tokens)
    return kept


def run_bot(bot: Dict[str, Any]) -> NewsResult:
    """Run the full pipeline for a bot row from the panel database.

    Used by both the panel's manual Run button and the background scheduler.
    """
    sources = []
    for feed in db.get_global_feeds(active_only=True):
        sources.append({"url": feed["url"], "name": feed["name"],
                        "bypass_relevance": bool(feed.get("bypass_relevance"))})
    for url in _parse_feeds(bot.get("custom_feeds") or ""):
        sources.append({"url": url, "name": url, "bypass_relevance": False})

    config = {
        "bot_id": str(bot["id"]),
        "bot_token": bot["bot_token"],
        "telegram_chat_id": bot["telegram_chat_id"],
        "city_name": bot["city_name"],
        "country_code": bot["country_code"],
        "news_language": bot.get("news_language", "en"),
        "target_language": bot.get("target_language", "fa"),
        "max_posts_per_run": bot.get("max_posts_per_run", 2),
        "sources": sources,
    }
    started = time.time()
    result = NewsProcessor(config).process_news()
    db.log_activity(
        city_name=bot["city_name"],
        source=f"bot:{bot['id']}",
        fetched=result.processed,
        returned=result.posted,
        duration_ms=int((time.time() - started) * 1000),
        error=result.error_message,
    )
    return result


def _parse_feeds(raw: str) -> List[str]:
    if not raw:
        return []
    return [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]

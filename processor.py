"""News processor: fetches articles via the v3 pipeline, writes posts in the channel's
language, and sends them to Telegram.

This is the delivery layer the panel and scheduler drive — one bot = one Telegram
channel with its own token, chat id, language, and feed list.
"""
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import exp
from typing import Any, Dict, List, Optional

import database as db
from news_platform.config import get_settings
from news_platform.learning import speech, video
from news_platform.learning.exercises import build_exercises
from news_platform.learning.lesson import (CAPTION_MAX, LessonWriter, caption_length, format_post,
                                           lesson_to_record, practice_button)
from news_platform.learning.practice import practice_link
from news_platform.pipeline import NewsPipeline
from news_platform.pipeline.ingestion import strip_publisher_suffix
from news_platform.pipeline import llm
from news_platform.pipeline.llm import PostWriter, tagged
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
# Same story if this share of the smaller English summary's key words appear in a
# recent post's summary (measured on real posts: repeats >= 0.24, others <= 0.19).
SAME_STORY_OVERLAP = 0.22
SAME_STORY_WINDOW_HOURS = 72
SUMMARY_STOPWORDS = set(
    "that with from this have has after were was are will their they them about into over "
    "more than been which said says also amid during before against while where when would "
    "could other some most many what there these those since being just only".split()
)


TRIES_PER_POST = 3  # articles a bot may try per daily post (each try costs Mistral requests)


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
        self.lessons = LessonWriter()
        self.learning = bot_config.get("mode") == "learning"
        self.max_age_hours = float(os.getenv("MAX_ARTICLE_AGE_HOURS", "36"))
        self.logger = logging.getLogger(__name__)
        self.run_id = bot_config.get("run_id")
        # "Post now" from the panel: one post regardless of the daily limit and spacing.
        self.force = bool(bot_config.get("force"))
        # "Test run": write the post but send nothing and record no post.
        self.dry_run = bool(bot_config.get("dry_run"))
        self._sent_lesson_id: Optional[int] = None
        self.publisher = None
        if bot_config.get("bot_token") and bot_config.get("telegram_chat_id"):
            self.publisher = TelegramPublisher(bot_config["bot_token"], str(bot_config["telegram_chat_id"]))

    def process_news(self) -> NewsResult:
        started = time.time()
        try:
            result = self.pipeline.refresh(
                query=self.config.get("city_name", ""),
                language=self._source_language() or "en",
                country=self.config.get("country_code", "US"),
                limit=60,
                skip_web=bool(self.config.get("skip_scraping", True)),
                translate=False,
                sources=self._sources(),
            )
            bot_id = self._bot_id()
            self._step("fetch", f"{result.fetched} articles in {len(self._sources() or [])} feeds, {result.processed} new",
                       {"in_feeds": result.fetched, "new": result.processed, "duplicates": result.skipped_duplicates})
            if self.learning and self.publisher and not self.lessons.enabled:
                return NewsResult(status="error", error_message="Learning channels need MISTRAL_API_KEY",
                                  duration=time.time() - started)
            if self.force or self.dry_run:
                max_posts, attempt_cap = 1, TRIES_PER_POST
                self._step("plan", "Test run: write one post, send nothing" if self.dry_run
                           else "Post now: one post, ignoring the daily limit and spacing")
            else:
                status = posting_status(self.config, bot_id)
                max_posts = status["may_post"]
                attempt_cap = min(max_posts * TRIES_PER_POST, status["tries_left"])
                self._step("plan", status["reason"], status)
            recent_summaries = self._recent_summaries(bot_id)
            candidates = self._candidates(result, self._recent_titles(bot_id))
            self._step("candidates", f"{len(candidates)} recent articles to choose from",
                       [{"title": a.get("title"), "url": a.get("url"), "source": a.get("source_name"),
                         "relevance": a.get("relevance_score")} for a in candidates[:40]])
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
                    if attempts >= attempt_cap:
                        break
                    attempts += 1
                    self._sent_lesson_id = None
                    self._step("article", article["title"], {"url": article["url"], "source": article.get("source_name")})
                    data = self._lesson(article) if self.learning else self._enrich_summaries(article)
                    skip_reason = "no clean post / not news" if data is None else None
                    if data is not None:
                        repeat = same_story(data["summary_en"], recent_summaries)
                        if repeat:
                            skip_reason = f"same story as {repeat}"
                    if skip_reason:
                        self.logger.info("skipping %s: %s", article["url"], skip_reason)
                        self._step("skip", skip_reason, {"url": article["url"]})
                        if bot_id is not None and not self.dry_run:
                            db.log_post(bot_id=bot_id, title=article["title"], url=article["url"],
                                        summary_en=(data or {}).get("summary_en", ""), summary_fa="",
                                        posted=False, error=db.SKIPPED + skip_reason)
                        continue
                    if self.dry_run:
                        self._step("preview", "Not sent (test run)", {"url": article["url"], "text": self._preview(data)})
                        out_articles.append(data)
                        break
                    if self.learning:
                        send = self._send_lesson(article, data["lesson"])
                    else:
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
                    self._step("sent" if send.ok else "send failed", article["title"] if send.ok else (send.error or ""),
                               {"url": article["url"], "message_id": send.message_id, "lesson_id": self._sent_lesson_id})
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
                            message_id=send.message_id,
                            lesson_id=self._sent_lesson_id,
                        )
                    if send.ok:
                        posted += 1
                        consecutive_failures = 0
                        recent_summaries.append((article["title"], data["summary_en"]))
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

        source_language = self._source_language()
        scored = []
        for article in articles.values():
            if NOT_POSTABLE_URL.search(article.get("url") or ""):
                continue
            # Global feeds are shared by all channels; each takes the ones in its language.
            if source_language and article.get("language") != source_language:
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

    def _post_budget(self, bot_id: Optional[int]) -> int:
        """How many posts this run may send (see posting_status)."""
        return posting_status(self.config, bot_id)["budget"]

    def _tries_left(self, bot_id: Optional[int]) -> int:
        return posting_status(self.config, bot_id)["tries_left"]

    def _preview(self, data: Dict[str, Any]) -> str:
        """The text a test run would have posted."""
        if self.learning:
            return format_post(data["lesson"], self.publisher.chat_username() if self.publisher else None)
        return f"{data.get('post_title', '')}\n\n{data.get('post_summary', '')}"

    def _recent_summaries(self, bot_id: Optional[int]) -> List[tuple]:
        if bot_id is None:
            return []
        since = (datetime.now(timezone.utc) - timedelta(hours=SAME_STORY_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        return [(p["title"], p.get("summary_en") or "") for p in db.get_sent_posts(bot_id, since)]

    def _recent_titles(self, bot_id: Optional[int]) -> List[str]:
        if bot_id is None:
            return []
        return [p["title"] for p in db.get_posts(bot_id=bot_id, limit=200) if p.get("posted")]

    def _enrich_summaries(self, article: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build English + target-language title/summary for one article.

        Uses the Mistral post writer when MISTRAL_API_KEY is set, otherwise
        machine-translates the RSS teaser. Returns None when the article should
        not be posted (writer judged it non-news or couldn't write a clean post).
        """
        target = self.config.get("target_language", "fa")
        source_lang = article.get("language") or "en"
        title = article["title"]
        base_summary = (article.get("summary") or {}).get("tldr") or title

        if self.writer.enabled:
            written = self.writer.write(title, self._article_text(article), source_lang, target)
            # No clean post (or not news): skip it. Machine translation of the
            # teaser is too unreliable for Persian to stand in for the writer.
            if not written or written.get("skip"):
                return None
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

    def _article_text(self, article: Dict[str, Any]) -> str:
        """The article's text; fetches the page when the feed only gave a teaser."""
        text = article.get("text") or (article.get("summary") or {}).get("tldr") or article["title"]
        source = "feed text"
        if len(text) < MIN_TEXT_FOR_SUMMARY:
            page_text = self.pipeline.extractor.fetch_text(article["url"])
            if len(page_text) > len(text):
                text, source = page_text, "article page"
        self._step("text", f"Read {len(text):,} characters from the {source}", {"url": article["url"], "text": text[:3000]})
        return text

    def _lesson(self, article: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """A checked lesson for a learning channel, or None to skip the article."""
        lesson = self.lessons.write(article["title"], self._article_text(article),
                                    language=article.get("language") or "de",
                                    level=self.config.get("level") or "B1",
                                    explain_language=self.config.get("target_language", "fa"))
        if not lesson or lesson.get("skip"):
            return None
        data = dict(article)
        data.update(lesson=lesson, summary_en=lesson["summary_en"], summary_target=lesson["text"],
                    summary_fa="", posted=False)
        return data

    def _send_lesson(self, article: Dict[str, Any], lesson: Dict[str, Any]):
        """One post: the slow audio with the text as its caption and a button that opens the
        practice (words, grammar, exercises) privately in the bot."""
        language = lesson.get("language") or article.get("language") or "de"
        explain = self.config.get("target_language", "fa")
        channel = self.config.get("channel_name") or "Deutsch mit Nachrichten"

        record = lesson_to_record(lesson)
        record.update(explain_language=explain, url=article["url"],
                      exercises=build_exercises(lesson, explain))
        lesson_id = db.save_lesson(self._bot_id(), record)
        self._sent_lesson_id = lesson_id
        username = self.publisher.bot_username()
        markup = None
        if username and record["exercises"]:
            markup = {"inline_keyboard": [[{"text": practice_button(lesson, explain),
                                            "url": practice_link(username, lesson_id)}]]}

        post = format_post(lesson, self.publisher.chat_username())
        audio = speech.synthesize(speech.lesson_script(lesson["title"], lesson["text"], channel, language), language)
        if audio and caption_length(post) <= CAPTION_MAX:
            # Audio, not video: Telegram plays channel videos muted and hides the text when
            # one is opened, while an audio post plays with sound and keeps the text visible.
            cover = self._lesson_cover(article)
            sent = self.publisher.send_audio(audio.data, title=lesson["title"], performer=f"{channel} · {audio.voice}",
                                             caption=post, duration=audio.duration, reply_markup=markup,
                                             thumbnail=cover)
            if not sent.ok and cover:
                self.logger.warning("lesson audio with cover failed (%s); trying without", sent.error)
                cover = None
                sent = self.publisher.send_audio(audio.data, title=lesson["title"],
                                                 performer=f"{channel} · {audio.voice}", caption=post,
                                                 duration=audio.duration, reply_markup=markup)
            if sent.ok:
                self._step("format", "audio with the news photo as cover" if cover else "audio (no photo)")
                return sent
            self.logger.warning("lesson audio failed (%s); posting the text alone", sent.error)
            why = f"Telegram refused the audio: {sent.error}"
        elif not audio:
            why = "speech synthesis failed with every voice tried"
        else:
            why = "the text is too long for an audio caption"
        self._step("format", f"text only: {why}")
        return self.publisher.send_message(post, link_preview=False, reply_markup=markup)

    def _lesson_cover(self, article: Dict[str, Any]) -> Optional[bytes]:
        """The news photo as a small square cover, or None (no photo, no ffmpeg, failure)."""
        if not video.available():
            return None
        image = video.download_image(article.get("image_url"))
        if image is None:
            image = video.download_image(self.pipeline.extractor.fetch_image_url(article["url"]))
        return video.cover(image) if image else None

    def _source_language(self) -> Optional[str]:
        language = (self.config.get("news_language") or "").strip().lower()
        return None if language in ("", "any") else language

    def _sources(self) -> Optional[List[Dict[str, Any]]]:
        sources = self.config.get("sources")
        return sources if sources else None

    def _step(self, step: str, summary: str = "", detail: Any = None) -> None:
        try:
            db.log_step(self.run_id, step, summary, detail)
        except Exception:
            self.logger.exception("could not record run step")

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


def _summary_words(text: str) -> set:
    return {w for w in re.findall(r"[a-zäöüß]{4,}", (text or "").lower()) if w not in SUMMARY_STOPWORDS}


def same_story(summary_en: str, recent: List[tuple], threshold: float = SAME_STORY_OVERLAP) -> Optional[str]:
    """Title of a recent post (title, summary_en) covering the same story, if any.

    Compares English summaries, so it also catches the same story from outlets in
    different languages, where the original titles share no words.
    """
    words = _summary_words(summary_en)
    if len(words) < 5:
        return None
    for title, other_summary in recent:
        other = _summary_words(other_summary)
        if len(other) >= 5 and len(words & other) / min(len(words), len(other)) >= threshold:
            return title
    return None


# One run per bot at a time: a manual Run during a scheduled run would otherwise
# check the daily budget before either has posted and send twice as many.
_run_locks: Dict[int, threading.Lock] = {}
_run_locks_guard = threading.Lock()


def posting_window(config: Dict[str, Any]):
    """(from hour, until hour) in Berlin time; the bot posts only in between."""
    def hour(key, default):
        value = config.get(key)
        return default if value is None or value == "" else min(24, max(0, int(value)))
    return hour("post_from_hour", 8), hour("post_until_hour", 20)


def window_hours(config: Dict[str, Any]) -> float:
    start, end = posting_window(config)
    return 24.0 if start == end else float((end - start) % 24 or 24)


def in_posting_window(config: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    start, end = posting_window(config)
    if start == end:
        return True  # same hour twice: no quiet time
    hour = (now or datetime.now(timezone.utc)).astimezone(speech._berlin()).hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def _utc(text: str) -> datetime:
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _fmt(moment: Optional[datetime]) -> Optional[str]:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else None


def next_in_window(config: Dict[str, Any], moment: datetime) -> datetime:
    """`moment`, or the start of the next posting hours if it falls outside them."""
    probe = moment
    for _ in range(49):
        if in_posting_window(config, probe):
            return probe
        probe = (probe + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return moment


def posting_status(config: Dict[str, Any], bot_id: Optional[int], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Whether the bot may post now and, if not, why and when it may next.

    With a daily limit, posts are spread out over the posting hours: at most one per run,
    and only once ~(posting hours)/limit has passed since the last post (10% slack for the
    run interval). Each daily post allows 3 article tries, so failing articles can't turn
    into an AI request every hour.
    """
    now = now or datetime.now(timezone.utc)
    per_run = int(config.get("max_posts_per_run") or 2)
    per_day = int(config.get("max_posts_per_day") or 0)
    status = {"per_day": per_day, "sent_24h": 0, "tries_left": 10 ** 6, "may_post": per_run, "budget": per_run,
              "next_post_at": None, "in_window": in_posting_window(config, now), "reason": "Can post now"}
    if bot_id is None or per_day <= 0:
        status["reason"] = "Can post now (no daily limit)"
        return status
    since = _fmt(now - timedelta(hours=24))
    sent = db.get_sent_posts(bot_id, since)
    status["sent_24h"] = len(sent)
    status["tries_left"] = max(0, per_day * TRIES_PER_POST - db.count_tries(bot_id, since))
    gap = timedelta(hours=window_hours(config) / per_day * 0.9)
    allowed = now
    if len(sent) >= per_day:
        allowed = _utc(sent[-1]["posted_at"]) + timedelta(hours=24)
        status["reason"] = f"Daily limit reached: {len(sent)} of {per_day} posts in the last 24 h"
    if sent and _utc(sent[0]["posted_at"]) + gap > allowed:
        if len(sent) < per_day:
            status["reason"] = f"Spacing: posts are spread {gap.total_seconds() / 3600:.1f} h apart"
        allowed = _utc(sent[0]["posted_at"]) + gap
    status["budget"] = 0 if allowed > now else 1  # posts allowed, before counting article tries
    if allowed > now:
        status["may_post"] = 0
        status["next_post_at"] = _fmt(next_in_window(config, allowed))
    elif status["tries_left"] <= 0:
        status["may_post"] = 0
        status["reason"] = f"Used today's {per_day * TRIES_PER_POST} article tries; trying again tomorrow"
    else:
        status["may_post"] = 1
    return status


def schedule_status(bot: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """posting_status plus when the scheduler will next check the bot."""
    now = now or datetime.now(timezone.utc)
    status = posting_status(bot, bot["id"], now)
    last = db.last_run_at(bot["id"])
    due = _utc(last) + timedelta(minutes=int(bot.get("run_every_minutes") or 60)) if last else now
    status["next_check_at"] = _fmt(next_in_window(bot, max(due, now))) if bot.get("active") else None
    status["running"] = is_running(bot["id"])
    return status


def is_running(bot_id: int) -> bool:
    lock = _run_locks.get(bot_id)
    return bool(lock and lock.locked())


def is_due(bot: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether the scheduler should run this bot now: inside its posting hours, and its own
    "run every" minutes have passed."""
    now = now or datetime.now(timezone.utc)
    if not in_posting_window(bot, now):
        return False
    last = db.last_run_at(bot["id"])
    if not last:
        return True
    last_time = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    minutes = int(bot.get("run_every_minutes") or 60)
    return now - last_time >= timedelta(minutes=minutes) - timedelta(seconds=30)


def _store_llm_call(call: Dict[str, Any]) -> None:
    db.log_llm_call(call.get("bot_id"), call.get("purpose"), call["provider"], call["model"], call["status"],
                    call.get("prompt_tokens"), call.get("completion_tokens"), call.get("duration_ms"),
                    run_id=call.get("run_id"), response=call.get("response"), error=call.get("error"))


if _store_llm_call not in llm.usage_listeners:
    llm.usage_listeners.append(_store_llm_call)


def _store_trace(run_id: Optional[int], step: str, summary: str, detail: Any) -> None:
    db.log_step(run_id, step, summary, detail)


if _store_trace not in llm.trace_listeners:
    llm.trace_listeners.append(_store_trace)


def record_member_counts(bots: List[Dict[str, Any]]) -> None:
    """Once a (Berlin) day per bot: the channel's subscriber count, and old traces pruned."""
    day = datetime.now(speech._berlin()).strftime("%Y-%m-%d")
    pruned = False
    for bot in bots:
        if not (bot.get("bot_token") and bot.get("telegram_chat_id")) or db.has_member_count(bot["id"], day):
            continue
        count = TelegramPublisher(bot["bot_token"], str(bot["telegram_chat_id"])).member_count()
        if count is not None:
            db.save_member_count(bot["id"], day, count)
        if not pruned:
            db.prune_traces()
            pruned = True


def run_bot(bot: Dict[str, Any], trigger: str = "manual", force: bool = False, dry_run: bool = False,
            run_id: Optional[int] = None) -> NewsResult:
    """Run the full pipeline for a bot row from the panel database.

    Used by both the panel's manual Run button and the background scheduler. The panel
    creates the run row first (`run_id`) so it can show the trace while the run goes on.
    """
    with _run_locks_guard:
        lock = _run_locks.setdefault(bot["id"], threading.Lock())
    if not lock.acquire(blocking=False):
        if run_id is not None:
            db.finish_run(run_id, "error", error="This bot is already running")
        return NewsResult(status="error", error_message="This bot is already running")
    try:
        return _run_bot(bot, trigger, force, dry_run, run_id)
    finally:
        lock.release()


def _run_bot(bot: Dict[str, Any], trigger: str = "manual", force: bool = False, dry_run: bool = False,
             run_id: Optional[int] = None) -> NewsResult:
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
        "max_posts_per_day": bot.get("max_posts_per_day", 2),
        "mode": bot.get("mode") or "news",
        "channel_name": bot.get("name") or "",
        "level": bot.get("level") or "B1",
        "post_from_hour": bot.get("post_from_hour"),
        "post_until_hour": bot.get("post_until_hour"),
        "sources": sources,
        "force": force,
        "dry_run": dry_run,
    }
    started = time.time()
    if run_id is None:
        run_id = db.start_run(bot["id"], trigger)
    config["run_id"] = run_id
    with tagged(bot_id=bot["id"], run_id=run_id):
        result = NewsProcessor(config).process_news()
    db.finish_run(run_id, result.status, result.processed, result.posted, result.error_message)
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

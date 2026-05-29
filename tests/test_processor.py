"""
Tests for NewsProcessor core logic.
All external I/O (Telegram, OpenAI, HTTP, SQLite) is mocked.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Point DATA_DIR to a temp directory for all tests
_TMP_DATA = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _TMP_DATA

from processor import NewsProcessor, NewsResult  # noqa: E402 — must come after env setup


def _make_config(**overrides) -> dict:
    base = {
        "bot_id": "test-bot",
        "bot_token": "fake:token",
        "telegram_chat_id": "-100123456",
        "city_name": "Tehran",
        "country_code": "IR",
        "news_language": "en",
        "max_posts_per_run": 3,
        "openai_api_key": None,
    }
    base.update(overrides)
    return base


def _make_processor(**config_overrides) -> NewsProcessor:
    """Build a NewsProcessor with a mocked Telegram bot."""
    config = _make_config(**config_overrides)
    with patch("processor.telegram.Bot"):
        return NewsProcessor(config)


class TestRelevanceKeywordFallback(unittest.TestCase):
    """Relevance scoring without OpenAI (keyword fallback)."""

    def setUp(self):
        self.proc = _make_processor()

    def test_city_in_title_scores_high(self):
        score = self.proc._calculate_relevance("Tehran flood warning", "content about Tehran")
        self.assertGreaterEqual(score, 0.5)

    def test_irrelevant_article_scores_zero(self):
        score = self.proc._calculate_relevance("Stock market up today", "Wall Street rally")
        self.assertEqual(score, 0.0)

    def test_city_only_in_content_scores_lower_than_title(self):
        title_score = self.proc._calculate_relevance("Tehran news", "random text")
        content_score = self.proc._calculate_relevance("random title", "Tehran mentioned here")
        self.assertGreater(title_score, content_score)

    def test_score_capped_at_one(self):
        score = self.proc._calculate_relevance(
            "Tehran IR news Tehran", "Tehran Tehran Tehran " * 20
        )
        self.assertLessEqual(score, 1.0)


class TestRelevanceLLM(unittest.TestCase):
    """Relevance scoring with a mocked OpenAI client."""

    def setUp(self):
        self.proc = _make_processor(openai_api_key="sk-fake")
        self.proc.openai_client = MagicMock()

    def _set_llm_response(self, score: float, relevant: bool = True):
        payload = json.dumps({"relevant": relevant, "score": score})
        mock_msg = MagicMock()
        mock_msg.content = payload
        self.proc.openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=mock_msg)]
        )

    def test_llm_returns_high_score(self):
        self._set_llm_response(0.9)
        score = self.proc._calculate_relevance("Tehran flood", "content")
        self.assertAlmostEqual(score, 0.9)

    def test_llm_returns_low_score(self):
        self._set_llm_response(0.1, relevant=False)
        score = self.proc._calculate_relevance("Unrelated news", "content")
        self.assertAlmostEqual(score, 0.1)

    def test_llm_failure_falls_back_to_keywords(self):
        self.proc.openai_client.chat.completions.create.side_effect = Exception("API error")
        # Falls back to keyword — city in title => score >= 0.5
        score = self.proc._calculate_relevance("Tehran news", "some content")
        self.assertGreaterEqual(score, 0.5)


class TestSummarizeAndTranslate(unittest.TestCase):
    """_summarize_and_translate: single call, combined output."""

    def setUp(self):
        self.proc = _make_processor(openai_api_key="sk-fake")
        self.proc.openai_client = MagicMock()

    def _set_llm_response(self, en: str, fa: str):
        payload = json.dumps({"summary_en": en, "summary_fa": fa})
        mock_msg = MagicMock()
        mock_msg.content = payload
        self.proc.openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=mock_msg)]
        )

    def test_returns_both_fields(self):
        self._set_llm_response("English summary.", "خلاصه فارسی.")
        result = self.proc._summarize_and_translate("article content here")
        self.assertEqual(result["summary_en"], "English summary.")
        self.assertEqual(result["summary_fa"], "خلاصه فارسی.")

    def test_only_one_api_call_made(self):
        self._set_llm_response("en", "fa")
        self.proc._summarize_and_translate("content")
        self.assertEqual(self.proc.openai_client.chat.completions.create.call_count, 1)

    def test_no_openai_key_returns_excerpt_fallback(self):
        proc = _make_processor()  # no openai key
        proc.openai_client = None
        result = proc._summarize_and_translate("A" * 300)
        self.assertIn("summary_en", result)
        self.assertIn("summary_fa", result)
        self.assertLessEqual(len(result["summary_en"]), 210)  # 200 + "..."

    def test_api_error_returns_none(self):
        self.proc.openai_client.chat.completions.create.side_effect = Exception("fail")
        result = self.proc._summarize_and_translate("content")
        self.assertIsNone(result)


class TestArticleDatabase(unittest.TestCase):
    """SQLite deduplication logic."""

    def setUp(self):
        # Unique bot_id per test so each gets its own fresh SQLite file
        self.proc = _make_processor(bot_id=self._testMethodName)

    def test_new_article_does_not_exist(self):
        self.assertFalse(self.proc._article_exists("https://example.com/new"))

    def test_saved_article_detected_as_existing(self):
        article = {
            "title": "Test title",
            "url": "https://example.com/article1",
            "source": "Test Source",
            "published_at": "2026-01-01",
        }
        self.proc._save_article(article, "en summary", "fa summary", 0.8, posted=True)
        self.assertTrue(self.proc._article_exists(article["url"]))

    def test_stats_reflect_saved_articles(self):
        for i in range(3):
            article = {
                "title": f"Title {i}",
                "url": f"https://example.com/{i}",
                "source": "Src",
                "published_at": "2026-01-01",
            }
            self.proc._save_article(article, "en", "fa", 0.7, posted=(i < 2))

        stats = self.proc.get_stats()
        self.assertEqual(stats["total_articles"], 3)
        self.assertEqual(stats["posted_articles"], 2)
        self.assertEqual(stats["unposted_articles"], 1)


class TestProcessAndPostArticle(unittest.TestCase):
    """Integration-style test for the full per-article pipeline."""

    def _make_proc_with_mocks(self, relevance=0.8, content="Tehran news content " * 20):
        proc = _make_processor(openai_api_key="sk-fake")
        proc.openai_client = MagicMock()

        # Mock relevance LLM
        rel_payload = json.dumps({"relevant": True, "score": relevance})
        rel_msg = MagicMock()
        rel_msg.content = rel_payload

        # Mock summarize LLM
        sum_payload = json.dumps({"summary_en": "English.", "summary_fa": "فارسی."})
        sum_msg = MagicMock()
        sum_msg.content = sum_payload

        proc.openai_client.chat.completions.create.side_effect = [
            MagicMock(choices=[MagicMock(message=rel_msg)]),
            MagicMock(choices=[MagicMock(message=sum_msg)]),
        ]

        with patch.object(proc, "_extract_content", return_value=content), \
             patch.object(proc, "_post_to_telegram", return_value=True):
            return proc

    def test_relevant_article_gets_posted(self):
        proc = self._make_proc_with_mocks(relevance=0.9)
        article = {"title": "Tehran floods", "url": "https://ex.com/1", "source": "BBC",
                   "published_at": "2026-01-01"}
        with patch.object(proc, "_extract_content", return_value="Tehran news content " * 20), \
             patch.object(proc, "_post_to_telegram", return_value=True):
            result = proc._process_and_post_article(article)
        self.assertTrue(result)

    def test_low_relevance_article_skipped(self):
        proc = _make_processor(openai_api_key="sk-fake")
        proc.openai_client = MagicMock()
        rel_payload = json.dumps({"relevant": False, "score": 0.1})
        rel_msg = MagicMock()
        rel_msg.content = rel_payload
        proc.openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=rel_msg)]
        )
        article = {"title": "Global stock market", "url": "https://ex.com/2",
                   "source": "CNN", "published_at": "2026-01-01"}
        with patch.object(proc, "_extract_content", return_value="Wall street content " * 20):
            result = proc._process_and_post_article(article)
        self.assertFalse(result)

    def test_duplicate_article_skipped(self):
        proc = _make_processor()
        article = {"title": "Dup", "url": "https://ex.com/dup",
                   "source": "Src", "published_at": "2026-01-01"}
        proc._save_article(article, "en", "fa", 0.8, posted=True)
        result = proc._process_and_post_article(article)
        self.assertFalse(result)

    def test_short_content_skipped(self):
        proc = _make_processor()
        article = {"title": "Short", "url": "https://ex.com/short",
                   "source": "Src", "published_at": "2026-01-01"}
        with patch.object(proc, "_extract_content", return_value="Too short"):
            result = proc._process_and_post_article(article)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()

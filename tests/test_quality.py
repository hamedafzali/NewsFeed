from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

import database as db
from news_platform.models import PipelineResult, RawArticle
from news_platform.pipeline.ingestion import FeedIngestor, guess_language, strip_publisher_suffix
from news_platform.pipeline.llm import MistralClient, PostWriter, parse_json_object
from news_platform.pipeline.ranking import age_hours
from news_platform.config import Settings
from processor import NewsProcessor, drop_same_story


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_MODEL", raising=False)
    monkeypatch.setenv("MISTRAL_MIN_INTERVAL_MS", "0")
    MistralClient._denied = set()
    MistralClient._cooling_until = {}
    MistralClient._working = None
    db.init_db()


def _rfc(hours_ago: float) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))


def _raw(url, title="t", source="https://feed.test/a"):
    return RawArticle(title=title, url=url, source_name="S", source_url=source)


# --- Ingestion / dates ---

def test_ingestor_interleaves_sources(monkeypatch):
    feeds = {
        "a": [_raw(f"https://a.test/{i}") for i in range(5)],
        "b": [_raw(f"https://b.test/{i}") for i in range(2)],
    }
    ingestor = FeedIngestor(Settings())
    monkeypatch.setattr(ingestor, "fetch_source", lambda source, **kw: feeds[source["url"]])
    urls = [a.url for a in ingestor.fetch([{"url": "a"}, {"url": "b"}])]
    assert urls[:4] == ["https://a.test/0", "https://b.test/0", "https://a.test/1", "https://b.test/1"]
    assert len(urls) == 7


def test_strip_publisher_suffix():
    assert strip_publisher_suffix("Funeral begins in Tehran - NPR", "NPR") == "Funeral begins in Tehran"
    assert strip_publisher_suffix("Plain title", "NPR") == "Plain title"
    assert strip_publisher_suffix("Ends - in dash", "") == "Ends - in dash"


def test_guess_language():
    assert guess_language("Massive Gen Z protests set to sweep cities across Germany. The protests come amid a wave", "de") == "en"
    assert guess_language("Die Proteste junger Menschen werfen die Frage auf, ob die Politik zu wenig tut", "en") == "de"
    assert guess_language("<p>اعتراضات جوانان در سراسر آلمان این سوال را مطرح می کند</p>", "de") == "fa"
    assert guess_language("Tübingen", "de") == "de"  # unsure -> default


def test_age_hours_parses_rfc_and_iso():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    assert age_hours("Sat, 31 Oct 2026 10:00:00 +0000", now) == pytest.approx(2)
    assert age_hours("2026-10-31T08:00:00Z", now) == pytest.approx(4)  # DW RDF format
    assert age_hours("not a date", now) is None
    assert age_hours(None, now) is None


# --- Candidate selection ---

def _article(url, title, hours_ago, quality=0.5):
    return {
        "url": url, "title": title, "source_name": "Src", "language": "en",
        "published_at": _rfc(hours_ago) if hours_ago is not None else None,
        "quality_score": quality, "summary": {"tldr": title}, "text": title,
    }


class _Stored:
    def __init__(self, data):
        self.url = data["url"]
        self._data = data

    def to_dict(self):
        return dict(self._data)


def test_candidates_filter_stale_undated_and_non_articles(monkeypatch):
    processor = NewsProcessor({"bot_id": None})
    monkeypatch.setattr(processor.store, "list_articles", lambda filters: [])
    rows = [
        _article("https://bbc.test/news/fresh", "Fresh story about the city council budget", 2),
        _article("https://bbc.test/news/old", "Four months old story", 24 * 120, quality=1.0),
        _article("https://bbc.test/news/undated", "Undated story", None),
        _article("https://news.google.com/rss/articles/xyz", "Aggregator bundle", 1),
        _article("https://www.bbc.co.uk/news/videos/abc", "A video", 1),
        _article("https://www.bbc.co.uk/news/live/abc", "A live ticker", 1),
        _article("https://www.dw.com/en/gen-z-protests/live-79814694", "DW live ticker", 1),
    ]
    result = PipelineResult(articles=[_Stored(r) for r in rows])
    urls = [a["url"] for a in processor._candidates(result)]
    assert urls == ["https://bbc.test/news/fresh"]


def test_candidates_prefer_newer_over_longer(monkeypatch):
    processor = NewsProcessor({"bot_id": None})
    monkeypatch.setattr(processor.store, "list_articles", lambda filters: [])
    rows = [
        _article("https://x.test/rich-but-older", "Long detailed analysis of budget", 30, quality=1.0),
        _article("https://x.test/new", "Breaking: bridge closed after accident", 1, quality=0.2),
    ]
    result = PipelineResult(articles=[_Stored(r) for r in rows])
    assert processor._candidates(result)[0]["url"] == "https://x.test/new"


def test_candidates_spread_across_sources(monkeypatch):
    processor = NewsProcessor({"bot_id": None})
    monkeypatch.setattr(processor.store, "list_articles", lambda filters: [])
    rows = [dict(_article(f"https://t.test/{i}", f"Tagesschau story number {i} about topic{i}", 0.5 + i * 0.1), source_name="Tagesschau")
            for i in range(4)]
    rows.append(dict(_article("https://dw.test/1", "DW story about something else", 1.5), source_name="DW"))
    result = PipelineResult(articles=[_Stored(r) for r in rows])
    top = [a["source_name"] for a in processor._candidates(result)[:2]]
    assert top == ["Tagesschau", "DW"]


def test_drop_same_story_across_sources_and_recent_posts():
    articles = [
        {"title": "Funeral for Khamenei begins in Tehran"},
        {"title": "Khamenei funeral begins in Tehran amid crowds"},
        {"title": "Stuttgart opens new train line"},
        {"title": "Tübingen council approves bike lanes"},
    ]
    kept = drop_same_story(articles, recent_titles=["Tübingen council approves new bike lanes"])
    assert [a["title"] for a in kept] == ["Funeral for Khamenei begins in Tehran", "Stuttgart opens new train line"]


# --- Mistral client ---

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _ok(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}]})


def test_mistral_disabled_without_key():
    assert not MistralClient().enabled
    assert not PostWriter().enabled


def test_mistral_skips_denied_and_rate_limited_models(monkeypatch):
    calls = []
    responses = {
        "mistral-large-latest": [_Resp(403)],
        "mistral-medium-latest": [_Resp(429), _Resp(429)],
        "mistral-small-latest": [_ok('{"title": "t"}')],
    }

    def fake_post(url, headers, json, timeout):
        calls.append(json["model"])
        return responses[json["model"]].pop(0)

    monkeypatch.setattr("news_platform.pipeline.llm.requests.post", fake_post)
    monkeypatch.setattr("news_platform.pipeline.llm.time.sleep", lambda s: None)
    client = MistralClient(api_key="k", model="")
    assert client.complete_json([{"role": "user", "content": "x"}]) == {"title": "t"}
    assert calls == ["mistral-large-latest", "mistral-medium-latest", "mistral-medium-latest", "mistral-small-latest"]
    # What was learned sticks: next call goes straight to the working model.
    assert client.candidate_models()[0] == "mistral-small-latest"
    assert "mistral-large-latest" not in client.candidate_models()


def test_parse_json_object_tolerates_fences():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": 2}') == {"a": 2}
    assert parse_json_object("nope") is None


def test_post_writer_rejects_wrong_language(monkeypatch):
    writer = PostWriter(MistralClient(api_key="k"))
    monkeypatch.setattr(writer.client, "complete_json",
                        lambda *a, **k: {"title": "Hello", "summary": "English text", "summary_en": "English text"})
    assert writer.write("t", "text", "de", "fa") is None
    monkeypatch.setattr(writer.client, "complete_json",
                        lambda *a, **k: {"title": "سلام", "summary": "متن خبر", "summary_en": "news text"})
    assert writer.write("t", "text", "de", "fa") == {"title": "سلام", "summary": "متن خبر", "summary_en": "news text"}
    monkeypatch.setattr(writer.client, "complete_json", lambda *a, **k: {"skip": True})
    assert writer.write("t", "text", "de", "fa") == {"skip": True}


def test_post_writer_sends_back_mixed_script_output(monkeypatch):
    writer = PostWriter(MistralClient(api_key="k"))
    replies = iter([
        {"title": "چिप‌های مغزی", "summary": "متن خبر", "summary_en": "x"},  # Devanagari slipped in
        {"title": "تراشه‌های مغزی", "summary": "شهر اوleshky", "summary_en": "x"},  # half-translated name
        {"title": "تراشه‌های مغزی", "summary": "از Hals به پایین", "summary_en": "x"},  # untranslated word
        {"title": "تراشه‌های مغزی", "summary": "شهر اولشکی و (BCI)، ناتو", "summary_en": "x"},  # acronym is fine
    ])
    seen = []

    def fake(messages, **kwargs):
        seen.append(messages[-1]["content"])
        return next(replies)

    monkeypatch.setattr(writer.client, "complete_json", fake)
    assert writer.write("t", "text", "de", "fa") is None  # three bad answers
    assert "اوleshky" in seen[2]  # the model is told which word to fix
    assert writer.write("t", "text", "de", "fa")["summary"] == "شهر اولشکی و (BCI)، ناتو"


def test_post_writer_gives_up_after_three_bad_answers(monkeypatch):
    writer = PostWriter(MistralClient(api_key="k"))
    calls = []
    monkeypatch.setattr(writer.client, "complete_json",
                        lambda *a, **k: calls.append(1) or {"title": "x", "summary": "شهر اوleshky"})
    assert writer.write("t", "text", "de", "fa") is None
    assert len(calls) == 3


def test_enrich_uses_writer_and_falls_back_to_translator(monkeypatch):
    processor = NewsProcessor({"bot_id": None, "target_language": "fa"})
    article = _article("https://x.test/a", "Brücke gesperrt", 1)
    article["language"] = "de"
    article["text"] = "x" * 700  # long enough: no page fetch

    # No key: translator path.
    monkeypatch.setattr(processor.translator, "translate", lambda text, s, t: f"[{t}]{text}")
    data = processor._enrich_summaries(article)
    assert data["post_title"] == "[fa]Brücke gesperrt"

    # Key set and writer answers: its output is posted.
    monkeypatch.setattr(processor.writer.client, "api_key", "k")
    monkeypatch.setattr(processor.writer, "write",
                        lambda *a: {"title": "پل بسته شد", "summary": "خلاصه", "summary_en": "Bridge closed"})
    data = processor._enrich_summaries(article)
    assert (data["post_title"], data["post_summary"], data["summary_en"]) == ("پل بسته شد", "خلاصه", "Bridge closed")

    # Writer says it's not news: article is skipped.
    monkeypatch.setattr(processor.writer, "write", lambda *a: {"skip": True})
    assert processor._enrich_summaries(article) is None

    # Writer fails: skip rather than post a machine translation.
    monkeypatch.setattr(processor.writer, "write", lambda *a: None)
    assert processor._enrich_summaries(article) is None


def test_bot_api_ignores_removed_key_fields():
    bot_id = db.add_bot({
        "name": "T", "city_name": "Tübingen", "country_code": "DE",
        "bot_token": "1:a", "telegram_chat_id": "-100", "openai_api_key": "sk-should-not-be-stored",
    })
    db.update_bot(bot_id, {"openai_api_key": "sk-nope", "newsapi_key": "nope"})
    bot = db.get_bot(bot_id)
    assert bot["openai_api_key"] is None and bot["newsapi_key"] is None
    assert bot["max_posts_per_run"] == 2


# --- Posting cadence / repeats ---

def _bot(**overrides):
    config = {"name": "T", "city_name": "Tübingen", "country_code": "DE",
              "bot_token": "1:a", "telegram_chat_id": "-100"}
    config.update(overrides)
    return db.add_bot(config)


def _sent(bot_id, title, hours_ago, summary_en="", posted=True, error=None):
    db.log_post(bot_id, title, f"https://x.test/{title}", summary_en, "", posted=posted, error=error)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db._conn() as c:
        c.execute("UPDATE posts SET posted_at = ? WHERE title = ?", (stamp, title))


def test_new_bots_default_to_two_posts_per_day():
    assert db.get_bot(_bot())["max_posts_per_day"] == 2


def test_daily_budget_spreads_posts():
    bot_id = _bot(max_posts_per_day=2)
    processor = NewsProcessor({"bot_id": bot_id, "max_posts_per_day": 2, "max_posts_per_run": 5})
    assert processor._post_budget(bot_id) == 1          # nothing posted yet: one now
    _sent(bot_id, "a", hours_ago=3)
    assert processor._post_budget(bot_id) == 0          # 3h ago, need ~12h between posts
    _sent(bot_id, "a2", hours_ago=0, posted=False, error="telegram down")
    assert processor._post_budget(bot_id) == 0          # failed sends don't count either way
    with db._conn() as c:
        c.execute("DELETE FROM posts")
    _sent(bot_id, "b", hours_ago=11)
    assert processor._post_budget(bot_id) == 1          # 11h > 0.9 * 12h
    _sent(bot_id, "c", hours_ago=11.5)
    assert processor._post_budget(bot_id) == 0          # two in the last 24h: done for today


def test_no_daily_limit_uses_per_run_cap():
    processor = NewsProcessor({"bot_id": 1, "max_posts_per_day": 0, "max_posts_per_run": 3})
    assert processor._post_budget(1) == 3


def test_same_story_across_languages():
    from processor import same_story
    recent = [
        ("Hundreds evacuated from Russian-occupied Oleshky",
         "Hundreds of residents left the devastated city of Oleshky after a ceasefire brokered by "
         "US First Lady Melania Trump; they crossed into Ukraine via Belarus."),
        ("Binance covers losses",
         "Crypto firm Binance said it would cover 90 percent of customer losses after a hack."),
    ]
    tagesschau = ("Residents of Oleshky arrived in Ukrainian-controlled territory via Belarus "
                  "after the ceasefire; Melania Trump had helped broker the evacuation.")
    assert same_story(tagesschau, recent) == "Hundreds evacuated from Russian-occupied Oleshky"
    other = "The UN Security Council failed to renew the Western Sahara peacekeeping mission after a Russian veto."
    assert same_story(other, recent) is None


def test_skipped_articles_are_not_retried():
    bot_id = _bot()
    _sent(bot_id, "dup", hours_ago=1, posted=False, error=db.SKIPPED + "same story as x")
    _sent(bot_id, "failed", hours_ago=1, posted=False, error="telegram down")
    assert db.has_posted(bot_id, "https://x.test/dup")
    assert not db.has_posted(bot_id, "https://x.test/failed")  # failed sends are retried


def test_run_bot_refuses_overlapping_runs(monkeypatch):
    import processor as proc
    bot = {"id": 42}
    lock = proc._run_locks.setdefault(42, __import__("threading").Lock())
    lock.acquire()
    try:
        assert proc.run_bot(bot).error_message == "This bot is already running"
    finally:
        lock.release()


def test_candidates_keep_only_the_source_language(monkeypatch):
    rows = [dict(_article("https://dw.test/de", "Bundestag beschließt Haushalt", 1), language="de"),
            dict(_article("https://bbc.test/en", "Floods in Spain", 1), language="en")]
    result = PipelineResult(articles=[_Stored(r) for r in rows])
    for language, expected in (("de", ["https://dw.test/de"]), ("any", ["https://dw.test/de", "https://bbc.test/en"])):
        processor = NewsProcessor({"bot_id": None, "news_language": language})
        monkeypatch.setattr(processor.store, "list_articles", lambda filters: [])
        assert sorted(a["url"] for a in processor._candidates(result)) == sorted(expected)


def test_run_every_is_set_per_bot():
    bot_id = _bot()
    assert db.get_bot(bot_id)["run_every_minutes"] == 60          # default
    db.update_bot(bot_id, {"run_every_minutes": 120})
    assert db.get_bot(bot_id)["run_every_minutes"] == 120
    db.update_bot(bot_id, {"run_every_minutes": 1})
    assert db.get_bot(bot_id)["run_every_minutes"] == 5            # floor
    assert db.get_bot(_bot(run_every_minutes="x"))["run_every_minutes"] == 60


def test_bot_is_due_after_its_own_interval():
    from processor import is_due

    fast, slow = _bot(run_every_minutes=30), _bot(run_every_minutes=240)
    assert is_due(db.get_bot(fast)) and is_due(db.get_bot(slow))   # never ran
    for bot_id in (fast, slow):
        db.log_activity("X", f"bot:{bot_id}", 0, 0, 0)
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    with db._conn() as c:
        c.execute("UPDATE activity SET created_at = ?", (stamp,))
    assert is_due(db.get_bot(fast))          # 45 min > 30
    assert not is_due(db.get_bot(slow))      # 45 min < 4 h

"""Running a bot from the panel: post now, test runs, the posting status, and the
decisions the AI writers record in the run trace."""
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import database as db
from news_platform.learning.lesson import LessonWriter
from news_platform.pipeline import llm
from news_platform.pipeline.llm import MistralClient
from news_platform.publishing.telegram import TelegramPublisher
from processor import posting_status
from tests.test_learning import _fake_model, _learning_processor, _lesson_json


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MIN_INTERVAL_MS", "0")
    llm.OpenAICompatClient._cooling_until.clear()
    db.init_db()


def _steps(run_id):
    c = sqlite3.connect(db._db_path())
    try:
        return [(r[0], r[1]) for r in c.execute("SELECT step, summary FROM run_steps WHERE run_id = ? ORDER BY ts",
                                                (run_id,))]
    finally:
        c.close()


def _sent(bot_id, title, hours_ago):
    db.log_post(bot_id, title, f"https://x.test/{title}", "", "", posted=True)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db._conn() as c:
        c.execute("UPDATE posts SET posted_at = ? WHERE title = ?", (stamp, title))


def test_the_trace_shows_the_fact_check_and_the_correction(monkeypatch):
    writer = LessonWriter(MistralClient(api_key="k"))
    wrong = "Im Januar sind bei Protesten viele Menschen gestorben -> im Februar"
    _fake_model(monkeypatch, writer, [_lesson_json(title="T" * 120), _lesson_json(), _lesson_json()],
                [{"errors": [wrong]}, {"errors": []}])
    with llm.tagged(run_id=5):
        assert writer.write("t", "article", "de", "B1", "fa") is not None
    steps = [s for s, _ in _steps(5)]
    assert steps == ["rejected", "validate", "fact check", "correct", "validate", "fact check", "accepted"]
    assert "title" in _steps(5)[0][1] and _steps(5)[2][1] == "1 wrong statement found"


def test_posting_status_says_why_and_when():
    bot_id = db.add_bot({"name": "b", "city_name": "x", "country_code": "DE", "bot_token": "1:a",
                         "telegram_chat_id": "-1", "max_posts_per_day": 2})
    config = {"max_posts_per_day": 2, "post_from_hour": 0, "post_until_hour": 0}
    assert posting_status(config, bot_id)["may_post"] == 1
    _sent(bot_id, "a", hours_ago=3)
    status = posting_status(config, bot_id)
    assert status["may_post"] == 0 and status["reason"].startswith("Spacing")
    expected = datetime.now(timezone.utc) - timedelta(hours=3) + timedelta(hours=24 / 2 * 0.9)
    assert abs(datetime.strptime(status["next_post_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
               - expected) < timedelta(seconds=5)
    _sent(bot_id, "b", hours_ago=14)
    status = posting_status(config, bot_id)
    assert status["reason"].startswith("Daily limit reached: 2 of 2") and status["sent_24h"] == 2


def test_post_now_ignores_the_limit_and_a_test_run_sends_nothing(monkeypatch):
    processor = _learning_processor(monkeypatch)
    bot_id = processor._bot_id()
    _sent(bot_id, "earlier", hours_ago=1)
    assert processor.process_news().posted == 0           # spacing: nothing now

    processor.dry_run = True
    processor.run_id = db.start_run(bot_id, "test")
    assert processor.process_news().posted == 0
    assert processor.publisher.sent == []
    assert [p["title"] for p in db.get_posts(bot_id=bot_id)] == ["earlier"]   # no post, no try counted
    step, summary = _steps(processor.run_id)[-1]
    assert step == "preview" and summary == "Not sent (test run)"

    processor.dry_run, processor.force = False, True
    assert processor.process_news().posted == 1
    assert len(processor.publisher.sent) == 1


def test_run_now_starts_in_the_background(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("SCHEDULE_INTERVAL_MINUTES", "0")
    monkeypatch.setattr(TelegramPublisher, "chat_username", lambda self: None)
    import panel
    from news_platform.app import create_app
    calls = []

    def fake_run(bot, trigger, force=False, dry_run=False, run_id=None):
        calls.append((force, dry_run))
        db.finish_run(run_id, "success", 3, 0)
    monkeypatch.setattr(panel, "run_bot_pipeline", fake_run)

    paused = db.add_bot({"name": "b", "city_name": "x", "country_code": "DE", "bot_token": "1:a",
                         "telegram_chat_id": "-1", "active": 0})
    client = create_app().test_client()
    assert client.post(f"/panel/api/bots/{paused}/run", json={"mode": "normal"}).status_code == 400
    res = client.post(f"/panel/api/bots/{paused}/run", json={"mode": "test"})
    assert res.status_code == 202
    run_id = res.get_json()["run_id"]
    for _ in range(50):
        if db.get_run(run_id)["finished_at"]:
            break
        time.sleep(0.02)
    assert calls == [(False, True)] and db.get_run(run_id)["trigger"] == "test"

    report = client.get(f"/panel/api/bots/{paused}/report").get_json()
    assert report["schedule"]["may_post"] == 1 and report["schedule"]["next_check_at"] is None   # paused


class _FakeTts:
    """Stands in for edge_tts: the listed voices fail, the others return audio."""
    def __init__(self, failing):
        self.failing, self.tried = set(failing), []

    def Communicate(self, text, voice, rate=None):
        tts = self

        class _Stream:
            async def stream(self):
                tts.tried.append(voice)
                if voice in tts.failing:
                    raise RuntimeError("No audio was received.")
                yield {"type": "audio", "data": b"mp3"}
        return _Stream()


def test_speech_tries_another_voice_when_one_fails(monkeypatch):
    import sys
    from news_platform.learning import speech
    voices = [v for v, _ in speech.VOICES["de"]]
    monkeypatch.setattr(speech, "RETRY_PAUSE", 0)
    monkeypatch.setattr(speech.random, "sample", lambda seq, k: list(seq)[:k])
    tts = _FakeTts(failing=voices[:1])
    monkeypatch.setitem(sys.modules, "edge_tts", tts)
    audio = speech.synthesize("Hallo", "de", with_intro=False)
    assert audio and audio.data == b"mp3" and tts.tried == voices[:2]

    tts = _FakeTts(failing=voices)
    monkeypatch.setitem(sys.modules, "edge_tts", tts)
    assert speech.synthesize("Hallo", "de", with_intro=False) is None
    assert len(tts.tried) == speech.VOICE_TRIES


def test_a_lesson_without_audio_says_why_in_the_trace(monkeypatch):
    from news_platform.learning import speech
    processor = _learning_processor(monkeypatch)
    monkeypatch.setattr(speech, "synthesize", lambda text, language: None)
    processor.run_id = db.start_run(processor._bot_id(), "manual")
    assert processor.process_news().posted == 1
    assert [kind for kind, _, _ in processor.publisher.sent] == ["message"]
    assert ("format", "text only: speech synthesis failed with every voice tried") in _steps(processor.run_id)

"""What the platform records for the reports: AI usage, run traces, post links,
subscribers, Üben taps (a count only) and reaction counts — nothing about learners."""
import sqlite3
import time

import pytest

import database as db
from news_platform.learning import video
from news_platform.learning.exercises import build_exercises
from news_platform.learning.lesson import lesson_to_record
from news_platform.learning.practice import PracticeBot
from news_platform.pipeline import llm
from news_platform.publishing.telegram import TelegramPublisher
from processor import record_member_counts
from tests.test_learning import FDP_LESSON, _checked_lesson, _FakePublisher, _learning_processor


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MIN_INTERVAL_MS", "0")
    llm.OpenAICompatClient._cooling_until.clear()
    db.init_db()


def _rows(sql, *args):
    c = sqlite3.connect(db._db_path())
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


class _Response:
    status_code = 200
    headers = {}

    def json(self):
        return {"choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30}}


def test_every_ai_request_is_recorded_with_tokens_and_answer(monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Response())
    client = llm.OpenAICompatClient("groq", api_key="k")
    with llm.tagged(bot_id=3, run_id=9), llm.tagged(purpose="check"):
        assert client.complete_json([{"role": "user", "content": "hi"}]) == {"ok": True}
    [call] = _rows("SELECT * FROM llm_calls")
    assert (call["bot_id"], call["run_id"], call["purpose"], call["provider"], call["status"]) == (3, 9, "check", "groq", 200)
    assert (call["prompt_tokens"], call["completion_tokens"], call["response"]) == (120, 30, '{"ok": true}')

    def down(*a, **k):
        raise llm.requests.ConnectionError("no route")
    monkeypatch.setattr(llm.requests, "post", down)
    with pytest.raises(llm.LlmUnavailable):
        llm.OpenAICompatClient("gemini", api_key="k").complete_json([{"role": "user", "content": "hi"}])
    failed = _rows("SELECT * FROM llm_calls WHERE provider = 'gemini'")[0]
    assert failed["status"] == 0 and "no route" in failed["error"] and failed["bot_id"] is None


def test_a_run_keeps_its_steps_and_the_post_link(monkeypatch):
    processor = _learning_processor(monkeypatch)
    run_id = db.start_run(processor._bot_id(), "manual")
    processor.run_id = run_id
    monkeypatch.setattr(video, "available", lambda: True)
    monkeypatch.setattr(video, "download_image", lambda url: b"jpg")
    monkeypatch.setattr(video, "still_video", lambda image, audio: video.Video(b"mp4", 1280, 720, 41))

    result = processor.process_news()
    db.finish_run(run_id, result.status, result.processed, result.posted)
    assert result.posted == 1
    steps = [r["step"] for r in _rows("SELECT step FROM run_steps WHERE run_id = ? ORDER BY id", run_id)]
    assert steps == ["fetch", "candidates", "article", "format", "sent"]
    [post] = _rows("SELECT message_id, lesson_id FROM posts")
    assert post["message_id"] == 77 and post["lesson_id"] == _rows("SELECT id FROM lessons")[0]["id"]
    assert _rows("SELECT status, posted FROM runs")[0] == {"status": "success", "posted": 1}


def test_subscribers_are_counted_once_a_day(monkeypatch):
    bot_id = db.add_bot({"name": "x", "city_name": "x", "country_code": "DE", "bot_token": "1:a",
                         "telegram_chat_id": "-100"})
    asked = []
    monkeypatch.setattr(TelegramPublisher, "member_count", lambda self: asked.append(1) or 12)
    bots = db.get_bots(include_secrets=True)
    record_member_counts(bots)
    record_member_counts(bots)
    assert len(asked) == 1
    assert _rows("SELECT bot_id, members FROM channel_stats") == [{"bot_id": bot_id, "members": 12}]


def test_practice_counts_taps_but_stores_nothing_about_the_person(monkeypatch):
    bot_id = db.add_bot({"name": "x", "city_name": "x", "country_code": "DE", "bot_token": "1:a",
                         "telegram_chat_id": "-100", "mode": "learning"})
    record = lesson_to_record(_checked_lesson())
    record.update(explain_language="fa", exercises=build_exercises(FDP_LESSON, "fa"))
    lesson_id = db.save_lesson(bot_id, record)
    sent = []
    monkeypatch.setattr("news_platform.learning.practice.TelegramPublisher",
                        lambda token, chat_id: sent.append(_FakePublisher(chat_id)) or sent[-1])
    bot = PracticeBot("1:a")
    message = {"chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Sara"},
               "date": int(time.time()), "text": f"/start l{lesson_id}"}

    bot.handle({"update_id": 1, "message": message})
    bot.handle({"update_id": 2, "message": message})  # double tap: not counted
    bot.handle({"update_id": 3, "message": {**message, "chat": {"id": 43, "type": "private"}}})
    assert _rows("SELECT lesson_id, bot_id, opens FROM lesson_opens") == [
        {"lesson_id": lesson_id, "bot_id": bot_id, "opens": 2}]
    quizzes = [extra for kind, _, extra in sent[0].sent if kind == "quiz"]
    assert quizzes and all(q == {"anonymous": True} for q in quizzes)
    bot.handle({"update_id": 4, "poll_answer": {"poll_id": "p", "user": {"id": 42}, "option_ids": [0]}})  # ignored

    bot.handle({"update_id": 5, "message_reaction_count": {
        "chat": {"id": -100}, "message_id": 77,
        "reactions": [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 3},
                      {"type": {"type": "emoji", "emoji": "❤"}, "total_count": 2}]}})
    assert _rows("SELECT message_id, total FROM post_reactions") == [{"message_id": 77, "total": 5}]

    # No table holds a Telegram user id or name.
    for table in [r["name"] for r in _rows("SELECT name FROM sqlite_master WHERE type = 'table'")]:
        columns = {r["name"] for r in _rows(f"PRAGMA table_info({table})")}
        assert not columns & {"user_id", "first_name", "username", "language_code"}, table


def test_learner_tables_from_before_are_dropped():
    c = sqlite3.connect(db._db_path())
    c.execute("CREATE TABLE learners (bot_id INTEGER, user_id INTEGER, first_name TEXT)")
    c.execute("INSERT INTO learners VALUES (1, 42, 'Sara')")
    c.commit()
    c.close()
    db.init_db()
    assert not _rows("SELECT name FROM sqlite_master WHERE name = 'learners'")

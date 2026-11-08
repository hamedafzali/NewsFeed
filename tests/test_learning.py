from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

import database as db
from news_platform.learning import speech
from news_platform.learning.grammar import GERMAN_TOPICS, find_topic
from news_platform.learning.lesson import LessonWriter, format_lesson, headword_forms
from news_platform.models import PipelineResult
from news_platform.pipeline.llm import MistralClient
from news_platform.publishing.telegram import SendResult, TelegramPublisher
from processor import NewsProcessor


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("MISTRAL_MIN_INTERVAL_MS", "0")
    MistralClient._denied, MistralClient._cooling_until, MistralClient._working = set(), {}, None
    db.init_db()


TEXT = ("Die Stadt Frankfurt hat am Sonntag einen Preis vergeben. Der Preis ist für Menschen, die für Freiheit "
        "kämpfen. Reza Pahlavi hat den Preis bekommen. Er ist der Sohn des letzten Schahs von Iran. Er sagte, "
        "dass viele Iraner seit Jahren gegen Unterdrückung kämpfen. Im Januar sind bei Protesten viele Menschen "
        "gestorben. Der Bürgermeister sagte, Frankfurt unterstützt die Menschen im Iran.")


def _lesson_json(**overrides):
    out = {
        "title": "Preis für Freiheit in Frankfurt",
        "text": TEXT,
        "vocabulary": [
            {"de": "der Preis, -e", "fa": "جایزه", "en": "award"},
            {"de": "die Freiheit (Sg.)", "fa": "آزادی", "en": "freedom"},
            {"de": "kämpfen, hat gekämpft", "fa": "مبارزه کردن", "en": "to fight"},
            {"de": "die Unterdrückung (Sg.)", "fa": "سرکوب", "en": "oppression"},
            {"de": "der Bürgermeister, -", "fa": "شهردار", "en": "mayor"},
            {"de": "unterstützen, hat unterstützt", "fa": "حمایت کردن", "en": "to support"},
            {"de": "sich stellen, hat sich gestellt", "fa": "ایستادگی کردن", "en": "to stand up"},  # not in text
            {"de": "die Gewalt (Sg.)", "fa": "violence", "en": "violence"},                          # not Persian
        ],
        "grammar_topic": "dass",
        "grammar_example": "Er sagte, **dass** viele Iraner kämpfen.",  # paraphrased, not copied
        "quiz": [
            {"q": "Wer hat den Preis bekommen?", "options": ["Der Bürgermeister", "Reza Pahlavi", "Ein Arzt"],
             "answer": 1},
            {"q": "Wo wurde der Preis vergeben?", "options": ["In Berlin", "In Frankfurt", "In Teheran"], "answer": 1},
            {"q": "Was sagt Pahlavi über Präsident Macron?", "options": ["a", "Macron unterstützt Wahlen", "c"],
             "answer": 1},  # answer not in the text
        ],
        "summary_en": "Reza Pahlavi received a freedom award in Frankfurt.",
    }
    out.update(overrides)
    return out


# --- Grammar notes ---

def test_every_grammar_topic_matches_its_own_sample():
    ids = [t.id for t in GERMAN_TOPICS]
    assert len(ids) == len(set(ids))
    for topic in GERMAN_TOPICS:
        assert topic.shown_by(topic.sample), topic.id
        assert topic.explanation_fa and topic.name


def test_grammar_markers_reject_unrelated_sentences():
    assert not find_topic("de", "dass").shown_by("Der Preis ist für Menschen.")
    assert not find_topic("de", "passiv").shown_by("Die Preise werden steigen.")
    assert find_topic("de", "passiv").shown_by("Die Straße wurde gesperrt.")
    dativ = find_topic("de", "dativ_praep")
    assert not dativ.shown_by("Sie hat gute Chancen, Präsidentin zu werden.")
    assert dativ.shown_by("Sie fährt zu ihrer Familie.")


# --- Lesson checks ---

def test_headword_forms():
    assert headword_forms("die Wahl, -en") == ["wahl"]
    assert headword_forms("beschließen, hat beschlossen") == ["beschließen", "beschlossen"]
    assert headword_forms("sich treffen, hat sich getroffen") == ["treffen", "getroffen"]


def test_check_keeps_what_holds_up_and_drops_the_rest():
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(_lesson_json(), "de", "fa")
    assert problem is None
    words = [w["word"] for w in lesson["vocabulary"]]
    assert "sich stellen, hat sich gestellt" not in words and "die Gewalt (Sg.)" not in words
    assert len(words) == 6
    # The paraphrased example is replaced by the real sentence from the text.
    assert lesson["grammar"]["topic"].id == "dass"
    assert lesson["grammar"]["example"].startswith("Er sagte, dass viele Iraner")
    # The question about something not in the text is dropped; answers survive shuffling.
    assert [q["q"] for q in lesson["quiz"]] == ["Wer hat den Preis bekommen?", "Wo wurde der Preis vergeben?"]
    for question, right in zip(lesson["quiz"], ["Reza Pahlavi", "In Frankfurt"]):
        assert question["options"][question["answer"]] == right


def test_check_sends_back_a_grammar_topic_the_text_does_not_show():
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(grammar_topic="obwohl"), "de", "fa")
    assert lesson is None and "grammar topic" in problem


def test_check_strips_markdown_and_sends_back_long_sentences():
    lesson, _ = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(text=TEXT.replace("Reza Pahlavi", "**Reza Pahlavi**"),
                     quiz=[{"q": "Wer hat den **Preis** bekommen?",
                            "options": ["Der Bürgermeister", "**Reza Pahlavi**", "Ein Arzt"], "answer": 1},
                           {"q": "Wo wurde der Preis vergeben?", "options": ["In Berlin", "In Frankfurt", "In Rom"],
                            "answer": 1}]), "de", "fa")
    assert "*" not in lesson["text"] and "Reza Pahlavi hat den Preis" in lesson["text"]
    assert all("*" not in q["q"] and not any("*" in o for o in q["options"]) for q in lesson["quiz"])

    long_sentence = ("In dieser Übung haben viele Behörden, Hilfsorganisationen und Betreiber kritischer "
                     "Infrastruktur wie Energieversorger, Krankenhäuser und Logistiker drei Tage lang zusammen "
                     "geübt, wie sie im Ernstfall reagieren.")
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(text=TEXT + " " + long_sentence), "de", "fa", "B1")
    assert lesson is None and "too long" in problem and "Logistiker" in problem


def _fake_model(monkeypatch, writer, lessons, checks):
    """Answers lesson requests from `lessons` and fact checks from `checks`; returns the feedback sent."""
    feedback = []

    def fake(messages, **kwargs):
        if messages[0]["content"].startswith("You fact-check"):
            return checks.pop(0)
        if len(messages) > 2:
            feedback.append(messages[-1]["content"])
        return lessons.pop(0)

    monkeypatch.setattr(writer.client, "complete_json", fake)
    return feedback


def test_writer_retries_with_feedback_then_succeeds(monkeypatch):
    writer = LessonWriter(MistralClient(api_key="k"))
    feedback = _fake_model(monkeypatch, writer, [_lesson_json(grammar_topic="konjunktiv2"), _lesson_json()],
                           [{"errors": []}])
    lesson = writer.write("t", "article", "de", "B1", "fa")
    assert lesson["level"] == "B1" and lesson["grammar"]["topic"].id == "dass"
    assert "grammar topic" in feedback[0]


def test_writer_sends_back_what_the_fact_check_finds(monkeypatch):
    writer = LessonWriter(MistralClient(api_key="k"))
    wrong = "Im Januar sind bei Protesten viele Menschen gestorben -> im Februar"
    feedback = _fake_model(monkeypatch, writer, [_lesson_json(), _lesson_json()],
                           [{"errors": [wrong]}, {"errors": []}])
    assert writer.write("t", "article", "de", "B1", "fa") is not None
    assert wrong in feedback[0]

    # Never clean: no lesson rather than a wrong one.
    feedback = _fake_model(monkeypatch, writer, [_lesson_json()] * 3, [{"errors": [wrong]}] * 3)
    assert writer.write("t", "article", "de", "B1", "fa") is None


def test_fact_check_ignores_errors_that_do_not_quote_the_lesson(monkeypatch):
    writer = LessonWriter(MistralClient(api_key="k"))
    _fake_model(monkeypatch, writer, [_lesson_json()], [{"errors": [
        "18.12.2025 Einigung auf Digitalpakt 2.0 Fünf Milliarden Euro -> Not in simplified text",
        "-> Der Parteitag war digital.",
    ]}])
    assert writer.write("t", "article", "de", "B1", "fa") is not None


def test_format_lesson_escapes_and_has_all_parts():
    lesson, _ = LessonWriter(MistralClient(api_key="k")).check(_lesson_json(title="A <b> & B"), "de", "fa")
    lesson["level"] = "B1"
    message = format_lesson(lesson, "https://x.test/a?b=1&c=2", "tagesschau", "de", "fa")
    assert "A &lt;b&gt; &amp; B" in message
    assert "der Preis, -e" in message and "جایزه" in message
    assert "Nebensatz mit" in message and find_topic("de", "dass").explanation_fa in message
    assert 'href="https://x.test/a?b=1&amp;c=2"' in message and "#B1" in message
    assert len(message) < 4096


# --- Telegram ---

def test_quiz_and_audio_requests(monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True, "result": {"message_id": 7}}

    monkeypatch.setattr("news_platform.publishing.telegram.requests.post",
                        lambda url, **kw: calls.append((url.rsplit("/", 1)[1], kw)) or Resp())
    publisher = TelegramPublisher("1:a", "-100")
    assert publisher.send_quiz("Wer?", ["A", "B", "C"], 2).ok
    assert publisher.send_audio(b"mp3", title="T", performer="Katja", duration=40).ok
    (method, quiz), (audio_method, audio) = calls
    assert method == "sendPoll" and quiz["json"]["type"] == "quiz" and quiz["json"]["correct_option_id"] == 2
    assert quiz["json"]["options"] == [{"text": "A"}, {"text": "B"}, {"text": "C"}]
    assert audio_method == "sendAudio" and audio["files"]["audio"][1] == b"mp3" and audio["data"]["duration"] == "40"


# --- Processor ---

def test_new_bots_are_news_channels_at_b1():
    bot = db.get_bot(db.add_bot({"name": "T", "city_name": "x", "country_code": "DE",
                                 "bot_token": "1:a", "telegram_chat_id": "-100"}))
    assert (bot["mode"], bot["level"]) == ("news", "B1")


class _Stored:
    def __init__(self, data):
        self.url = data["url"]
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakePublisher:
    def __init__(self):
        self.sent = []

    def send_message(self, text, link_preview=True):
        self.sent.append(("message", text))
        return SendResult(ok=True)

    def send_audio(self, data, **kwargs):
        self.sent.append(("audio", kwargs["performer"]))
        return SendResult(ok=True)

    def send_quiz(self, question, options, correct):
        self.sent.append(("quiz", question))
        return SendResult(ok=True)


def test_learning_channel_posts_text_audio_and_quiz(monkeypatch):
    bot_id = db.add_bot({"name": "L", "city_name": "x", "country_code": "DE", "bot_token": "1:a",
                         "telegram_chat_id": "-100", "news_language": "de", "mode": "learning"})
    processor = NewsProcessor({"bot_id": bot_id, "bot_token": "1:a", "telegram_chat_id": "-100",
                               "news_language": "de", "target_language": "fa", "mode": "learning",
                               "level": "B1", "max_posts_per_day": 2})
    processor.publisher = _FakePublisher()
    article = {"url": "https://tagesschau.test/a", "title": "Pahlavi erhält Preis", "source_name": "tagesschau",
               "language": "de", "published_at": format_datetime(datetime.now(timezone.utc) - timedelta(hours=1)),
               "quality_score": 0.5, "summary": {"tldr": "x"}, "text": "x" * 700}
    monkeypatch.setattr(processor.pipeline, "refresh",
                        lambda **kw: PipelineResult(articles=[_Stored(article)], processed=1))
    monkeypatch.setattr(processor.store, "list_articles", lambda filters: [])
    monkeypatch.setattr(processor.lessons.client, "api_key", "k")
    monkeypatch.setattr(processor.lessons.client, "complete_json", lambda *a, **k: _lesson_json())
    monkeypatch.setattr(speech, "synthesize", lambda text, language: speech.Audio(b"mp3", "Katja", 40))

    result = processor.process_news()

    assert result.status == "success" and result.posted == 1
    kinds = [kind for kind, _ in processor.publisher.sent]
    assert kinds == ["message", "audio", "quiz", "quiz"]
    assert ("audio", "Langsam gelesen · Katja") in processor.publisher.sent
    post = db.get_posts(bot_id=bot_id)[0]
    assert post["posted"] and post["summary_en"].startswith("Reza Pahlavi")


def test_learning_channel_needs_a_mistral_key():
    processor = NewsProcessor({"bot_id": None, "bot_token": "1:a", "telegram_chat_id": "-100", "mode": "learning"})
    monkeypatch_refresh = lambda **kw: PipelineResult(articles=[], processed=0)  # noqa: E731
    processor.pipeline.refresh = monkeypatch_refresh
    result = processor.process_news()
    assert result.status == "error" and "MISTRAL_API_KEY" in result.error_message

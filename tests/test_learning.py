from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import random

import pytest

import database as db
from news_platform.learning import speech
from news_platform.learning.exercises import build_exercises
from news_platform.learning.grammar import GERMAN_TOPICS, find_topic
from news_platform.learning.lesson import (CAPTION_MAX, LessonWriter, caption_length, format_post, format_practice,
                                           headword_forms, lesson_from_record, lesson_to_record)
from news_platform.learning.practice import PracticeBot
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
    perfekt = find_topic("de", "perfekt")
    assert perfekt.shown_by("Viele Menschen sind gestorben.")
    assert perfekt.shown_by("Er ist nach Berlin gefahren, weil er arbeiten muss.")
    assert not perfekt.shown_by("Sie ist bekannt für ihre einfache Art.")         # adjective, not Perfekt
    assert not perfekt.shown_by("Sie hat gute Chancen, Präsidentin zu werden.")


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


def test_check_swaps_a_grammar_topic_the_text_does_not_show():
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(grammar_topic="obwohl"), "de", "fa")
    assert problem is None
    topic, example = lesson["grammar"]["topic"], lesson["grammar"]["example"]
    assert topic.id != "obwohl" and topic.shown_by(example) and example in lesson["text"]


def test_check_strips_markdown_and_sends_back_long_sentences_and_texts():
    lesson, _ = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(title="**Preis** für Freiheit", text=TEXT.replace("Reza Pahlavi", "**Reza Pahlavi**")), "de", "fa")
    assert "*" not in lesson["text"] + lesson["title"] and "Reza Pahlavi hat den Preis" in lesson["text"]

    # Too long for the caption: cut after the last whole sentence that fits.
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(text=" ".join([TEXT] * 3)), "de", "fa")
    assert problem is None and len(lesson["text"]) <= 950 and lesson["text"].endswith(".")
    assert caption_length(format_post(lesson)) <= CAPTION_MAX

    long_sentence = ("In dieser Übung haben viele Behörden, Hilfsorganisationen und Betreiber kritischer "
                     "Infrastruktur wie Energieversorger, Krankenhäuser und Logistiker drei Tage lang zusammen "
                     "geübt, wie sie im Ernstfall reagieren.")
    # One longer sentence (often a quote) is fine; two are not.
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(text=TEXT + " " + long_sentence), "de", "fa", "B1")
    assert problem is None
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(
        _lesson_json(text=long_sentence + " " + TEXT + " " + long_sentence), "de", "fa", "B1")
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
    feedback = _fake_model(monkeypatch, writer, [_lesson_json(title="T" * 120), _lesson_json()],
                           [{"errors": []}])
    lesson = writer.write("t", "article", "de", "B1", "fa")
    assert lesson["level"] == "B1" and lesson["grammar"]["topic"].id == "dass"
    assert "title" in feedback[0]


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


def _checked_lesson(**overrides):
    lesson, problem = LessonWriter(MistralClient(api_key="k")).check(_lesson_json(**overrides), "de", "fa")
    assert problem is None
    lesson.update(level="B1", language="de")
    return lesson


def test_post_is_title_and_text_within_the_caption_limit():
    post = format_post(_checked_lesson(title="A <b> & B"))
    assert post.startswith("🗞 <b>A &lt;b&gt; &amp; B</b>") and TEXT in post and "#LangsameNachrichten" in post
    assert "Quelle" not in post and "http" not in post
    assert caption_length(post) <= CAPTION_MAX


def test_practice_message_has_words_and_grammar():
    message = format_practice(_checked_lesson(), "fa")
    assert "der Preis, -e" in message and "جایزه" in message
    assert "Nebensatz mit" in message and find_topic("de", "dass").explanation_fa in message


def test_lesson_survives_storage():
    lesson = _checked_lesson()
    stored = db.get_lesson(db.save_lesson(None, lesson_to_record(lesson)))
    assert lesson_from_record(stored) == lesson


# --- Exercises ---

FDP_LESSON = {
    "language": "de", "level": "B1", "title": "FDP sucht nach Lösungen",
    "text": ("Die FDP trifft sich heute zu einem digitalen Parteitag. Nach vielen Wahlniederlagen ist die Partei in "
             "einer tiefen Krise. Kubicki hat gesagt, dass er etwas anderes erwartet hat. Deshalb sollen die "
             "Mitglieder mehr Geld in einen Kampagnenfonds einzahlen. Die FDP muss sparen, da sie weniger "
             "staatliche Unterstützung erhält."),
    "vocabulary": [
        {"word": "der Parteitag, -e", "meaning": "کنگره‌ی حزب", "en": "party congress"},
        {"word": "die Krise, -n", "meaning": "بحران", "en": "crisis"},
        {"word": "erwarten, hat erwartet", "meaning": "انتظار داشتن", "en": "to expect"},
        {"word": "einzahlen, hat eingezahlt", "meaning": "واریز کردن", "en": "to pay in"},
        {"word": "die Unterstützung, -en", "meaning": "حمایت", "en": "support"},
        {"word": "sparen, hat gespart", "meaning": "پس‌انداز کردن", "en": "to save"},
    ],
    "grammar": {"topic": find_topic("de", "perfekt"), "example": "Kubicki hat gesagt, dass er etwas anderes erwartet hat."},
}


def test_exercises_are_about_words_and_grammar_and_right_by_construction():
    for seed in range(20):
        exercises = build_exercises(FDP_LESSON, "fa", rng=random.Random(seed))
        kinds = [e["kind"] for e in exercises]
        assert len(exercises) == 6 and {"meaning", "gap", "article", "perfekt"} <= set(kinds)
        for e in exercises:
            right = e["options"][e["answer"]]
            assert len(set(e["options"])) == len(e["options"]) and len(e["q"]) <= 255
            if e["kind"] == "meaning":
                word = next(w for w in FDP_LESSON["vocabulary"] if w["word"].split(",")[0] in e["q"])
                assert right == word["meaning"]
            elif e["kind"] == "gap":
                sentence = e["q"].split("\n\n", 1)[1]
                assert "___" in sentence and sentence.replace("___", right) in FDP_LESSON["text"]
            elif e["kind"] == "article":
                noun = e["q"].rsplit("___ ", 1)[1]
                assert f"{right} {noun}," in " ".join(w["word"] for w in FDP_LESSON["vocabulary"])
            else:
                assert right.startswith("hat ") and right.split()[-1] in " ".join(
                    w["word"] for w in FDP_LESSON["vocabulary"])


def test_gap_options_do_not_give_the_answer_away():
    for seed in range(20):
        for e in build_exercises(FDP_LESSON, "fa", rng=random.Random(seed)):
            if e["kind"] == "gap":
                # nouns are offered with nouns, verbs with verbs
                assert len({o[0].isupper() for o in e["options"]}) == 1


def test_spoken_intro_has_the_date_and_channel():
    from datetime import datetime
    intro = speech.spoken_intro("Deutsch mit Nachrichten", "de", datetime(2026, 11, 8, 9))
    assert intro == "Heute ist Sonntag, der achte November 2026. Sie hören kurze Nachrichten von Deutsch mit Nachrichten."
    assert speech.ordinal_de(31) == "einunddreißigste" and speech.ordinal_de(3) == "dritte"


# --- Telegram ---

class _Resp:
    status_code = 200
    headers = {"content-type": "application/json"}

    def json(self):
        return {"ok": True, "result": {"message_id": 7}}


def test_quiz_and_audio_requests(monkeypatch):
    calls = []
    monkeypatch.setattr("news_platform.publishing.telegram.requests.post",
                        lambda url, **kw: calls.append((url.rsplit("/", 1)[1], kw)) or _Resp())
    publisher = TelegramPublisher("1:a", "-100")
    assert publisher.send_quiz("Wer?", ["A", "B", "C"], 2, explanation="weil").ok
    button = {"inline_keyboard": [[{"text": "Üben", "url": "https://t.me/x?start=l1"}]]}
    assert publisher.send_audio(b"mp3", title="T", performer="Katja", caption="c", duration=40,
                                reply_markup=button).ok
    (method, quiz), (audio_method, audio) = calls
    assert method == "sendPoll" and quiz["json"]["type"] == "quiz" and quiz["json"]["correct_option_id"] == 2
    assert quiz["json"]["options"] == [{"text": "A"}, {"text": "B"}, {"text": "C"}]
    assert quiz["json"]["explanation"] == "weil"
    assert audio_method == "sendAudio" and audio["files"]["audio"][1] == b"mp3" and audio["data"]["duration"] == "40"
    assert publisher.send_video(b"mp4", caption="c", duration=41, width=1280, height=720, reply_markup=button).ok
    video_method, clip = calls[-1]
    assert video_method == "sendVideo" and clip["files"]["video"][1] == b"mp4"
    assert clip["data"]["width"] == "1280" and clip["data"]["supports_streaming"] == "true"
    assert '"url": "https://t.me/x?start=l1"' in audio["data"]["reply_markup"]


# --- Processor and bot ---

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
    def __init__(self, chat_id="-100"):
        self.chat_id = chat_id
        self.sent = []

    def bot_username(self):
        return "PixelWazardsNewsBot"

    def chat_username(self):
        return "LangsameNachrichten"

    def send_message(self, text, link_preview=True, reply_markup=None):
        self.sent.append(("message", text, reply_markup))
        return SendResult(ok=True)

    def send_audio(self, data, **kwargs):
        self.sent.append(("audio", kwargs, kwargs.get("reply_markup")))
        return SendResult(ok=True)

    def send_video(self, data, **kwargs):
        self.sent.append(("video", kwargs, kwargs.get("reply_markup")))
        return SendResult(ok=True)

    def send_quiz(self, question, options, correct, explanation=""):
        self.sent.append(("quiz", question, None))
        return SendResult(ok=True)


def _learning_processor(monkeypatch):
    bot_id = db.add_bot({"name": "Deutsch mit Nachrichten", "city_name": "x", "country_code": "DE",
                         "bot_token": "1:a", "telegram_chat_id": "-100", "news_language": "de", "mode": "learning"})
    processor = NewsProcessor({"bot_id": bot_id, "bot_token": "1:a", "telegram_chat_id": "-100",
                               "news_language": "de", "target_language": "fa", "mode": "learning",
                               "level": "B1", "max_posts_per_day": 2, "channel_name": "Deutsch mit Nachrichten"})
    processor.publisher = _FakePublisher()
    article = {"url": "https://tagesschau.test/a", "title": "Pahlavi erhält Preis", "source_name": "tagesschau",
               "language": "de", "published_at": format_datetime(datetime.now(timezone.utc) - timedelta(hours=1)),
               "quality_score": 0.5, "summary": {"tldr": "x"}, "text": "x" * 700,
               "image_url": "https://images.test/a.jpg"}
    monkeypatch.setattr(processor.pipeline, "refresh",
                        lambda **kw: PipelineResult(articles=[_Stored(article)], processed=1))
    monkeypatch.setattr(processor.store, "list_articles", lambda filters: [])
    answer = (lambda messages, **k: {"errors": []} if messages[0]["content"].startswith("You fact-check")
              else _lesson_json())
    for chain in (processor.lessons.client, processor.lessons.checker):
        monkeypatch.setattr(chain.clients[0], "api_key", "k")
        monkeypatch.setattr(chain, "complete_json", answer)
    monkeypatch.setattr(speech, "synthesize", lambda text, language: speech.Audio(b"mp3", "Katja", 40))
    return processor


def test_learning_post_is_the_news_photo_with_the_audio(monkeypatch):
    from news_platform.learning import video
    processor = _learning_processor(monkeypatch)
    monkeypatch.setattr(video, "available", lambda: True)
    monkeypatch.setattr(video, "download_image", lambda url: b"jpg" if url == "https://images.test/a.jpg" else None)
    made = []
    monkeypatch.setattr(video, "still_video",
                        lambda image, audio: made.append((image, audio)) or video.Video(b"mp4", 1280, 720, 41))

    assert processor.process_news().posted == 1
    [(kind, clip, markup)] = processor.publisher.sent
    assert kind == "video" and made == [(b"jpg", b"mp3")]
    assert clip["caption"].endswith("<i>Niveau B1</i> · 📢 @LangsameNachrichten")   # forwarded posts lead back
    assert clip["caption"].startswith("🗞 <b>Preis für Freiheit") and clip["width"] == 1280 and clip["duration"] == 41
    assert markup["inline_keyboard"][0][0]["url"].startswith("https://t.me/PixelWazardsNewsBot?start=l")


def test_learning_post_falls_back_to_audio_without_a_photo(monkeypatch):
    from news_platform.learning import video
    processor = _learning_processor(monkeypatch)
    monkeypatch.setattr(video, "available", lambda: True)
    monkeypatch.setattr(video, "download_image", lambda url: None)
    monkeypatch.setattr(processor.pipeline.extractor, "fetch_image_url", lambda url: None)

    assert processor.process_news().posted == 1
    assert [kind for kind, _, _ in processor.publisher.sent] == ["audio"]


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
def test_still_video_is_a_playable_mp4(tmp_path):
    import subprocess
    from news_platform.learning import video
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=641x361", "-frames:v", "1",
                    str(tmp_path / "i.jpg")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=3", "-b:a", "48k",
                    str(tmp_path / "a.mp3")], check=True)
    clip = video.still_video((tmp_path / "i.jpg").read_bytes(), (tmp_path / "a.mp3").read_bytes())
    assert clip and clip.data[4:8] == b"ftyp"
    assert (clip.width, clip.height) == (640, 360) and clip.duration == 3   # even size for phones


def test_learning_channel_posts_one_audio_with_text_and_practice_button(monkeypatch):
    from news_platform.learning import video
    monkeypatch.setattr(video, "available", lambda: False)
    bot_id = db.add_bot({"name": "Deutsch mit Nachrichten", "city_name": "x", "country_code": "DE",
                         "bot_token": "1:a", "telegram_chat_id": "-100", "news_language": "de", "mode": "learning"})
    processor = NewsProcessor({"bot_id": bot_id, "bot_token": "1:a", "telegram_chat_id": "-100",
                               "news_language": "de", "target_language": "fa", "mode": "learning",
                               "level": "B1", "max_posts_per_day": 2, "channel_name": "Deutsch mit Nachrichten"})
    processor.publisher = _FakePublisher()
    article = {"url": "https://tagesschau.test/a", "title": "Pahlavi erhält Preis", "source_name": "tagesschau",
               "language": "de", "published_at": format_datetime(datetime.now(timezone.utc) - timedelta(hours=1)),
               "quality_score": 0.5, "summary": {"tldr": "x"}, "text": "x" * 700}
    monkeypatch.setattr(processor.pipeline, "refresh",
                        lambda **kw: PipelineResult(articles=[_Stored(article)], processed=1))
    monkeypatch.setattr(processor.store, "list_articles", lambda filters: [])
    answer = (lambda messages, **k: {"errors": []} if messages[0]["content"].startswith("You fact-check")
              else _lesson_json())
    for chain in (processor.lessons.client, processor.lessons.checker):
        monkeypatch.setattr(chain.clients[0], "api_key", "k")
        monkeypatch.setattr(chain, "complete_json", answer)
    spoken = []
    monkeypatch.setattr(speech, "synthesize",
                        lambda text, language: spoken.append(text) or speech.Audio(b"mp3", "Katja", 40))

    result = processor.process_news()

    assert result.status == "success" and result.posted == 1
    [(kind, audio, markup)] = processor.publisher.sent  # one post, nothing else in the channel
    assert kind == "audio" and audio["caption"].startswith("🗞 <b>Preis für Freiheit")
    assert audio["performer"] == "Deutsch mit Nachrichten · Katja"
    assert spoken[0].startswith("Heute ist ") and "Sie hören kurze Nachrichten von Deutsch mit Nachrichten." in spoken[0]
    url = markup["inline_keyboard"][0][0]["url"]
    assert url.startswith("https://t.me/PixelWazardsNewsBot?start=l")
    stored = db.get_lesson(int(url.rsplit("l", 1)[1]))
    assert stored["exercises"] and stored["explain_language"] == "fa"
    post = db.get_posts(bot_id=bot_id)[0]
    assert post["posted"] and post["summary_en"].startswith("Reza Pahlavi")


def test_practice_bot_sends_the_lesson_privately(monkeypatch):
    import time
    record = lesson_to_record(_checked_lesson())
    record.update(explain_language="fa", exercises=build_exercises(FDP_LESSON, "fa"))
    lesson_id = db.save_lesson(None, record)
    sent = []
    monkeypatch.setattr("news_platform.learning.practice.TelegramPublisher",
                        lambda token, chat_id: sent.append(_FakePublisher(chat_id)) or sent[-1])
    bot = PracticeBot("1:a")
    message = {"chat": {"id": 42, "type": "private"}, "date": int(time.time()), "text": f"/start l{lesson_id}"}

    bot.handle({"update_id": 1, "message": message})
    publisher = sent[-1]
    assert publisher.chat_id == "42"
    kinds = [k for k, _, _ in publisher.sent]
    assert kinds == ["message", "message"] + ["quiz"] * len(record["exercises"])
    assert "جایزه" in publisher.sent[0][1]

    bot.handle({"update_id": 2, "message": message})  # tapped twice: nothing new
    assert sent[-1].sent == []
    bot.handle({"update_id": 3, "message": {**message, "chat": {"id": 7, "type": "group"}}})  # not private
    bot.handle({"update_id": 4, "message": {**message, "text": "/start l99999"}})
    assert "پیدا نشد" in sent[-1].sent[0][1]


def test_learning_channel_needs_a_mistral_key():
    processor = NewsProcessor({"bot_id": None, "bot_token": "1:a", "telegram_chat_id": "-100", "mode": "learning"})
    monkeypatch_refresh = lambda **kw: PipelineResult(articles=[], processed=0)  # noqa: E731
    processor.pipeline.refresh = monkeypatch_refresh
    result = processor.process_news()
    assert result.status == "error" and "MISTRAL_API_KEY" in result.error_message


def test_channel_address_is_looked_up_once_and_named_in_posts(monkeypatch):
    TelegramPublisher._chat_usernames.clear()
    asked, sent = [], []

    class _Chat:
        def json(self):
            return {"ok": True, "result": {"id": -100, "username": "LangsameNachrichten"}}

    monkeypatch.setattr("news_platform.publishing.telegram.requests.get",
                        lambda url, params, timeout: asked.append(params) or _Chat())
    monkeypatch.setattr("news_platform.publishing.telegram.requests.post",
                        lambda url, **kw: sent.append(kw) or _Resp())
    publisher = TelegramPublisher("1:a", "-100")
    assert publisher.chat_username() == "LangsameNachrichten" == publisher.chat_username()
    assert asked == [{"chat_id": "-100"}]                                    # one lookup per channel
    assert TelegramPublisher("1:a", "@Kanal").chat_username() == "Kanal"     # no lookup needed
    assert publisher.send_article("T", "S", "https://x.test/a", "tagesschau").ok
    assert sent[-1]["json"]["text"].endswith("\n📢 @LangsameNachrichten")
    assert format_post(FDP_LESSON).endswith("#LangsameNachrichten")         # private channel: no address

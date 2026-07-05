import pytest

import database as db
from news_platform.pipeline.translation import Translator
from news_platform.publishing.telegram import TelegramPublisher


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db.init_db()
    return tmp_path


def test_telegram_message_format_escapes_html():
    message = TelegramPublisher.format_message(
        title="A <b>bold</b> claim", summary="Q&A session", url="https://x.test/a",
        source="DW", language="fa",
    )
    assert "&lt;b&gt;bold&lt;/b&gt;" in message
    assert "Q&amp;A" in message
    assert "متن کامل" in message
    assert 'href="https://x.test/a"' in message


def test_telegram_read_more_label_per_language():
    for lang, label in [("en", "Read more"), ("de", "Weiterlesen"), ("fa", "متن کامل")]:
        assert label in TelegramPublisher.format_message("t", "s", "https://u", "src", lang)


def test_translator_pivots_through_english(monkeypatch):
    calls = []

    def fake_pair(self, text, source, target):
        calls.append((source, target))
        return f"[{target}]{text}"

    monkeypatch.setattr(Translator, "_translate_pair", fake_pair)
    result = Translator(libretranslate_url="http://lt.test").translate("Hallo Welt", "de", "fa")
    assert calls == [("de", "en"), ("en", "fa")]
    assert result == "[fa][en]Hallo Welt"


def test_translator_noop_when_same_language():
    assert Translator().translate("hello", "en", "en") == "hello"


def test_bot_crud_and_posted_dedupe():
    bot_id = db.add_bot({
        "name": "Test", "city_name": "Tübingen", "country_code": "DE",
        "news_language": "de", "target_language": "fa",
        "bot_token": "123:abc", "telegram_chat_id": "-100999",
    })
    bots = db.get_bots()
    assert len(bots) == 1
    assert bots[0]["bot_token_masked"] == "****:abc"
    assert "bot_token" not in bots[0]
    assert bots[0]["target_language"] == "fa"
    assert db.get_bots(include_secrets=True)[0]["bot_token"] == "123:abc"

    url = "https://example.test/article-1"
    assert not db.has_posted(bot_id, url)
    db.log_post(bot_id, "Title", url, "sum en", "sum fa", posted=True)
    assert db.has_posted(bot_id, url)

    # Failed sends must not block a retry on the next run.
    url2 = "https://example.test/article-2"
    db.log_post(bot_id, "Title2", url2, "sum en", "sum fa", posted=False, error="429")
    assert not db.has_posted(bot_id, url2)


def test_panel_endpoints_registered(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("SCHEDULE_INTERVAL_MINUTES", "0")
    from news_platform.app import create_app

    client = create_app().test_client()
    assert client.get("/panel/").status_code == 200
    assert client.get("/panel/api/stats").status_code == 200
    response = client.post("/panel/api/bots", json={"name": "x"})
    assert response.status_code == 400  # missing required fields is a client error, not a crash

import io

import pytest

import database as db
import promo
from news_platform.publishing.telegram import SendResult

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("API_KEY", raising=False)
    db.init_db()


def _bot(**overrides):
    config = {"name": "Deutsch mit Nachrichten", "city_name": "Deutschland", "country_code": "DE",
              "news_language": "de", "target_language": "fa", "bot_token": "1:a", "telegram_chat_id": "-100",
              "mode": "learning", "level": "B1", "max_posts_per_day": 2}
    config.update(overrides)
    return db.add_bot(config)


class _Chain:
    enabled = True

    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def complete_json(self, messages, temperature=0.3, max_tokens=900):
        self.seen.append(list(messages))
        return self.answers.pop(0)


GOOD = {"text": "یادگیری آلمانی با خبرهای واقعی هر روز 📰\nهر خبر با فایل صوتی و توضیح لغات به فارسی. همین حالا عضو شوید!",
        "image_prompt": "Flat illustration of a student with headphones reading a newspaper in a cafe, warm colours, no text."}


class _Publisher:
    def __init__(self, members=120, pin_ok=True):
        self.sent, self.pinned, self.members, self.pin_ok = [], [], members, pin_ok

    def send_photo(self, data, caption="", filename="", content_type=""):
        self.sent.append(("photo", caption, content_type))
        return SendResult(ok=True, message_id=55)

    def send_message(self, text, link_preview=True, reply_markup=None):
        self.sent.append(("text", text, None))
        return SendResult(ok=True, message_id=56)

    def pin(self, message_id):
        self.pinned.append(message_id)
        return SendResult(ok=self.pin_ok, error=None if self.pin_ok else "not enough rights")

    def member_count(self):
        return self.members

    def channel_link(self):
        return "https://t.me/LangsameNachrichten"


def test_facts_describe_what_the_channel_really_does():
    learning = promo.channel_facts(db.get_bot(_bot()))
    assert "Level B1" in learning and "audio" in learning and "2 posts a day" in learning
    local = promo.channel_facts(db.get_bot(_bot(name="Tübingen News", mode="news", city_name="Tübingen", city_only=True,
                                                custom_feeds="https://www.swr.de/~rss/x.xml")))
    assert "Only local news about Tübingen" in local and "swr.de" in local


def test_writer_sends_back_ads_with_links_or_in_the_wrong_language():
    host = db.get_bot(_bot())
    chain = _Chain([{"text": "Join https://t.me/x now", "image_prompt": "a cafe"},
                    {"text": "Learn German with news every day!", "image_prompt": "a cafe"},
                    GOOD])
    ad = promo.PromoWriter(chain).write(host, host, hints="free, with audio", tone="fun")
    assert ad == GOOD
    assert len(chain.seen) == 3
    assert "link" in chain.seen[1][-1]["content"] and "Persian" in chain.seen[2][-1]["content"]
    assert "free, with audio" in chain.seen[0][1]["content"] and "humour" in chain.seen[0][0]["content"]


def test_an_ad_does_not_start_with_welcome_unless_asked():
    host = db.get_bot(_bot())
    welcome = {"text": "به کانال Deutsch mit Nachrichten خوش آمدید!\n" + GOOD["text"], "image_prompt": GOOD["image_prompt"]}
    chain = _Chain([welcome, GOOD])
    assert promo.PromoWriter(chain).write(host, host, hints="it is an ad") == GOOD
    assert "welcome" in chain.seen[1][-1]["content"]
    assert "not a welcome message" in chain.seen[0][0]["content"]
    asked = _Chain([welcome])
    assert promo.PromoWriter(asked).write(host, host, hints="start with خوش آمدید") == welcome


def test_cross_promotion_speaks_to_the_other_channels_readers():
    host = db.get_bot(_bot(name="Pixel Wizards News", mode="news"))
    target = db.get_bot(_bot())
    chain = _Chain([GOOD])
    promo.PromoWriter(chain).write(host, target)
    system, user = chain.seen[0][0]["content"], chain.seen[0][1]["content"]
    assert "Pixel Wizards News" in system and "Level B1" in user


def test_posting_an_ad_with_photo_and_pin(monkeypatch):
    bot_id = _bot()
    pub = _Publisher()
    monkeypatch.setattr(promo, "publisher_for", lambda bot: pub if bot else None)
    promo_id = db.add_promo(bot_id, bot_id, "", "friendly", "سلام <دنیا>", "prompt", "https://t.me/LangsameNachrichten")
    db.update_promo(promo_id, {"image": PNG})
    assert promo.post_promo(promo_id, pin=True)["ok"]
    kind, caption, content_type = pub.sent[0]
    assert kind == "photo" and content_type == "image/png"
    assert "&lt;دنیا&gt;" in caption and 'href="https://t.me/LangsameNachrichten"' in caption
    assert pub.pinned == [55]
    saved = db.get_promo(promo_id)
    assert saved["message_id"] == 55 and saved["pinned"] == 1 and saved["members_before"] == 120 and saved["posted_at"]


def test_posted_but_not_pinned_says_why(monkeypatch):
    bot_id = _bot()
    monkeypatch.setattr(promo, "publisher_for", lambda bot: _Publisher(pin_ok=False) if bot else None)
    promo_id = db.add_promo(bot_id, bot_id, "", "friendly", "متن", "prompt", None)
    result = promo.post_promo(promo_id, pin=True)
    assert result["ok"] and "could not pin" in result["error"]
    assert db.get_promo(promo_id)["pinned"] == 0


def test_panel_flow_generate_upload_post(monkeypatch):
    from news_platform.app import create_app
    host, target = _bot(name="Pixel Wizards News", mode="news"), _bot()
    monkeypatch.setattr(promo.PromoWriter, "__init__", lambda self, client=None: setattr(self, "client", _Chain([GOOD])))
    monkeypatch.setattr(promo, "target_link", lambda bot: "https://t.me/LangsameNachrichten")
    pub = _Publisher()
    monkeypatch.setattr(promo, "publisher_for", lambda bot: pub if bot else None)
    client = create_app().test_client()

    res = client.post(f"/panel/api/bots/{host}/promos", json={"target_bot_id": target, "hints": "free", "tone": "fun"})
    assert res.status_code == 201
    ad = res.get_json()
    assert ad["text"] == GOOD["text"] and ad["link"] == "https://t.me/LangsameNachrichten" and not ad["has_image"]

    assert client.post(f"/panel/api/promos/{ad['id']}/image",
                       data={"image": (io.BytesIO(b"not an image"), "x.txt")}).status_code == 400
    assert client.post(f"/panel/api/promos/{ad['id']}/image",
                       data={"image": (io.BytesIO(PNG), "ad.png")}).status_code == 200
    assert client.get(f"/panel/api/promos/{ad['id']}/image").data == PNG
    client.put(f"/panel/api/promos/{ad['id']}", json={"text": "متن ویرایش‌شده"})

    assert client.post(f"/panel/api/promos/{ad['id']}/post", json={"pin": False}).get_json()["ok"]
    assert "متن ویرایش‌شده" in pub.sent[0][1]
    assert client.post(f"/panel/api/promos/{ad['id']}/post", json={}).status_code == 409  # only once

    # Listed for both channels: where it was posted and the one it promotes.
    for bot_id in (host, target):
        listed = client.get(f"/panel/api/bots/{bot_id}/promos").get_json()
        assert [p["id"] for p in listed["promos"]] == [ad["id"]]
        assert "image" not in listed["promos"][0]
    assert client.get(f"/panel/api/bots/{target}/promos").get_json()["last"]["hints"] == "free"

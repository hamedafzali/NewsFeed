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


IMAGE = ("Editorial flat vector illustration. A young woman in a mustard coat sits by the window of a Berlin "
         "U-Bahn carriage, eyes closed, smiling, white earbuds in, a small notebook with colourful sticky notes "
         "open on her knee. Through the window the city slides past in soft motion blur: a tram, a bakery, "
         "autumn trees. Square 1:1, eye-level medium shot, subject in the lower right, calm empty sky in the "
         "upper third. Warm morning light, hopeful and calm mood. Palette: deep teal, mustard yellow, "
         "cream and coral. No text, letters, numbers, logos or watermarks anywhere in the image.")
GOOD = {"text": "یادگیری آلمانی با خبرهای واقعی هر روز 📰\nهر خبر با فایل صوتی و توضیح لغات به فارسی. همین حالا عضو شوید!",
        "image_prompt": IMAGE}


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


def test_thin_or_branded_image_prompts_are_sent_back():
    host = db.get_bot(_bot(custom_feeds="https://www.tagesschau.de/x.xml"))
    thin = dict(GOOD, image_prompt="A person with headphones reading German news, warm colours.")
    branded = dict(GOOD, image_prompt=IMAGE.replace("a tram", "a Tagesschau logo on a screen"))
    no_ending = dict(GOOD, image_prompt=IMAGE.replace(" " + promo.NO_TEXT, ""))
    chain = _Chain([thin, branded, no_ending])
    ad = promo.PromoWriter(chain).write(host, host)
    assert "too thin" in chain.seen[1][-1]["content"] and "tagesschau" in chain.seen[2][-1]["content"]
    assert ad["image_prompt"].endswith(promo.NO_TEXT)  # always added when the AI left it out


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


def test_stock_clip_art_prompts_are_sent_back():
    host = db.get_bot(_bot())
    stock = dict(GOOD, image_prompt=(
        "Editorial flat vector illustration, a young adult wearing casual clothes, smiling while holding a "
        "smartphone showing wave-like audio icons, next to a coffee mug with a newspaper headline peeking out; "
        "background includes a small globe and a stack of language books, composition square 1:1, subject "
        "centered lower third, empty calm sky space in upper third, soft natural lighting, warm and inviting "
        "mood, palette of teal, amber, cream, and soft gray. " + promo.NO_TEXT))
    chain = _Chain([stock, GOOD])
    assert promo.PromoWriter(chain).write(host, host) == GOOD
    feedback = chain.seen[1][-1]["content"]
    assert "stock props" in feedback and "globe" in feedback and "smartphone" in feedback


def test_picture_settings_shape_the_rules():
    target = db.get_bot(_bot())
    assert promo.picture_options({"style": "oil", "format": "story", "title": "  Hallo   B1 ", "phone": "x"}) == \
        {"style": "auto", "format": "story", "title": "Hallo B1", "phone": "none"}
    plain = promo.image_rules(promo.picture_options({}), target)
    assert "1:1 square" in plain and promo.NO_TEXT in plain and "phone" in plain  # phones are a cliché here
    rules = promo.image_rules(promo.picture_options(
        {"style": "photo", "format": "story", "title": "Deutsch mit Nachrichten", "phone": "blank"}), target)
    assert "9:16" in rules and "cinematic photograph" in rules and "completely blank" in rules
    assert 'exactly the words "Deutsch mit Nachrichten"' in rules and promo.NO_TEXT not in rules


def test_image_prompt_checks():
    target = db.get_bot(_bot())
    check = promo.PromoWriter._image_problem
    assert check(IMAGE, target) is None
    assert "nationality" in check(IMAGE.replace("A young woman", "A young Persian woman"), target)
    copied = IMAGE.replace("a tram, a bakery", "rain at dawn, a raincoat on the platform")
    assert "copies the example" in check(copied, target)
    assert check(IMAGE.replace("a tram", "a regional train"), target) is None  # "train" is not "rain"
    titled = {"style": "auto", "format": "square", "title": "Deutsch mit Nachrichten", "phone": "none"}
    assert "exact words" in check(IMAGE, target, titled)
    with_title = IMAGE.replace("upper third.", 'upper third with the title "Deutsch mit Nachrichten" in bold letters.')
    assert check(with_title, target, titled) is None
    assert "channel" not in (check(with_title, target, titled) or "")
    blank = dict(titled, title="", phone="blank")
    assert "phone" in check(IMAGE, target, blank)
    phone = IMAGE.replace("open on her knee", "open on her knee, a smartphone in her hand with a plain white blank screen")
    assert check(phone, target, blank) is None
    assert "smartphone" in check(phone, target)  # without the phone setting it is a stock prop


def test_only_one_no_text_line_at_the_end():
    target = db.get_bot(_bot())
    pic = promo.picture_options({})
    body = IMAGE.replace(" " + promo.NO_TEXT, "")
    out = promo.finish_image_prompt(body + " No props, text, or logos.", pic, target)
    assert out == body + " " + promo.NO_TEXT
    mid = "A blank screen with no reflections. Calm light"
    assert promo.finish_image_prompt(mid, pic, target).startswith("A blank screen with no reflections.")
    titled = promo.picture_options({"title": "B1 News"})
    twice = body + ' The only readable text in the image is "B1 News"; no other text.'
    assert promo.finish_image_prompt(twice, titled, target).count("The only readable text") == 1
    assert promo.finish_image_prompt(body, titled, target).endswith('The only readable text in the image is "B1 News"; '
                                                                   "no other text, letters, numbers, logos or watermarks.")


def test_new_image_prompt_keeps_the_text(monkeypatch):
    from news_platform.app import create_app
    bot_id = _bot()
    prompt = IMAGE.replace("open on her knee", "open on her knee, holding a phone with a plain blank screen")
    monkeypatch.setattr(promo.PromoWriter, "__init__",
                        lambda self, client=None: setattr(self, "client", _Chain([{"image_prompt": prompt}])))
    promo_id = db.add_promo(bot_id, bot_id, "free", "fun", "متن آگهی", "old prompt", None)
    client = create_app().test_client()
    res = client.post(f"/panel/api/promos/{promo_id}/image-prompt", json={"picture": {"phone": "blank", "format": "story"}})
    assert res.status_code == 200
    ad = res.get_json()
    assert ad["text"] == "متن آگهی" and "blank screen" in ad["image_prompt"]
    assert ad["picture"]["phone"] == "blank" and ad["picture"]["format"] == "story"
    assert client.get(f"/panel/api/bots/{bot_id}/promos").get_json()["last"]["picture"]["phone"] == "blank"

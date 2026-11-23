"""Ads for our channels, written by the AI from hints and the channel's real settings.

An ad is posted in one channel (the "host") and promotes the same channel or another of
ours (the "target"), e.g. the Persian news channel telling its readers about the German
learning channel. The AI also writes an English prompt for an image generator; the
picture itself is made by hand in another tool and uploaded in the panel.
"""
import html
import json
import logging
import random
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import database as db
from news_platform.pipeline.llm import LANGUAGE_NAMES, PERSIAN_CHARS, LlmUnavailable, make_client, tagged
from news_platform.publishing.telegram import TelegramPublisher

TONES = {
    "friendly": "warm and friendly, like a recommendation from a friend",
    "professional": "clear and professional, trustworthy",
    "fun": "light and playful, with a little humour",
}
TEXT_MAX = 700        # the caption limit is 1024; the link and channel name are added after
JOIN = {"fa": "عضو شوید", "de": "Jetzt beitreten", "en": "Join"}
# Picture settings the owner picks in the panel; they shape the image prompt.
PICTURE_DEFAULT = {"style": "auto", "format": "square", "title": "", "phone": "none"}
STYLES = {
    "auto": "Choose the style and medium that fits the tone (e.g. risograph print, gouache illustration, "
            "cinematic 35mm photo, paper-cut collage, soft clay 3D) and name it first",
    "photo": "A cinematic photograph: name it first, with lens, depth of field and film look",
    "illustration": "A hand-made looking illustration (e.g. gouache, risograph print, ink and watercolour, "
                    "editorial vector): name the exact medium first",
    "3d": "A soft 3D render (clay or toy-like materials, gentle studio light): name it first",
    "collage": "A paper-cut or mixed-media collage with visible paper textures: name it first",
}
FORMATS = {
    "square": ("1:1 square", "an empty calm area in the upper third"),
    "portrait": ("4:5 portrait", "an empty calm area in the upper quarter"),
    "story": ("9:16 vertical (for stories)",
              "the top fifth and bottom fifth calm and empty, because app buttons cover them"),
}
PHONES = ("none", "ai", "blank")
# Varied starting points, so not every picture is the same rainy window.
SETTINGS = ["a bakery queue in the morning", "a regional train crossing a river bridge", "a laundromat at night",
            "a park bench under autumn trees", "a small kitchen while cooking dinner", "a bike ride along a canal",
            "a weekly market stall", "a library staircase", "a balcony full of plants", "a tram stop in snow",
            "a night bus with fogged windows", "a lake shore in summer", "a student dorm room at midnight",
            "a hiking trail with a view over a valley", "a busy station hall at rush hour", "a rooftop at sunset",
            "a cosy café corner on a rainy afternoon", "a street festival with string lights"]
EXAMPLES = (
    "  * Risograph print in two inks, deep teal and tomato red on warm off-white paper with visible grain. "
    "A woman in her thirties in a yellow raincoat waits on a rainy city platform at dawn, eyes closed, one "
    "earbud in, lips slightly moving as she repeats a word. The rain around her head turns into tiny paper "
    "birds flying upward. Camera low on the platform, woman in the lower left, the birds leading the eye up "
    "into a calm, empty pale sky.\n"
    "  * Cinematic photograph, 35mm, shallow depth of field, golden hour. From a wooden punt on a calm river, "
    "an old man in a flat cap and a teenage girl lean over the side laughing at a folded city map; behind "
    "them pastel half-timbered houses mirror in the water and willow branches hang in from above. Camera at "
    "water level in the boat, subjects in the lower half, wide calm sky above. Palette: honey gold, sage "
    "green, faded rose, river blue."
)
# Words from the examples: three or more means the AI copied instead of inventing.
EXAMPLE_MARKERS = ("rain", "dawn", "earbud", "raincoat", "platform", "paper birds", "punt", "willow",
                   "flat cap", "half-timbered", "window")
# Stock props that make every ad picture look the same.
CLICHES = re.compile(r"\b(globe|flags?|stacks? of books|language books|light ?bulb|speech bubbles?|"
                     r"smartphone|laptop|tablet|headlines?|coffee mug|mug|audio icons?|icons?)\b", re.I)
PHONE_WORDS = {"smartphone", "icon", "icons"}
# A picture can't show nationality, and naming it invites stereotypes.
ORIGIN = re.compile(r"\b(persian|iranian|afghan|tajik|middle[- ]eastern|arab(?:ic)?|ethnic|"
                    r"german (?:man|woman|girl|boy|person|student)s?)\b", re.I)
# A closing "no text ..." sentence written by the AI; replaced by ours.
NO_TEXT_SENTENCE = re.compile(r"(?:^|(?<=[.!]))\s*(?:(?:no|without)\b[^.]*\b(?:text|letters|logos?|watermarks?)\b"
                              r"|the only (?:readable )?text\b)[^.]*(?:\.|$)", re.I)
NO_TEXT = "No text, letters, numbers, logos or watermarks anywhere in the image."


def picture_options(raw: Any) -> Dict[str, str]:
    """Clean picture settings from the panel (unknown values fall back to the defaults)."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "style": raw.get("style") if raw.get("style") in STYLES else "auto",
        "format": raw.get("format") if raw.get("format") in FORMATS else "square",
        "title": re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()[:40],
        "phone": raw.get("phone") if raw.get("phone") in PHONES else "none",
    }


def _shown_name(picture: Dict[str, str], target: Dict[str, Any]) -> str:
    """The only words allowed in the picture: the title, or the channel name on a drawn phone screen."""
    if picture["title"]:
        return picture["title"]
    return (target.get("name") or "") if picture["phone"] == "ai" else ""


def image_ending(picture: Dict[str, str], target: Dict[str, Any]) -> str:
    name = _shown_name(picture, target)
    if name:
        return (f'The only readable text in the image is "{name}"; no other text, letters, numbers, logos '
                "or watermarks.")
    return NO_TEXT


def image_rules(picture: Dict[str, str], target: Dict[str, Any], seed: Optional[random.Random] = None) -> str:
    fmt, space = FORMATS[picture["format"]]
    settings = (seed or random).sample(SETTINGS, 3)
    if target.get("city_only") and target.get("city_name"):
        settings.insert(0, f"a recognisable everyday spot in {target['city_name']}")
    parts = [
        "a ready-to-paste prompt in English for an AI image generator (ChatGPT, Gemini, Midjourney), one "
        f"paragraph of 90-150 words, for a {fmt} picture people stop scrolling for.",
        "Idea: ONE fresh visual idea that fits only this channel: a real moment with a feeling, or a simple "
        "visual metaphor. Include one visible detail that shows the channel's idea without readable words "
        "(e.g. lips moving as someone repeats a word aloud, a hand-written vocabulary card, a local landmark). "
        f"Settings to consider for variety, or a better one: {'; '.join(settings)}.",
        "Describe it like a film director: who (age, clothes, expression, gesture; not nationality, ethnicity "
        "or language, which a picture can't show), where (a specific place, weather, time of day), what is "
        "happening right now.",
        f"Style: {STYLES[picture['style']]}.",
        "Camera: where the camera is (inside or outside, height, distance, angle), where the subject sits in "
        f"the frame, and {space}. Then the light and a palette of 3-4 named colours.",
    ]
    name = target.get("name") or ""
    if picture["phone"] == "ai":
        parts.append(
            "Include a hand holding a modern phone, screen facing the viewer, sharp and large in the frame. The "
            "screen shows a channel post in a generic messenger app: at the top the channel name "
            f'"{picture["title"] or name}", below it a news photo, an audio player bar with a waveform, and a '
            "few lines of small blurred text. No app logos or app names.")
    elif picture["phone"] == "blank":
        parts.append(
            "Include a hand holding a modern phone, screen facing the viewer almost straight on, large in the "
            "frame. The screen is completely blank: plain bright white, evenly lit, no reflections, so a real "
            "screenshot can be placed on it later.")
    if picture["title"]:
        parts.append(
            f'Put a title in the empty area: exactly the words "{picture["title"]}", spelled exactly like that, '
            "in bold, clean, rounded sans-serif letters, large and easy to read.")
    avoid = ("Avoid clichés: no globes, flags, stacks of books, light bulbs, speech bubbles, newspapers with "
             "headlines, coffee mugs, laptops, generic 'smiling young adult'")
    if picture["phone"] == "none":
        avoid += ", no phones or tablets showing icons"
    parts.append(avoid + ". Never name brands, news outlets, apps or logos"
                 + (", and no readable writing except what is asked above." if _shown_name(picture, target)
                    else ", the channel, and nothing with readable writing."))
    parts.append(f"End with exactly: '{image_ending(picture, target)}'")
    parts.append("The quality to aim for. Do NOT reuse their settings, weather, people or poses:\n" + EXAMPLES)
    return "\n  ".join(parts)


def finish_image_prompt(prompt: str, picture: Dict[str, str], target: Dict[str, Any]) -> str:
    """Replace whatever "no text" sentence the AI wrote with exactly one of ours."""
    body = NO_TEXT_SENTENCE.sub("", prompt).strip()
    if body and body[-1] not in ".!":
        body += "."
    return f"{body} {image_ending(picture, target)}"


# A welcome greeting (fa / de / en); only allowed when the owner asks for one in the hints.
WELCOME = re.compile(r"خوش\s*آمد|willkommen|welcome|سلام|hallo")

logger = logging.getLogger(__name__)


def _hours(bot: Dict[str, Any]) -> str:
    start, end = bot.get("post_from_hour"), bot.get("post_until_hour")
    if start is None or end is None or start == end:
        return "at any time of day"
    return f"between {start:02d}:00 and {end:02d}:00 (German time)"


def _feed_names(bot: Dict[str, Any]) -> List[str]:
    names = []
    for line in (bot.get("custom_feeds") or "").replace(",", "\n").splitlines():
        host = urlparse(line.strip()).netloc.replace("www.", "")
        if host and host not in names:
            names.append(host)
    if not bot.get("city_only"):
        for feed in db.get_global_feeds(active_only=True):
            name = re.sub(r"\s*\([^)]*\)\s*$", "", feed["name"]).strip()
            if name and name not in names:
                names.append(name)
    return names[:6]


def channel_facts(bot: Dict[str, Any]) -> str:
    """What the channel really does, so the ad promises nothing it doesn't."""
    reader = LANGUAGE_NAMES.get(bot.get("target_language") or "fa", "Persian")
    source = LANGUAGE_NAMES.get(bot.get("news_language") or "de", "German")
    per_day = bot.get("max_posts_per_day") or 0
    rhythm = f"{per_day} posts a day" if per_day else "several posts a day"
    if (bot.get("mode") or "news") == "learning":
        facts = [
            f"A free Telegram channel for {reader} speakers learning {source} with real, current news.",
            f"Level {bot.get('level') or 'B1'}. {rhythm}, {_hours(bot)}.",
            f"Each post: a short {source} news text written at that level, read aloud as audio "
            f"(listen and read along), the key words and one grammar point explained in {reader}.",
            "Under each post a button opens free practice exercises (quizzes) privately in the bot.",
        ]
    else:
        facts = [f"A free Telegram news channel in {reader}.",
                 f"{rhythm} {_hours(bot)}: short, neutral summaries of real news, each with a link to the source."]
        if bot.get("city_only"):
            facts.append(f"Only local news about {bot.get('city_name')} ({bot.get('country_code')}).")
        elif bot.get("country_code"):
            facts.append(f"News from {source}-language media about {bot.get('country_code')} and the world.")
    sources = _feed_names(bot)
    if sources:
        facts.append("Sources: " + ", ".join(sources) + ".")
    facts.append(f"Channel name: {bot.get('name')}.")
    return "\n".join(facts)


class PromoWriter:
    """Writes the ad text (in the host channel's language) and an image prompt (English)."""

    def __init__(self, client=None):
        self.client = client or make_client()

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    def write(self, host: Dict[str, Any], target: Dict[str, Any], hints: str = "", tone: str = "friendly",
              picture: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        """{"text", "image_prompt"}, or None if no AI provider gave a usable answer."""
        picture = picture_options(picture)
        language = host.get("target_language") or "fa"
        language_name = LANGUAGE_NAMES.get(language, language)
        same = host["id"] == target["id"]
        audience = ("people who don't know the channel yet: it introduces the channel, is pinned in it "
                    "and forwarded to friends and groups"
                    if same else f"readers of our other channel \"{host.get('name')}\", who may not know this one")
        system = (
            f"You write short Telegram ads in {language_name}.\n"
            f"Write an ad that introduces the channel described below, aimed at {audience}.\n"
            "Rules:\n"
            f"- text: in {language_name}, at most 90 words, 3-6 short lines. A catchy first line, the "
            "concrete benefits, and a closing call to join. 1-3 fitting emojis. No hashtags.\n"
            "- It is an ad, not a welcome message: never start with a greeting or \"welcome\". "
            "Open with what the reader gains (e.g. a question or a promise).\n"
            "- The owner's wishes (if given) come first: follow them over these style rules.\n"
            "- Only promise what the facts say. No invented numbers, prices, prizes or reviews.\n"
            "- Do not put any link or @name in the text; the join link is added below it.\n"
            "- Channel names and German words may stay in Latin letters.\n"
            f"- Tone: {TONES.get(tone, TONES['friendly'])}.\n"
            f"- image_prompt: {image_rules(picture, target)}\n"
            'Reply with JSON only: {"text": "...", "image_prompt": "..."}'
        )
        user = f"Channel facts:\n{channel_facts(target)}"
        if hints.strip():
            user += f"\n\nWhat the owner wants the ad to say or feel like:\n{hints.strip()[:800]}"

        def check(out):
            ad = {"text": str(out.get("text") or "").strip(),
                  "image_prompt": str(out.get("image_prompt") or "").strip()}
            problem = (self._problem(ad, language, language_name, hints)
                       or self._image_problem(ad["image_prompt"], target, picture))
            if problem:
                return None, problem
            ad["image_prompt"] = finish_image_prompt(ad["image_prompt"], picture, target)
            return ad, None
        return self._ask(host, [{"role": "system", "content": system}, {"role": "user", "content": user}], check)

    def image_prompt(self, host: Dict[str, Any], target: Dict[str, Any], text: str, hints: str = "",
                     picture: Optional[Dict[str, str]] = None) -> Optional[str]:
        """Only a new image prompt for an existing ad, e.g. after changing the picture settings."""
        picture = picture_options(picture)
        system = ("You write prompts for AI image generators, for pictures that go with Telegram ads.\n"
                  f"Write image_prompt: {image_rules(picture, target)}\n"
                  'Reply with JSON only: {"image_prompt": "..."}')
        user = f"Channel facts:\n{channel_facts(target)}\n\nThe ad the picture goes with:\n{text.strip()}"
        if hints.strip():
            user += f"\n\nWhat the owner wants:\n{hints.strip()[:800]}"

        def check(out):
            prompt = str(out.get("image_prompt") or "").strip()
            problem = ("The image_prompt is missing." if not prompt else
                       "The image_prompt must be in English." if PERSIAN_CHARS.search(prompt) else
                       self._image_problem(prompt, target, picture))
            return (None, problem) if problem else (finish_image_prompt(prompt, picture, target), None)
        return self._ask(host, [{"role": "system", "content": system}, {"role": "user", "content": user}], check)

    def _ask(self, host: Dict[str, Any], messages: List[Dict[str, str]], check):
        """Up to 3 tries; each problem goes back to the AI as feedback."""
        for _ in range(3):
            try:
                with tagged(bot_id=host["id"], purpose="promo"):
                    out = self.client.complete_json(messages, temperature=0.8, max_tokens=1000)
            except LlmUnavailable as exc:
                logger.warning("promo writer unavailable: %s", exc)
                return None
            result, problem = check(out)
            if not problem:
                return result
            messages += [{"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                         {"role": "user", "content": f"{problem} Reply with the corrected JSON."}]
        return None

    @staticmethod
    def _image_problem(prompt: str, target: Dict[str, Any], picture: Optional[Dict[str, str]] = None
                       ) -> Optional[str]:
        picture = picture or dict(PICTURE_DEFAULT)
        body = NO_TEXT_SENTENCE.sub("", prompt)
        lower = body.lower()
        if len(body.split()) < 60:
            return ("The image_prompt is too thin. Write 90-150 words: style and medium, one concrete "
                    "subject, setting, camera position, composition, light, a named colour palette.")
        cliches = sorted({m.lower() for m in CLICHES.findall(body)}
                         - (PHONE_WORDS if picture["phone"] != "none" else set()))
        if cliches:
            return (f"The image_prompt uses stock props ({', '.join(cliches)}). Find one fresh visual idea, "
                    "a real moment or a metaphor, and rewrite it without them.")
        origin = sorted({m.lower() for m in ORIGIN.findall(body)})
        if origin:
            return (f"The image_prompt says {', '.join(origin)}. A picture can't show nationality or "
                    "language; describe only what is visible (age, clothes, expression, gesture).")
        copied = [m for m in EXAMPLE_MARKERS if re.search(rf"\b{re.escape(m)}", lower)]
        if len(copied) >= 3:
            return (f"The image_prompt copies the example ({', '.join(copied)}). Invent a different setting, "
                    "time of day, person and pose.")
        shown = _shown_name(picture, target)
        if shown and not re.search(rf'["“„]{re.escape(shown)}["”“]', body):
            return f'The image_prompt must contain the exact words "{shown}" in double quotes.'
        if picture["phone"] == "blank" and not re.search(r"\b(blank|plain|empty)\b[^.]{0,60}\bscreen|"
                                                        r"\bscreen\b[^.]{0,60}\b(blank|plain|empty)\b", lower):
            return "The phone screen must be completely blank (plain bright white), so a screenshot fits on it."
        if picture["phone"] != "none" and not re.search(r"\b(?:smart)?phone\b", lower):
            return "The image_prompt must include the hand holding a phone."
        # Brands come out as garbled logos: outlets, Telegram and (unless it's the title) the channel.
        names = ["Telegram"] + ([] if shown == target.get("name") else [target.get("name") or ""])
        for source in _feed_names(target):  # "tagesschau.de" -> also "tagesschau"; "DW Deutsch" -> also "DW"
            names += [source, source.split(".")[0], source.split()[0]]
        unquoted = re.sub(rf'["“„]{re.escape(shown)}["”“]', "", body) if shown else body
        named = list(dict.fromkeys(n for n in names if len(n) >= 2
                                   and re.search(rf"\b{re.escape(n)}\b", unquoted, re.I)))
        if named:
            return (f"The image_prompt names {', '.join(named)}. No brands, outlets, channel names or "
                    "logos; show the idea with objects and scenes instead.")
        return None

    @staticmethod
    def _problem(ad: Dict[str, str], language: str, language_name: str, hints: str = "") -> Optional[str]:
        if not ad["text"] or not ad["image_prompt"]:
            return "The text or the image_prompt is missing."
        first_line = ad["text"].splitlines()[0].lower()
        if WELCOME.search(first_line) and not WELCOME.search(hints.lower()):
            return "It is an ad, not a welcome message: don't start with a greeting or \"welcome\"."
        if len(ad["text"]) > TEXT_MAX:
            return f"The text is too long ({len(ad['text'])} characters); keep it under {TEXT_MAX}."
        if re.search(r"https?://|t\.me/|@\w", ad["text"]):
            return "Remove the link / @name from the text; it is added automatically."
        if language == "fa" and len(PERSIAN_CHARS.findall(ad["text"])) < 20:
            return f"The text must be written in {language_name}."
        if PERSIAN_CHARS.search(ad["image_prompt"]):
            return "The image_prompt must be in English."
        return None


def format_promo(text: str, link: Optional[str], language: str = "fa") -> str:
    """The ad as posted: the text, then the join link."""
    body = html.escape(text.strip())
    if link:
        body += f'\n\n👉 <a href="{html.escape(link)}">{JOIN.get(language, JOIN["en"])}</a>'
    return body


def publisher_for(bot: Dict[str, Any]) -> Optional[TelegramPublisher]:
    if not bot or not bot.get("bot_token") or not bot.get("telegram_chat_id"):
        return None
    return TelegramPublisher(bot["bot_token"], str(bot["telegram_chat_id"]))


def target_link(target: Dict[str, Any]) -> Optional[str]:
    publisher = publisher_for(target)
    return publisher.channel_link() if publisher else None


def post_promo(promo_id: int, pin: bool = False) -> Dict[str, Any]:
    """Send the ad to the host channel; with `pin`, pin it there. Returns {"ok", "error"}."""
    promo = db.get_promo(promo_id, with_image=True)
    if not promo:
        return {"ok": False, "error": "Ad not found"}
    host, target = db.get_bot(promo["bot_id"]), db.get_bot(promo["target_bot_id"])
    publisher = publisher_for(host)
    if not publisher:
        return {"ok": False, "error": "The channel has no bot token or chat id yet"}
    if not promo["text"].strip():
        return {"ok": False, "error": "The ad has no text"}
    link = promo.get("link") or (target_link(target) if target else None)
    message = format_promo(promo["text"], link, host.get("target_language") or "fa")
    if promo.get("image") and len(message) <= 1024:
        sent = publisher.send_photo(promo["image"], caption=message, filename="ad.jpg",
                                    content_type=_image_type(promo["image"]))
    else:
        sent = publisher.send_message(message, link_preview=not promo.get("image"))
    if not sent.ok:
        db.update_promo(promo_id, {"error": sent.error})
        return {"ok": False, "error": sent.error}
    members = publisher_for(target).member_count() if target and publisher_for(target) else None
    fields = {"posted_at": _now(), "message_id": sent.message_id, "link": link, "members_before": members,
              "error": None}
    result = {"ok": True, "error": None, "message_id": sent.message_id}
    if pin:
        pinned = publisher.pin(sent.message_id)
        fields["pinned"] = 1 if pinned.ok else 0
        if not pinned.ok:  # posted, but not pinned: say why (usually a missing admin right)
            result["error"] = f"Posted, but could not pin: {pinned.error}"
    db.update_promo(promo_id, fields)
    return result


def _image_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def is_image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or _image_type(data) != "image/jpeg"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

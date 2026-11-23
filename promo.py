"""Ads for our channels, written by the AI from hints and the channel's real settings.

An ad is posted in one channel (the "host") and promotes the same channel or another of
ours (the "target"), e.g. the Persian news channel telling its readers about the German
learning channel. The AI also writes an English prompt for an image generator; the
picture itself is made by hand in another tool and uploaded in the panel.
"""
import html
import json
import logging
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
IMAGE_RULES = (
    "a ready-to-paste prompt in English for an AI image generator (ChatGPT, Gemini, Midjourney), "
    "one paragraph of 80-140 words, for a square picture people stop scrolling for.\n"
    "  First think of ONE fresh visual idea that fits only this channel: a real moment with a feeling "
    "(someone understanding a German sentence for the first time, a city scene at a special hour) or a "
    "simple visual metaphor. Then describe that scene like a film director: who exactly (age, clothes, "
    "expression, gesture), where exactly (a specific kind of place, weather, time of day), what is "
    "happening right now.\n"
    "  Start with the style and medium (e.g. risograph print, gouache illustration, cinematic 35mm "
    "photo, paper-cut collage, soft clay 3D) and then give the camera angle, where the subject sits in the "
    "square, an empty calm upper third, the light, and a palette of 3-4 named colours.\n"
    "  Avoid clichés: no globes, flags, stacks of books, light bulbs, speech bubbles, phones or laptops "
    "showing icons, newspapers with headlines, coffee mugs, laptops, generic 'smiling young adult'. "
    "Never name brands, news outlets, the channel, apps or logos, and nothing with readable writing.\n"
    "  The quality to aim for (do not copy these):\n"
    "  * Risograph print in two inks, deep teal and tomato red on warm off-white paper with visible grain. "
    "A woman in her thirties in a yellow raincoat waits on a rainy Berlin S-Bahn platform at dawn, eyes "
    "closed, one earbud in, a small smile as if she just understood something. The rain around her head "
    "turns into tiny paper birds flying upward. Low angle, woman in the lower left, the birds leading the "
    "eye up into a calm, empty pale sky. Quiet, hopeful mood. No text, letters, numbers, logos or "
    "watermarks anywhere in the image.\n"
    "  * Cinematic photograph, 35mm, shallow depth of field, golden hour. From a wooden punt on a calm "
    "river, an old man in a flat cap and a teenage girl lean over the side laughing; behind them pastel "
    "half-timbered houses mirror in the water and willow branches hang in from above. Subjects in the "
    "lower half, wide calm sky above. Palette: honey gold, sage green, faded rose, river blue. No text, "
    "letters, numbers, logos or watermarks anywhere in the image."
)
# Stock props that make every ad picture look the same.
CLICHES = re.compile(r"\b(globe|flags?|stacks? of books|language books|light ?bulb|speech bubbles?|"
                     r"smartphone|laptop|tablet|headlines?|coffee mug|mug|audio icons?|icons?)\b", re.I)
NO_TEXT = "No text, letters, numbers, logos or watermarks anywhere in the image."
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

    def write(self, host: Dict[str, Any], target: Dict[str, Any], hints: str = "",
              tone: str = "friendly") -> Optional[Dict[str, str]]:
        """{"text", "image_prompt"}, or None if no AI provider gave a usable answer."""
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
            f"- image_prompt: {IMAGE_RULES}\n"
            'Reply with JSON only: {"text": "...", "image_prompt": "..."}'
        )
        user = f"Channel facts:\n{channel_facts(target)}"
        if hints.strip():
            user += f"\n\nWhat the owner wants the ad to say or feel like:\n{hints.strip()[:800]}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(3):
            try:
                with tagged(bot_id=host["id"], purpose="promo"):
                    out = self.client.complete_json(messages, temperature=0.8, max_tokens=900)
            except LlmUnavailable as exc:
                logger.warning("promo writer unavailable: %s", exc)
                return None
            ad = {"text": str(out.get("text") or "").strip(), "image_prompt": str(out.get("image_prompt") or "").strip()}
            problem = (self._problem(ad, language, language_name, hints)
                       or self._image_problem(ad["image_prompt"], target))
            if not problem:
                if "no text" not in ad["image_prompt"].lower():
                    ad["image_prompt"] = f"{ad['image_prompt'].rstrip()} {NO_TEXT}"
                return ad
            messages += [{"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                         {"role": "user", "content": f"{problem} Reply with the corrected JSON."}]
        return None

    @staticmethod
    def _image_problem(prompt: str, target: Dict[str, Any]) -> Optional[str]:
        if len(prompt.split()) < 60:
            return ("The image_prompt is too thin. Write 80-140 words: style and medium, one concrete "
                    "subject, setting with props, composition, light, a named colour palette.")
        # Brands come out as garbled logos: the outlets, the channel and Telegram must not be named.
        cliches = sorted({m.lower() for m in CLICHES.findall(prompt.split("No text")[0])})
        if cliches:
            return (f"The image_prompt uses stock props ({', '.join(cliches)}). Find one fresh visual idea, "
                    "a real moment or a metaphor, and rewrite it without them.")
        names = [target.get("name") or "", "Telegram"]
        for source in _feed_names(target):  # "tagesschau.de" -> also "tagesschau"; "DW Deutsch" -> also "DW"
            names += [source, source.split(".")[0], source.split()[0]]
        named = list(dict.fromkeys(n for n in names if len(n) >= 2
                                   and re.search(rf"\b{re.escape(n)}\b", prompt, re.I)))
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

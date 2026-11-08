"""The bot side of learning channels.

Each lesson post has a button that opens the bot with /start l<lesson id>. The bot
then sends that person, privately, the lesson's words and grammar note and the
practice questions. Updates come in by long polling (getUpdates), one thread per
learning-bot token, so the server needs no public URL. Only one process may poll a
token: run gunicorn with a single worker.
"""
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

import requests

import database as db
from news_platform.learning.lesson import format_practice, lesson_from_record
from news_platform.publishing.telegram import API_BASE, TelegramPublisher

DEEP_LINK = re.compile(r"^/start\s+l(\d+)\s*$")
DELETE = re.compile(r"^/(loeschen|delete)(@\w+)?\s*$", re.IGNORECASE)
UPDATES = '["message","poll_answer","message_reaction_count"]'
STALE_SECONDS = 600  # don't answer messages older than this (e.g. queued while the server was down)
REPEAT_SECONDS = 120  # tapping the same button again within this time sends nothing new

MESSAGES = {
    "fa": {
        "welcome": "سلام! 👋\nزیر هر خبر در کانال روی دکمه‌ی «✏️ Üben · تمرین» بزنید تا لغات، گرامر و تمرین‌های "
                   "همان خبر اینجا برایتان بیاید.",
        "missing": "این درس پیدا نشد. لطفاً دوباره از دکمه‌ی زیر خبر در کانال استفاده کنید.",
        "start": "🧩 {n} تمرین برای این خبر — جواب درست را انتخاب کنید:",
        "privacy": "ℹ️ برای نمایش پیشرفت شما، شناسه‌ی تلگرام، نام کوچک و جواب‌های تمرین‌ها ذخیره می‌شود. "
                   "برای پاک کردن همه‌ی آن‌ها /loeschen را بفرستید.",
        "deleted": "✅ همه‌ی اطلاعات شما پاک شد. اگر دوباره تمرین کنید، از نو ذخیره می‌شود.",
    },
    "en": {
        "welcome": "Hi! 👋\nTap \"✏️ Practice\" under a news post in the channel to get its words, grammar and "
                   "exercises here.",
        "missing": "This lesson wasn't found. Please use the button under the post in the channel again.",
        "start": "🧩 {n} exercises for this story — pick the right answer:",
        "privacy": "ℹ️ To show your progress, your Telegram id, first name and exercise answers are stored. "
                   "Send /loeschen to delete them.",
        "deleted": "✅ All your data was deleted. If you practise again, it is stored anew.",
    },
}


def practice_link(bot_username: str, lesson_id: int) -> str:
    return f"https://t.me/{bot_username}?start=l{lesson_id}"


class PracticeBot:
    def __init__(self, token: str):
        self.token = token
        self.logger = logging.getLogger(__name__)
        self._recent: Dict[tuple, float] = {}
        self._bot_id: Optional[int] = None

    @property
    def bot_id(self) -> Optional[int]:
        if self._bot_id is None:
            self._bot_id = db.bot_id_for_token(self.token)
        return self._bot_id

    def handle(self, update: Dict[str, Any]) -> None:
        if "poll_answer" in update:
            self._answer(update["poll_answer"])
            return
        if "message_reaction_count" in update:
            self._reactions(update["message_reaction_count"])
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or time.time() - message.get("date", 0) > STALE_SECONDS:
            return
        publisher = TelegramPublisher(self.token, str(chat["id"]))
        user = message.get("from") or {"id": chat["id"], "first_name": chat.get("first_name")}
        text = (message.get("text") or "").strip()
        if DELETE.match(text):
            db.delete_learner(user["id"])
            publisher.send_message(MESSAGES["fa"]["deleted"], link_preview=False)
            return
        match = DEEP_LINK.match(text)
        lesson_id = int(match.group(1)) if match else None
        record = db.get_lesson(lesson_id) if lesson_id else None
        explain = (record or {}).get("explain_language", "fa")
        texts = MESSAGES.get(explain, MESSAGES["en"])
        if db.touch_learner(self.bot_id, user):
            publisher.send_message(texts["privacy"], link_preview=False)
        if not match:
            publisher.send_message(MESSAGES["fa"]["welcome"], link_preview=False)
            return
        if not record:
            publisher.send_message(MESSAGES["fa"]["missing"], link_preview=False)
            return
        key = (chat["id"], lesson_id)
        if time.time() - self._recent.get(key, 0) < REPEAT_SECONDS:
            return
        self._recent[key] = time.time()
        bot_id = db.lesson_bot_id(lesson_id) or self.bot_id
        db.log_practice(bot_id, lesson_id, user["id"], "open")
        self.send_practice(publisher, record, lesson_id=lesson_id, user_id=user["id"], bot_id=bot_id)

    @staticmethod
    def send_practice(publisher: TelegramPublisher, record: Dict[str, Any], lesson_id: Optional[int] = None,
                      user_id: Optional[int] = None, bot_id: Optional[int] = None) -> None:
        explain = record.get("explain_language", "fa")
        texts = MESSAGES.get(explain, MESSAGES["en"])
        lesson = lesson_from_record(record)
        publisher.send_message(format_practice(lesson, explain), link_preview=False)
        exercises = record.get("exercises") or []
        if exercises:
            publisher.send_message(texts["start"].format(n=len(exercises)), link_preview=False)
        for position, exercise in enumerate(exercises):
            sent = publisher.send_quiz(exercise["q"], exercise["options"], exercise["answer"],
                                       explanation=exercise.get("explanation", ""), anonymous=user_id is None)
            if user_id is not None and sent.ok and sent.poll_id:
                db.save_quiz_poll(sent.poll_id, bot_id, lesson_id, user_id, position, exercise.get("kind"),
                                  exercise.get("word"), exercise["answer"])

    @staticmethod
    def _answer(answer: Dict[str, Any]) -> None:
        user = answer.get("user") or {}
        options = answer.get("option_ids") or []
        if user.get("id") and answer.get("poll_id") and options:
            db.save_quiz_answer(answer["poll_id"], user["id"], options[0])

    @staticmethod
    def _reactions(update: Dict[str, Any]) -> None:
        chat, message_id = (update.get("chat") or {}).get("id"), update.get("message_id")
        if chat is None or message_id is None:
            return
        counts = {}
        for item in update.get("reactions") or []:
            kind = item.get("type") or {}
            label = kind.get("emoji") or kind.get("custom_emoji_id") or kind.get("type") or "?"
            counts[label] = counts.get(label, 0) + int(item.get("total_count") or 0)
        db.save_reactions(str(chat), message_id, counts)

    def run(self, stop: threading.Event) -> None:
        offset: Optional[int] = None
        while not stop.is_set():
            try:
                params = {"timeout": 50, "allowed_updates": UPDATES}
                if offset is not None:
                    params["offset"] = offset
                data = requests.get(f"{API_BASE}/bot{self.token}/getUpdates", params=params, timeout=65).json()
            except Exception as exc:
                self.logger.warning("practice bot: getUpdates failed: %s", exc)
                stop.wait(10)
                continue
            if not data.get("ok"):
                # 409: another process polls this token, or a webhook is set.
                self.logger.warning("practice bot: getUpdates refused: %s", data.get("description"))
                stop.wait(30)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:
                    self.logger.exception("practice bot: update failed")


def start_practice_bots(check_every: float = 60) -> threading.Thread:
    """Keep one polling thread per learning-bot token (active or not, so old posts keep working)."""
    logger = logging.getLogger(__name__)
    running: Dict[str, threading.Event] = {}

    def supervise():
        while True:
            try:
                tokens = {b["bot_token"] for b in db.get_bots(include_secrets=True)
                          if b.get("mode") == "learning" and b.get("bot_token")}
            except Exception:
                logger.exception("practice bot: could not read bots")
                tokens = set(running)
            for token in set(running) - tokens:
                running.pop(token).set()
            for token in tokens - set(running):
                running[token] = threading.Event()
                threading.Thread(target=PracticeBot(token).run, args=(running[token],), daemon=True,
                                 name="practice-bot").start()
                logger.info("practice bot: polling started for a learning bot")
            time.sleep(check_every)

    thread = threading.Thread(target=supervise, daemon=True, name="practice-bots")
    thread.start()
    return thread

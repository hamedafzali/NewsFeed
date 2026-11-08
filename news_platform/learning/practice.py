"""The bot side of learning channels.

Each lesson post has a button that opens the bot with /start l<lesson id>. The bot
then sends that person, privately, the lesson's words and grammar note and the
practice questions. Updates come in by long polling (getUpdates), one thread per
learning-bot token, so the server needs no public URL. Only one process may poll a
token: run gunicorn with a single worker.

Privacy: nothing about the people who use the bot is stored — no Telegram ids, names
or answers. The quizzes are anonymous; only how often a lesson's button was used is
counted.
"""
import json
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
# Reaction counts of the channel's posts (totals only; Telegram doesn't say who reacted).
UPDATES = json.dumps(["message", "message_reaction_count"])
STALE_SECONDS = 600  # don't answer messages older than this (e.g. queued while the server was down)
REPEAT_SECONDS = 120  # tapping the same button again within this time sends nothing new

MESSAGES = {
    "fa": {
        "welcome": "سلام! 👋\nزیر هر خبر در کانال روی دکمه‌ی «✏️ Üben · تمرین» بزنید تا لغات، گرامر و تمرین‌های "
                   "همان خبر اینجا برایتان بیاید.",
        "missing": "این درس پیدا نشد. لطفاً دوباره از دکمه‌ی زیر خبر در کانال استفاده کنید.",
        "start": "🧩 {n} تمرین برای این خبر — جواب درست را انتخاب کنید:",
    },
    "en": {
        "welcome": "Hi! 👋\nTap \"✏️ Practice\" under a news post in the channel to get its words, grammar and "
                   "exercises here.",
        "missing": "This lesson wasn't found. Please use the button under the post in the channel again.",
        "start": "🧩 {n} exercises for this story — pick the right answer:",
    },
}


def practice_link(bot_username: str, lesson_id: int) -> str:
    return f"https://t.me/{bot_username}?start=l{lesson_id}"


class PracticeBot:
    def __init__(self, token: str):
        self.token = token
        self.logger = logging.getLogger(__name__)
        self._recent: Dict[tuple, float] = {}  # in memory only, to ignore double taps

    def handle(self, update: Dict[str, Any]) -> None:
        if "message_reaction_count" in update:
            self._reactions(update["message_reaction_count"])
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private" or time.time() - message.get("date", 0) > STALE_SECONDS:
            return
        publisher = TelegramPublisher(self.token, str(chat["id"]))
        match = DEEP_LINK.match((message.get("text") or "").strip())
        if not match:
            publisher.send_message(MESSAGES["fa"]["welcome"], link_preview=False)
            return
        lesson_id = int(match.group(1))
        record = db.get_lesson(lesson_id)
        if not record:
            publisher.send_message(MESSAGES["fa"]["missing"], link_preview=False)
            return
        now = time.time()
        self._recent = {k: t for k, t in self._recent.items() if now - t < REPEAT_SECONDS}
        key = (chat["id"], lesson_id)
        if key in self._recent:
            return
        self._recent[key] = now
        db.count_open(db.lesson_bot_id(lesson_id), lesson_id)
        self.send_practice(publisher, record)

    @staticmethod
    def send_practice(publisher: TelegramPublisher, record: Dict[str, Any]) -> None:
        explain = record.get("explain_language", "fa")
        texts = MESSAGES.get(explain, MESSAGES["en"])
        lesson = lesson_from_record(record)
        publisher.send_message(format_practice(lesson, explain), link_preview=False)
        exercises = record.get("exercises") or []
        if exercises:
            publisher.send_message(texts["start"].format(n=len(exercises)), link_preview=False)
        for exercise in exercises:
            publisher.send_quiz(exercise["q"], exercise["options"], exercise["answer"],
                                explanation=exercise.get("explanation", ""))

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

    def clear_commands(self) -> None:
        """Remove the command menu an earlier version set (/fortschritt, /wiederholen, /loeschen)."""
        try:
            requests.post(f"{API_BASE}/bot{self.token}/deleteMyCommands", json={}, timeout=15)
        except Exception as exc:
            self.logger.warning("practice bot: deleteMyCommands failed: %s", exc)

    def run(self, stop: threading.Event) -> None:
        self.clear_commands()
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

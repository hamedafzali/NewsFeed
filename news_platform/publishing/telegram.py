"""Telegram channel publisher using the plain Bot HTTP API (no async library needed)."""
import html
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

API_BASE = "https://api.telegram.org"

# Link label per target language for the "read more" anchor.
READ_MORE = {"fa": "متن کامل", "de": "Weiterlesen", "en": "Read more"}


@dataclass
class SendResult:
    ok: bool
    error: Optional[str] = None
    message_id: Optional[int] = None
    poll_id: Optional[str] = None


class TelegramPublisher:
    _usernames: dict = {}  # bot token -> @username, looked up once
    _chat_usernames: dict = {}  # (bot token, chat id) -> the channel's public @username, looked up once

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)

    def bot_username(self) -> Optional[str]:
        """The bot's @username (without @), for t.me links; None if Telegram can't be reached."""
        if self.bot_token not in self._usernames:
            try:
                resp = requests.get(f"{API_BASE}/bot{self.bot_token}/getMe", timeout=self.timeout)
                username = resp.json().get("result", {}).get("username")
            except Exception as exc:
                self.logger.error("Telegram getMe failed: %s", exc)
                return None
            if not username:
                return None
            self._usernames[self.bot_token] = username
        return self._usernames[self.bot_token]

    def chat_username(self) -> Optional[str]:
        """The channel's public @username (without @), so posts can name the channel;
        None for private channels or if Telegram can't be reached."""
        chat = str(self.chat_id or "")
        if chat.startswith("@"):
            return chat[1:]
        key = (self.bot_token, chat)
        if key not in self._chat_usernames:
            try:
                resp = requests.get(f"{API_BASE}/bot{self.bot_token}/getChat", params={"chat_id": chat},
                                    timeout=self.timeout)
                data = resp.json()
            except Exception as exc:
                self.logger.warning("Telegram getChat failed: %s", exc)
                return None
            if not data.get("ok"):
                return None
            self._chat_usernames[key] = data.get("result", {}).get("username")
        return self._chat_usernames[key]

    def member_count(self) -> Optional[int]:
        """Subscribers of the channel (needs the bot to be in it), or None."""
        try:
            resp = requests.get(f"{API_BASE}/bot{self.bot_token}/getChatMemberCount",
                                params={"chat_id": self.chat_id}, timeout=self.timeout)
            data = resp.json()
        except Exception as exc:
            self.logger.warning("Telegram getChatMemberCount failed: %s", exc)
            return None
        return data.get("result") if data.get("ok") else None

    def test_token(self) -> bool:
        try:
            resp = requests.get(f"{API_BASE}/bot{self.bot_token}/getMe", timeout=self.timeout)
            return resp.status_code == 200 and resp.json().get("ok", False)
        except Exception as exc:
            self.logger.error("Telegram getMe failed: %s", exc)
            return False

    def test_chat(self) -> SendResult:
        """Check the bot can see the configured chat (must be member/admin)."""
        try:
            resp = requests.get(f"{API_BASE}/bot{self.bot_token}/getChat",
                                params={"chat_id": self.chat_id}, timeout=self.timeout)
            data = resp.json()
            if data.get("ok"):
                return SendResult(ok=True)
            return SendResult(ok=False, error=data.get("description", f"HTTP {resp.status_code}"))
        except Exception as exc:
            return SendResult(ok=False, error=str(exc))

    def send_article(self, title: str, summary: str, url: str, source: str,
                     language: str = "fa", image_url: Optional[str] = None) -> SendResult:
        message = self.format_message(title, summary, url, source, language)
        channel = self.chat_username()
        if channel:  # forwarded or copied posts still lead back to the channel
            message += f"\n📢 @{channel}"
        if image_url and len(message) <= 1024:
            result = self._call("sendPhoto", {
                "chat_id": self.chat_id,
                "photo": image_url,
                "caption": message,
                "parse_mode": "HTML",
            })
            if result.ok:
                return result
            # Bad/unreachable image URLs are common in RSS — fall back to text.
            self.logger.warning("sendPhoto failed (%s), falling back to text", result.error)
        return self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })

    def send_message(self, text: str, link_preview: bool = True,
                     reply_markup: Optional[dict] = None) -> SendResult:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not link_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def send_audio(self, data: bytes, title: str, performer: str = "", caption: str = "",
                   duration: Optional[int] = None, filename: str = "lesson.mp3",
                   reply_markup: Optional[dict] = None) -> SendResult:
        payload = {"chat_id": self.chat_id, "title": title[:64], "performer": performer[:64],
                   "caption": caption, "parse_mode": "HTML"}
        if duration:
            payload["duration"] = str(duration)
        if reply_markup:  # multipart form fields are strings
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._call("sendAudio", payload, files={"audio": (filename, data, "audio/mpeg")})

    def send_video(self, data: bytes, caption: str = "", duration: Optional[int] = None, width: int = 0,
                   height: int = 0, filename: str = "lesson.mp4", reply_markup: Optional[dict] = None) -> SendResult:
        payload = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML", "supports_streaming": "true"}
        for key, value in (("duration", duration), ("width", width), ("height", height)):
            if value:
                payload[key] = str(value)
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        return self._call("sendVideo", payload, files={"video": (filename, data, "video/mp4")})

    def send_quiz(self, question: str, options: list, correct: int, explanation: str = "",
                  anonymous: bool = True) -> SendResult:
        payload = {
            "chat_id": self.chat_id,
            "question": question,
            "options": [{"text": o} for o in options],
            "type": "quiz",
            "correct_option_id": correct,
            # Channels need anonymous polls. In a private chat a non-anonymous one lets the bot
            # receive the answer (poll_answer), for the learner's progress; nobody else sees it.
            "is_anonymous": anonymous,
        }
        if explanation:  # shown after answering
            payload["explanation"] = explanation[:200]
        return self._call("sendPoll", payload)

    @staticmethod
    def format_message(title: str, summary: str, url: str, source: str, language: str = "fa") -> str:
        title = html.escape((title or "").strip())
        summary = html.escape((summary or "").strip())
        source = html.escape((source or "Unknown").strip())
        label = READ_MORE.get(language, READ_MORE["en"])
        body = f"<b>{title}</b>"
        if summary and summary != title:
            body += f"\n\n{summary}"
        body += f'\n\n📰 <a href="{html.escape(url or "")}">{label}</a> | {source}'
        return body

    def _call(self, method: str, payload: dict, retried: bool = False, files: Optional[dict] = None) -> SendResult:
        try:
            url = f"{API_BASE}/bot{self.bot_token}/{method}"
            if files:  # uploads go as multipart form data
                resp = requests.post(url, data=payload, files=files, timeout=max(self.timeout, 60))
            else:
                resp = requests.post(url, json=payload, timeout=self.timeout)
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 200 and data.get("ok"):
                result = data.get("result") or {}
                return SendResult(ok=True, message_id=result.get("message_id"),
                                  poll_id=(result.get("poll") or {}).get("id"))
            if resp.status_code == 429 and not retried:
                retry_after = int(data.get("parameters", {}).get("retry_after", 3))
                self.logger.warning("Telegram rate limit, waiting %ss", retry_after)
                time.sleep(min(retry_after, 30))
                return self._call(method, payload, retried=True, files=files)
            error = data.get("description") or f"HTTP {resp.status_code}"
            self.logger.error("Telegram %s failed: %s", method, error)
            return SendResult(ok=False, error=error)
        except Exception as exc:
            self.logger.error("Telegram %s error: %s", method, exc)
            return SendResult(ok=False, error=str(exc))

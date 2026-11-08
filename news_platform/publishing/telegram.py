"""Telegram channel publisher using the plain Bot HTTP API (no async library needed)."""
import html
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


class TelegramPublisher:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)

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

    def send_message(self, text: str, link_preview: bool = True) -> SendResult:
        return self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not link_preview,
        })

    def send_audio(self, data: bytes, title: str, performer: str = "", caption: str = "",
                   duration: Optional[int] = None, filename: str = "lesson.mp3") -> SendResult:
        payload = {"chat_id": self.chat_id, "title": title[:64], "performer": performer[:64],
                   "caption": caption[:1024], "parse_mode": "HTML"}
        if duration:
            payload["duration"] = str(duration)
        return self._call("sendAudio", payload, files={"audio": (filename, data, "audio/mpeg")})

    def send_quiz(self, question: str, options: list, correct: int) -> SendResult:
        # Polls in channels are always anonymous.
        return self._call("sendPoll", {
            "chat_id": self.chat_id,
            "question": question,
            "options": [{"text": o} for o in options],
            "type": "quiz",
            "correct_option_id": correct,
            "is_anonymous": True,
        })

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
                return SendResult(ok=True, message_id=data.get("result", {}).get("message_id"))
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

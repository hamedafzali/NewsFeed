"""Translation service: LibreTranslate (local, unlimited) with MyMemory fallback.

Language pairs not directly supported (e.g. de→fa) are pivoted through English,
matching the proven two-step approach: de→en→fa.
"""
import logging
import os
from typing import Optional

import requests


class Translator:
    def __init__(self, libretranslate_url: Optional[str] = None, timeout: int = 20):
        self.libretranslate_url = (libretranslate_url or os.getenv("LIBRETRANSLATE_URL", "")).rstrip("/")
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)

    def translate(self, text: str, source: str, target: str) -> str:
        """Translate text; returns the original text when translation is unavailable."""
        text = (text or "").strip()
        if not text or source == target:
            return text
        # Pivot through English for pairs like de→fa that LibreTranslate/MyMemory
        # don't serve directly.
        if source != "en" and target != "en":
            english = self._translate_pair(text, source, "en")
            if english and english != text:
                return self._translate_pair(english, "en", target) or english
            return self._translate_pair(text, source, target) or text
        return self._translate_pair(text, source, target) or text

    def _translate_pair(self, text: str, source: str, target: str) -> Optional[str]:
        translated = self._libretranslate(text, source, target)
        if translated:
            return translated
        return self._mymemory(text, source, target)

    def _libretranslate(self, text: str, source: str, target: str) -> Optional[str]:
        if not self.libretranslate_url:
            return None
        try:
            resp = requests.post(
                f"{self.libretranslate_url}/translate",
                json={"q": text, "source": source, "target": target, "format": "text"},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                result = resp.json().get("translatedText", "").strip()
                return result or None
            self.logger.warning("LibreTranslate %s→%s returned %s", source, target, resp.status_code)
        except Exception as exc:
            self.logger.warning("LibreTranslate %s→%s failed: %s", source, target, exc)
        return None

    def _mymemory(self, text: str, source: str, target: str) -> Optional[str]:
        """MyMemory free API — 500 chars per request, no key needed, daily quota."""
        try:
            resp = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text[:500], "langpair": f"{source}|{target}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("responseStatus") == 200:
                    translated = (data["responseData"].get("translatedText") or "").strip()
                    if translated and "PLEASE SELECT" not in translated.upper() and len(translated) > 3:
                        return translated
        except Exception as exc:
            self.logger.debug("MyMemory %s→%s failed: %s", source, target, exc)
        return None

"""Mistral-backed post writer: summarises an article and writes it in the channel's language.

Configured like Trainova: MISTRAL_API_KEY in the deployment's environment turns it
on (empty = off, and the processor falls back to LibreTranslate). MISTRAL_MODEL pins
a model; otherwise models are tried in order, skipping ones the key's tier can't use
(403/404) and ones out of quota (429, cooled down for a while). Requests are spaced
to stay under the key's requests-per-minute limit, read from Mistral's response
headers (free keys may allow only 2 a minute); when that limit is hit, the client waits
for the next slot and asks the same model again instead of giving up.
"""
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests

API_URL = "https://api.mistral.ai/v1/chat/completions"
MODELS = ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest",
          "open-mistral-nemo", "ministral-8b-latest"]

LANGUAGE_NAMES = {"fa": "Persian (Farsi)", "en": "English", "de": "German"}
PERSIAN_CHARS = re.compile(r"[؀-ۿ]")
# Small models sometimes slip into other scripts mid-word (e.g. Devanagari in Persian).
FOREIGN_SCRIPT = re.compile(r"[ऀ-෿Ѐ-ӿ぀-ヿ一-鿿]")
# A Latin fragment glued to Persian letters ("اوleshky") is a half-translated name.
MIXED_WORD = re.compile(r"[؀-ۿ][A-Za-z]|[A-Za-z][؀-ۿ]")
# Whole Latin words left untranslated ("Hals", "Interface"); acronyms like BCI/NATO are fine.
LATIN_WORD = re.compile(r"^(?![A-Z0-9]{2,6}$)[A-Za-z][A-Za-z'-]*$")
PUNCTUATION = "،؛؟«»()[]\"'.,:;!?…"


class LlmUnavailable(Exception):
    pass


class MistralClient:
    # Shared across instances: the processor builds a new client per run, but what
    # we learned about the key (denied models, quota cooldowns) should persist.
    _lock = threading.Lock()
    _denied: set = set()
    _cooling_until: Dict[str, float] = {}
    _working: Optional[str] = None
    _last_start = 0.0
    _key_interval = 0.0  # seconds between requests the key's per-minute limit allows

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, timeout: int = 45):
        self.api_key = (api_key if api_key is not None else os.getenv("MISTRAL_API_KEY", "")).strip()
        self.pinned = (model if model is not None else os.getenv("MISTRAL_MODEL", "")).strip()
        self.timeout = timeout
        self.min_interval = float(os.getenv("MISTRAL_MIN_INTERVAL_MS", "1100")) / 1000
        self.cooldown = float(os.getenv("MISTRAL_RATE_COOLDOWN_MS", "600000")) / 1000
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def candidate_models(self) -> List[str]:
        cls = MistralClient
        ordered = ([cls._working] if cls._working else []) + ([self.pinned] if self.pinned else []) + MODELS
        usable = [m for m in dict.fromkeys(ordered) if m not in cls._denied]
        now = time.time()
        ready = [m for m in usable if cls._cooling_until.get(m, 0) <= now]
        # Everything cooling down: try anyway rather than fail without asking.
        return ready or usable

    def complete_json(self, messages: List[Dict[str, str]], temperature: float = 0.3, max_tokens: int = 900) -> Dict[str, Any]:
        if not self.enabled:
            raise LlmUnavailable("MISTRAL_API_KEY is not set")
        cls = MistralClient
        models = self.candidate_models()
        if not models:
            raise LlmUnavailable("no Mistral model is available for this API key")
        for model in models:
            status, data = self._post(model, messages, temperature, max_tokens)
            for _ in range(2):
                if status != 429:
                    break
                # Out of requests for this minute (the limit is per key, so another model
                # wouldn't help): ask again in the next slot. With a known per-minute limit
                # _post already waits for that slot; without one, pause briefly.
                if not cls._key_interval:
                    time.sleep(1.5)
                status, data = self._post(model, messages, temperature, max_tokens)
            if status == 200:
                if cls._working != model:
                    self.logger.info("using Mistral model %s", model)
                cls._working = model
                text = data["choices"][0]["message"]["content"]
                parsed = parse_json_object(text if isinstance(text, str) else "")
                if parsed is None:
                    raise LlmUnavailable("unreadable model output")
                return parsed
            if status == 401:
                raise LlmUnavailable("MISTRAL_API_KEY was rejected (401)")
            if status in (403, 404):
                self.logger.warning("Mistral model %s not available for this key, trying the next one", model)
                cls._denied.add(model)
            elif status == 429:
                self.logger.warning("Mistral model %s is rate limited, trying the next one", model)
                cls._cooling_until[model] = time.time() + self.cooldown
            else:
                raise LlmUnavailable(f"Mistral returned {status}")
            if cls._working == model:
                cls._working = None
        raise LlmUnavailable("all Mistral models refused the request")

    def _post(self, model: str, messages, temperature: float, max_tokens: int):
        self._wait_for_slot()
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LlmUnavailable(f"Mistral request failed: {exc}") from exc
        self._learn_limit(resp.headers)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return resp.status_code, data

    @staticmethod
    def _learn_limit(headers) -> None:
        try:
            per_minute = int(headers.get("x-ratelimit-limit-req-minute") or 0)
        except (TypeError, ValueError):
            return
        if per_minute > 0:
            MistralClient._key_interval = 60.0 / per_minute + 0.5

    def _wait_for_slot(self):
        cls = MistralClient
        with cls._lock:
            wait = cls._last_start + max(self.min_interval, cls._key_interval) - time.time()
            if wait > 0:
                time.sleep(wait)
            cls._last_start = time.time()


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object out of model text (tolerates code fences / leading prose)."""
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced:
        candidate = fenced.group(1)
    else:
        braces = re.search(r"\{[\s\S]*\}", text)
        candidate = braces.group(0) if braces else text
    try:
        value = json.loads(candidate)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# --- Other providers (OpenAI-style chat API) and the provider chain ---

PROVIDERS = {
    # name: (endpoint, key variable, model variable, default model, extra request fields)
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-2.5-flash", {"reasoning_effort": "none"}),
    "groq": ("https://api.groq.com/openai/v1/chat/completions",
             "GROQ_API_KEY", "GROQ_MODEL", "llama-3.3-70b-versatile", {}),
}


class OpenAICompatClient:
    """One model of a provider with an OpenAI-style API (Gemini, Groq)."""
    _last_start: Dict[str, float] = {}
    _cooling_until: Dict[str, float] = {}
    _lock = threading.Lock()
    MAX_RETRY_WAIT = 60.0

    def __init__(self, name: str, api_key: Optional[str] = None, model: Optional[str] = None, timeout: int = 60):
        url, key_var, model_var, default_model, extra = PROVIDERS[name]
        self.name, self.url, self.extra = name, url, extra
        self.api_key = (api_key if api_key is not None else os.getenv(key_var, "")).strip()
        self.model = (model if model is not None else os.getenv(model_var, "")).strip() or default_model
        self.timeout = timeout
        self.min_interval = float(os.getenv("LLM_MIN_INTERVAL_MS", "2000")) / 1000
        self.cooldown = float(os.getenv("MISTRAL_RATE_COOLDOWN_MS", "600000")) / 1000
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def complete_json(self, messages: List[Dict[str, str]], temperature: float = 0.3, max_tokens: int = 900) -> Dict[str, Any]:
        if not self.enabled:
            raise LlmUnavailable(f"{self.name}: no API key")
        if OpenAICompatClient._cooling_until.get(self.name, 0) > time.time():
            raise LlmUnavailable(f"{self.name}: out of quota for now")
        payload = {"model": self.model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "response_format": {"type": "json_object"}, **self.extra}
        resp = self._post(payload)
        if resp.status_code == 400 and ("response_format" in payload or self.extra):
            # Some models don't take JSON mode or the extra fields: ask plainly (the JSON is parsed anyway).
            payload = {k: v for k, v in payload.items() if k != "response_format" and k not in self.extra}
            resp = self._post(payload)
        if resp.status_code == 429:
            wait = _retry_after(resp.headers)
            if wait is not None and wait <= self.MAX_RETRY_WAIT:
                time.sleep(wait + 0.5)
                resp = self._post(payload)
        if resp.status_code == 429:
            OpenAICompatClient._cooling_until[self.name] = time.time() + self.cooldown
            raise LlmUnavailable(f"{self.name}: rate limited")
        if resp.status_code in (401, 403):
            raise LlmUnavailable(f"{self.name}: API key rejected ({resp.status_code})")
        if resp.status_code != 200:
            raise LlmUnavailable(f"{self.name} returned {resp.status_code}")
        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmUnavailable(f"{self.name}: unreadable response")
        parsed = parse_json_object(text if isinstance(text, str) else "")
        if parsed is None:
            raise LlmUnavailable(f"{self.name}: unreadable model output")
        return parsed

    def _post(self, payload: Dict[str, Any]):
        with OpenAICompatClient._lock:
            wait = OpenAICompatClient._last_start.get(self.name, 0) + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            OpenAICompatClient._last_start[self.name] = time.time()
        try:
            return requests.post(self.url, headers={"Authorization": f"Bearer {self.api_key}"},
                                 json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LlmUnavailable(f"{self.name} request failed: {exc}") from exc


def _retry_after(headers) -> Optional[float]:
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


class ChainClient:
    """Asks the providers in order; the first that answers wins."""

    def __init__(self, clients: List[Any]):
        self.clients = clients
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return any(c.enabled for c in self.clients)

    @property
    def names(self) -> List[str]:
        return [getattr(c, "name", "mistral") for c in self.clients if c.enabled]

    def complete_json(self, messages: List[Dict[str, str]], temperature: float = 0.3, max_tokens: int = 900) -> Dict[str, Any]:
        problems = []
        for client in (c for c in self.clients if c.enabled):
            try:
                return client.complete_json(messages, temperature=temperature, max_tokens=max_tokens)
            except LlmUnavailable as exc:
                problems.append(str(exc))
                self.logger.warning("LLM provider failed, trying the next one: %s", exc)
        raise LlmUnavailable("all LLM providers failed: " + "; ".join(problems) if problems else "no LLM provider has a key")


def _provider(name: str):
    name = name.strip().lower()
    if name == "mistral":
        client = MistralClient()
        client.name = "mistral"
        return client
    if name in PROVIDERS:
        return OpenAICompatClient(name)
    logging.getLogger(__name__).warning("unknown LLM provider %r in LLM_PROVIDERS", name)
    return None


def make_client(variable: str = "LLM_PROVIDERS", rotate: int = 0) -> ChainClient:
    """The provider chain named in `variable` (comma-separated, e.g. "gemini,groq,mistral").

    Empty = Mistral only, as before. Providers without a key are skipped. `rotate` moves the
    first providers to the end, so another model can e.g. fact-check the writer's text.
    """
    names = [n for n in (os.getenv(variable) or os.getenv("LLM_PROVIDERS") or "mistral").split(",") if n.strip()]
    clients = [c for c in (_provider(n) for n in names) if c is not None]
    if rotate:
        enabled = [c for c in clients if c.enabled]
        if len(enabled) > 1:
            k = rotate % len(enabled)
            clients = enabled[k:] + enabled[:k]
    return ChainClient(clients)


class PostWriter:
    """Turns an article into a channel post (title + short summary) in the target language."""

    def __init__(self, client=None):
        self.client = client or make_client()
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    def write(self, title: str, text: str, source_language: str, target_language: str) -> Optional[Dict[str, Any]]:
        """Returns {"title", "summary", "summary_en"}, {"skip": True} for non-news, or None on failure.

        Output that fails the checks is sent back with what was wrong, up to twice.
        """
        target_name = LANGUAGE_NAMES.get(target_language, target_language)
        system = (
            f"You are the editor of a {target_name} news channel on Telegram. "
            f"Given a news article in any language, write a post in {target_name}.\n"
            "Rules:\n"
            "- title: a short, clear headline. No outlet name, no clickbait.\n"
            "- summary: 2-3 short sentences, at most 60 words, with the key facts (who, what, where). "
            "Neutral tone; no opinions or speculation.\n"
            "- Accuracy first: only facts stated in the article. Keep numbers, titles and roles exactly "
            "as written (hundreds stays hundreds, First Lady stays First Lady). Do not add dates, "
            "places or background the article does not give.\n"
            f"- Write names of people, places and organisations the way {target_name} news media "
            "normally spell them (e.g. BBC Persian / DW Farsi for Persian), not in Latin letters.\n"
            "- summary_en: the same summary in English.\n"
            'Reply with JSON only: {"title": "...", "summary": "...", "summary_en": "..."}.\n'
            "Features, analysis and interviews count as news. "
            'Only if the text is not journalism at all (an ad, a video page with no text, a live ticker, '
            'a newsletter teaser), reply {"skip": true}.'
        )
        user = f"Article language: {source_language}\nTitle: {title}\n\nArticle text:\n{(text or title)[:4000]}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(3):
            try:
                out = self.client.complete_json(messages, temperature=0.2)
            except LlmUnavailable as exc:
                self.logger.warning("post writer unavailable: %s", exc)
                return None
            if out.get("skip") is True:
                return {"skip": True}
            post = {key: str(out.get(key) or "").strip() for key in ("title", "summary", "summary_en")}
            problem = self._problem(post, target_language, target_name)
            if not problem:
                return post
            self.logger.warning("post writer output rejected (%s): %s", problem, post["title"])
            messages += [
                {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                {"role": "user", "content": f"{problem} Reply with the corrected JSON."},
            ]
        return None

    @staticmethod
    def _problem(post: Dict[str, str], target_language: str, target_name: str) -> Optional[str]:
        """What is wrong with a post, phrased as feedback to the model; None if it is fine."""
        if not post["title"] or not post["summary"]:
            return "The title or summary is missing."
        if target_language != "fa":
            return None
        if not PERSIAN_CHARS.search(post["summary"]):
            return f"The summary must be written in {target_name}."
        text = f"{post['title']} {post['summary']}"
        words = [w.strip(PUNCTUATION) for w in text.split()]
        bad = [w for w in words if FOREIGN_SCRIPT.search(w) or MIXED_WORD.search(w) or LATIN_WORD.match(w)]
        if bad:
            return (f"These words are not in Persian script: {', '.join(dict.fromkeys(bad))}. "
                    "Write each one entirely in Persian letters (transliterate names, as BBC Persian would).")
        return None

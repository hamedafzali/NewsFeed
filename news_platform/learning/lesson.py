"""Turns a news article into a language lesson: simple text, vocabulary, a grammar
note and a short quiz.

Mistral writes the text, picks the words and writes the questions; the checks here
drop what doesn't hold up (words that aren't in the text, questions whose answer
isn't in it) and send the rest back with what to fix, like the post writer does.
"""
import html
import json
import logging
import random
import re
from typing import Any, Dict, List, Optional

from news_platform.learning.grammar import GrammarTopic, find_topic, topics_for
from news_platform.pipeline.llm import LANGUAGE_NAMES, PERSIAN_CHARS, LlmUnavailable, MistralClient

LEVEL_GUIDE = {
    "A1": "5-6 very short main clauses, present tense, only everyday words",
    "A2": "5-7 short sentences, present tense and Perfekt, everyday words, few subordinate clauses",
    "B1": "6-9 short sentences, Perfekt for past events, common news words, some subordinate clauses",
    "B2": "8-10 sentences, normal news language with explanations of special terms",
}
LEVELS = list(LEVEL_GUIDE)
# Longest sentence (in words) a level allows; longer ones go back to be split.
MAX_SENTENCE_WORDS = {"A1": 10, "A2": 13, "B1": 18, "B2": 25}
MIN_WORDS, MAX_WORDS = 5, 8
MIN_QUESTIONS, MAX_QUESTIONS = 2, 3
# Telegram limits for quiz polls.
POLL_QUESTION_MAX, POLL_OPTION_MAX = 255, 100
NOT_TARGET_SCRIPT = re.compile(r"[؀-ۿЀ-ӿ一-鿿]")
LATIN = re.compile(r"[A-Za-z]")
ARTICLES = {"der", "die", "das", "sich"}
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MARKDOWN = re.compile(r"\*+|__+|`+")


def _plain(value: Any) -> str:
    """Model text without markdown marks (Telegram gets HTML; the voice would read them out)."""
    return re.sub(r"\s+", " ", MARKDOWN.sub("", str(value or ""))).strip()


def _words(text: str) -> List[str]:
    return re.findall(r"[a-zäöüß]+", (text or "").lower())


def _in_text(form: str, text_words: List[str]) -> bool:
    """Is this word (or an inflected form of it) used in the text?"""
    form = form.lower()
    if len(form) < 3:
        return False
    stem = form[:max(4, len(form) - 3)] if len(form) > 5 else form
    return any(w.startswith(stem) for w in text_words)


def headword_forms(entry: str) -> List[str]:
    """Forms of a vocabulary entry to look for in the text.

    "die Wahl, -en" -> ["wahl"]; "beschließen, hat beschlossen" -> ["beschließen", "beschlossen"].
    """
    head, _, rest = re.sub(r"\(.*?\)", " ", entry).partition(",")
    words = [w for w in _words(head) if w not in ARTICLES]
    forms = words[-1:] if words else []
    tail = _words(rest)
    if tail and len(tail[-1]) > 3:
        forms.append(tail[-1])  # Perfekt participle of a verb
    return forms


class LessonWriter:
    def __init__(self, client: Optional[MistralClient] = None):
        self.client = client or MistralClient()
        self.logger = logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    def write(self, title: str, text: str, language: str = "de", level: str = "B1",
              explain_language: str = "fa") -> Optional[Dict[str, Any]]:
        """Returns a checked lesson, {"skip": True} for non-news, or None if no clean lesson came out."""
        level = level if level in LEVEL_GUIDE else "B1"
        topics = topics_for(language)[:]
        random.shuffle(topics)  # the model tends to take the first fitting topic: vary it
        messages = [{"role": "system", "content": self._prompt(language, level, explain_language, topics)},
                    {"role": "user", "content": f"Title: {title}\n\nArticle:\n{(text or title)[:5000]}"}]
        for _ in range(3):
            try:
                out = self.client.complete_json(messages, temperature=0.3, max_tokens=1800)
            except LlmUnavailable as exc:
                self.logger.warning("lesson writer unavailable: %s", exc)
                return None
            if out.get("skip") is True:
                return {"skip": True}
            # Some answers wrap everything in one key ({"lesson": {...}}).
            if "text" not in out and len(out) == 1 and isinstance(next(iter(out.values())), dict):
                out = next(iter(out.values()))
            lesson, problem = self.check(out, language, explain_language, level)
            if lesson:
                errors = self.fact_check(text or title, lesson["text"], language)
                if errors is None:
                    return None
                if not errors:
                    lesson["level"] = level
                    return lesson
                problem = ("These statements in your text are wrong or not in the article: "
                           + " | ".join(errors) + ". Fix them in the text, vocabulary and quiz.")
            self.logger.warning("lesson rejected (%s): %s %s", problem, out.get("title"), sorted(out)[:8])
            messages += [{"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                         {"role": "user", "content": f"{problem} Reply with the corrected JSON."}]
        return None

    def fact_check(self, article: str, lesson_text: str, language: str) -> Optional[List[str]]:
        """Statements of the lesson the article doesn't support; None if the check itself failed."""
        lang = LANGUAGE_NAMES.get(language, language)
        messages = [
            {"role": "system", "content": (
                f"You fact-check a simplified {lang} news text written for learners against the original article.\n"
                "List only statements in the simplified text that contradict the article or are not in it: wrong "
                "numbers, dates or years, names, places, or who did what to whom. Do NOT list style, wording, "
                "punctuation, quotation marks, grammar, simplifications or things left out.\n"
                "At most 6 items, each one short line. "
                'Reply with JSON only: {"errors": ["<quote from the simplified text> -> <what the article says>"]}, '
                'or {"errors": []} if there are none.')},
            {"role": "user", "content": f"ARTICLE:\n{article[:5000]}\n\nSIMPLIFIED TEXT:\n{lesson_text}"},
        ]
        out = None
        for _ in range(2):  # a long answer sometimes comes back cut off: ask once more
            try:
                out = self.client.complete_json(messages, temperature=0.0, max_tokens=1200)
                break
            except LlmUnavailable as exc:
                self.logger.warning("lesson fact check unavailable: %s", exc)
        if out is None:
            return None
        errors = [str(e).strip() for e in (out.get("errors") or []) if str(e).strip()]
        kept = [e for e in errors if self._quotes_lesson(e, lesson_text)]
        if errors:
            self.logger.info("lesson fact check found: %s (kept %d)", errors, len(kept))
        return kept[:6]

    @staticmethod
    def _quotes_lesson(error: str, lesson_text: str) -> bool:
        """Does the error quote the lesson? Small models also "find" article sentences, or nothing at all."""
        quoted = error.split("->", 1)[0]
        words = [w for w in _words(_plain(quoted)) if len(w) >= 4]
        if not words:
            return False
        lesson_words = set(_words(lesson_text))
        return sum(w in lesson_words for w in words) >= 0.6 * len(words)

    @staticmethod
    def _prompt(language: str, level: str, explain_language: str, topics: List[GrammarTopic]) -> str:
        lang = LANGUAGE_NAMES.get(language, language)
        explain = LANGUAGE_NAMES.get(explain_language, explain_language)
        topic_list = "\n".join(f"- {t.id}: {t.name}" for t in topics)
        return (
            f"You write lessons for a Telegram channel where {explain} speakers learn {lang} from real news.\n"
            f"From the article, write JSON with these keys:\n"
            f'"title": a short {lang} headline in simple words.\n'
            f'"text": the news retold in simple {lang} at CEFR level {level}: {LEVEL_GUIDE[level]}. '
            f"At most {MAX_SENTENCE_WORDS[level]} words per sentence. "
            "Only facts from the article; no opinions. Correct, natural grammar. Plain text: no markdown, "
            "no asterisks.\n"
            f'"vocabulary": {MAX_WORDS - 1} useful words that appear in your text. Each: '
            '{"de": nouns with article and plural ("die Wahl, -en"; uncountable: "die Gewalt (Sg.)"), '
            'verbs as infinitive + Perfekt ("beschließen, hat beschlossen"), other words as in a dictionary; '
            f'"fa": the usual {explain} dictionary translation in {explain} script only; '
            '"en": the English meaning}}.\n'
            '"grammar_topic": the id of the first topic in this list that a sentence of your text clearly shows:\n'
            f"{topic_list}\n"
            '"grammar_example": that sentence, copied exactly from your text.\n'
            f'"quiz": {MAX_QUESTIONS} comprehension questions in simple {lang}, each '
            '{"q": question, "options": 3 short options, "answer": index of the correct option}. '
            "The answer must be stated in your text.\n"
            '"summary_en": one English sentence saying what the story is about.\n'
            "Reply with JSON only. If the article is not news (an ad, a video page with no text, a live ticker), "
            'reply {"skip": true}.'
        )

    def check(self, out: Dict[str, Any], language: str, explain_language: str, level: str = "B1"):
        """(lesson, None) when usable after dropping weak parts, else (None, feedback for the model)."""
        title = _plain(out.get("title"))
        text = _plain(out.get("text"))
        if not title or len(text) < 250:
            return None, "The title is missing or the text is too short."
        if NOT_TARGET_SCRIPT.search(title + text):
            return None, f"Write the title and text only in {LANGUAGE_NAMES.get(language, language)}."
        sentences = [s.strip() for s in SENTENCE_END.split(text) if s.strip()]
        limit = MAX_SENTENCE_WORDS.get(level, MAX_SENTENCE_WORDS["B1"])
        too_long = [s for s in sentences if len(s.split()) > limit + 3]  # a little slack
        if too_long:
            return None, (f"These sentences are too long for level {level} (at most {limit} words each); "
                          "split them into shorter sentences: " + " | ".join(too_long))
        text_words = _words(text)

        vocabulary, missing = [], []
        for item in out.get("vocabulary") or []:
            if not isinstance(item, dict):
                continue
            entry = _plain(item.get("de") or item.get(language))
            meaning = _plain(item.get("fa") or item.get(explain_language))
            english = _plain(item.get("en"))
            if not entry or not meaning:
                continue
            if explain_language == "fa" and (not PERSIAN_CHARS.search(meaning) or LATIN.search(meaning)):
                continue
            if not any(_in_text(f, text_words) for f in headword_forms(entry)):
                missing.append(entry)
                continue
            vocabulary.append({"word": entry, "meaning": meaning, "en": english})
        vocabulary = vocabulary[:MAX_WORDS]
        if len(vocabulary) < MIN_WORDS:
            hint = f" Not in your text: {', '.join(missing)}." if missing else ""
            return None, f"Give at least {MIN_WORDS} vocabulary words that appear in your text.{hint}"

        topic, example = self._grammar(out, language, sentences)
        if not topic:
            return None, ("No sentence of your text shows the grammar topic you chose. "
                          "Choose another topic from the list and copy a sentence that shows it.")

        quiz = []
        for item in out.get("quiz") or []:
            question = self._question(item, text_words)
            if question:
                quiz.append(question)
        quiz = quiz[:MAX_QUESTIONS]
        if len(quiz) < MIN_QUESTIONS:
            return None, (f"Write {MAX_QUESTIONS} questions with 3 short options each whose correct answer "
                          "is stated in your text.")

        return {
            "title": title,
            "text": text,
            "vocabulary": vocabulary,
            "grammar": {"topic": topic, "example": example},
            "quiz": quiz,
            "summary_en": _plain(out.get("summary_en")),
        }, None

    @staticmethod
    def _grammar(out: Dict[str, Any], language: str, sentences: List[str]):
        """The chosen topic and a sentence of the text that shows it."""
        topic = find_topic(language, str(out.get("grammar_topic") or "").strip())
        if not topic:
            return None, None
        wanted = _plain(out.get("grammar_example")).replace("_", "")
        for sentence in sentences:
            if wanted and (wanted.rstrip(".!? ") in sentence) and topic.shown_by(sentence):
                return topic, sentence
        # The model's example isn't from the text or doesn't show the topic: use one that does.
        match = next((s for s in sentences if topic.shown_by(s)), None)
        return (topic, match) if match else (None, None)

    @staticmethod
    def _question(item: Any, text_words: List[str]) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        q = _plain(item.get("q"))
        options = [_plain(o) for o in (item.get("options") or []) if _plain(o)]
        try:
            answer = int(item.get("answer"))
        except (TypeError, ValueError):
            return None
        if (not q or len(q) > POLL_QUESTION_MAX or not 3 <= len(options) <= 4 or len(set(options)) != len(options)
                or any(len(o) > POLL_OPTION_MAX for o in options) or not 0 <= answer < len(options)):
            return None
        # The right answer has to come from the text, not from the article or the model.
        key_words = [w for w in _words(options[answer]) if len(w) >= 4]
        if key_words and sum(_in_text(w, text_words) for w in key_words) < len(key_words) / 2:
            return None
        correct = options[answer]
        random.shuffle(options)  # models put the right answer in the same place every time
        return {"q": q, "options": options, "answer": options.index(correct)}


LABELS = {
    "de": {"words": "Wörter", "grammar": "Grammatik", "source": "Quelle", "level": "Niveau",
           "series": "Langsame Nachrichten", "listen": "Langsam gelesen – hören und mitlesen"},
}
EXPLAIN_LABELS = {"fa": {"words": "لغات", "grammar": "گرامر"}, "en": {"words": "Words", "grammar": "Grammar"}}


def format_lesson(lesson: Dict[str, Any], url: str, source: str, language: str = "de",
                  explain_language: str = "fa") -> str:
    """The lesson's main message (Telegram HTML)."""
    def e(value: str) -> str:
        return html.escape(value, quote=False)
    labels = LABELS.get(language, LABELS["de"])
    explain = EXPLAIN_LABELS.get(explain_language, EXPLAIN_LABELS["en"])
    level = lesson.get("level", "B1")
    lines = [f"🗞 <b>{e(lesson['title'])}</b>",
             f"<i>{labels['level']} {e(level)} · {labels['series']}</i>",
             "",
             e(lesson["text"]),
             "",
             f"📚 <b>{labels['words']} · {explain['words']}</b>"]
    for word in lesson["vocabulary"]:
        english = f" ({e(word['en'])})" if word.get("en") else ""
        lines.append(f"▪️ <b>{e(word['word'])}</b> — {e(word['meaning'])}{english}")
    topic: GrammarTopic = lesson["grammar"]["topic"]
    lines += ["",
              f"✏️ <b>{labels['grammar']} · {explain['grammar']}: {e(topic.name)}</b>",
              e(topic.explanation_fa),
              f"🔹 <i>{e(lesson['grammar']['example'])}</i>",
              "",
              f'📰 {labels["source"]}: <a href="{html.escape(url or "")}">{e(source or "")}</a>',
              f"#{e(level)} #{labels['series'].replace(' ', '')}"]
    return "\n".join(lines)

"""Practice questions about the words and grammar of a lesson, built by code.

Every question comes from the lesson's own vocabulary list and text, so the right
answer is right by construction (no model writes quiz content):
  - meaning:  what does "die Krise" mean?        -> its translation vs. two others from the list
  - gap:      a sentence of the text with a word left out -> that word vs. two other words of the text
  - article:  ___ Krise                         -> der / die / das
  - perfekt:  Perfekt of "erwarten"?             -> hat erwartet / ist erwartet
"""
import random
import re
from typing import Any, Dict, List, Optional

from news_platform.learning.lesson import SENTENCE_END, headword_forms, stem_of

# Telegram limits for quiz polls.
QUESTION_MAX, OPTION_MAX, EXPLANATION_MAX = 255, 100, 200
NOUN = re.compile(r"^(der|die|das)\s+([A-ZÄÖÜ][\wäöüß-]*)")
PERFEKT = re.compile(r",\s*(hat|ist)\s+((?:sich\s+)?[\wäöüß]+)\s*$")
TOKEN = re.compile(r"[A-Za-zÄÖÜäöüß]+")

PROMPTS = {
    "fa": {"meaning": "معنی این کلمه چیست؟", "gap": "کدام کلمه در جای خالی درست است؟",
           "article": "حرف تعریف درست کدام است؟", "perfekt": "Perfekt درست کدام است؟"},
    "en": {"meaning": "What does it mean?", "gap": "Which word fits the gap?",
           "article": "Which article is right?", "perfekt": "Which Perfekt form is right?"},
}


def _head(entry: str) -> str:
    """The entry without plural or Perfekt part: "die Wahl, -en" -> "die Wahl"."""
    return re.sub(r"\(.*?\)", "", entry.split(",")[0]).strip()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _surface(entry: str, text: str):
    """(sentence, word as written in the text) for the first use of the entry in the text."""
    stems = [stem_of(f) for f in headword_forms(entry) if len(f) >= 3]
    for sentence in SENTENCE_END.split(text):
        for token in TOKEN.findall(sentence):
            if any(token.lower().startswith(s) for s in stems):
                return sentence.strip(), token
    return None, None


def _quiz(kind: str, question: str, options: List[str], correct: str, explanation: str,
          rng: random.Random, shuffle: bool = True) -> Optional[Dict[str, Any]]:
    options = [o.strip() for o in options]
    if len(set(o.lower() for o in options)) != len(options) or any(len(o) > OPTION_MAX for o in options):
        return None
    if len(question) > QUESTION_MAX:
        return None
    if shuffle:
        rng.shuffle(options)
    return {"kind": kind, "q": question, "options": options, "answer": options.index(correct.strip()),
            "explanation": _cut(explanation, EXPLANATION_MAX)}


def build_exercises(lesson: Dict[str, Any], explain_language: str = "fa", count: int = 6,
                    rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
    rng = rng or random.Random()
    prompts = PROMPTS.get(explain_language, PROMPTS["en"])
    words = list(lesson.get("vocabulary") or [])
    text = lesson.get("text") or ""
    rng.shuffle(words)
    found = {w["word"]: _surface(w["word"], text) for w in words}

    def meaning(word):
        others = [w["meaning"] for w in words if w is not word and w["meaning"] != word["meaning"]]
        if len(others) < 2:
            return None
        sentence = found[word["word"]][0] or ""
        return _quiz("meaning", f"„{_head(word['word'])}“\n{prompts['meaning']}",
                     [word["meaning"]] + rng.sample(others, 2), word["meaning"], sentence, rng)

    def gap(word):
        sentence, token = found[word["word"]]
        if not token:
            return None
        noun = bool(NOUN.match(word["word"]))
        if token[0].isupper() != noun:
            return None  # a capitalised verb at the start of a sentence would give the answer away
        others = [found[w["word"]][1] for w in words if w is not word and found[w["word"]][1]]
        others = [o for o in dict.fromkeys(others) if o.lower() != token.lower()]
        # Nouns with nouns, other words with other words: a verb among two nouns gives the answer away.
        pool = [o for o in others if o[0].isupper() == noun]
        if len(pool) < 2:
            return None
        blanked = re.sub(rf"\b{re.escape(token)}\b", "___", sentence, count=1)
        return _quiz("gap", f"{prompts['gap']}\n\n{blanked}", [token] + rng.sample(pool, 2), token,
                     f"{word['word']} — {word['meaning']}", rng)

    def article(word):
        match = NOUN.match(word["word"])
        if not match:
            return None
        return _quiz("article", f"{prompts['article']}\n\n___ {match.group(2)}", ["der", "die", "das"],
                     match.group(1), f"{_head(word['word'])} — {word['meaning']}", rng, shuffle=False)

    def perfekt(word):
        match = PERFEKT.search(word["word"])
        if not match:
            return None
        infinitive = _head(word["word"])
        return _quiz("perfekt", f"„{infinitive}“\n{prompts['perfekt']}",
                     [f"hat {match.group(2)}", f"ist {match.group(2)}"], f"{match.group(1)} {match.group(2)}",
                     f"{word['word']} — {word['meaning']}", rng, shuffle=False)

    # Two meaning and two gap questions, one article and one Perfekt; other kinds fill in when one runs short.
    plan = ["meaning", "article", "gap", "meaning", "perfekt", "gap"]
    makers = {"meaning": meaning, "gap": gap, "article": article, "perfekt": perfekt}
    exercises: List[Dict[str, Any]] = []
    used = set()  # (kind, word): the same word may come up under two kinds, not twice in one

    def take(kind) -> bool:
        for word in sorted(words, key=lambda w: sum(w["word"] == u for _, u in used)):
            if (kind, word["word"]) in used:
                continue
            question = makers[kind](word)
            if question:
                question["word"] = word["word"]  # for progress: which words a learner gets wrong
                used.add((kind, word["word"]))
                exercises.append(question)
                return True
        return False

    for kind in plan:
        if len(exercises) < count and not take(kind):
            for other in ("gap", "meaning", "article", "perfekt"):
                if take(other):
                    break
    return exercises[:count]

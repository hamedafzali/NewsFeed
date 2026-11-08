"""A learner's progress, computed from their stored quiz answers and Üben taps.

/fortschritt shows it, a score line follows each practice round, and /wiederholen
asks again the questions whose latest answer was wrong.
"""
import html
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import database as db
from news_platform.learning import speech

REVIEW_SIZE = 6
WEAK_WORDS_SHOWN = 8

TEXTS = {
    "fa": {
        "score": "🎯 نتیجه: {correct} از {sent} درست{praise}\n"
                 "/wiederholen مرور اشتباه‌ها · /fortschritt پیشرفت شما",
        "perfect": " — عالی! 👏",
        "empty": "📊 هنوز تمرینی انجام نداده‌اید.\nزیر هر خبر در کانال روی «✏️ Üben» بزنید.",
        "progress": "📊 <b>پیشرفت شما</b>\n\n"
                    "📚 درس‌های تمرین‌شده: {lessons}\n"
                    "✏️ جواب‌ها: {answers} — درست: {correct} ({percent}٪)\n"
                    "📅 هفت روز اخیر: {week} جواب\n"
                    "🔥 روزهای پشت‌سرهم: {streak}",
        "weak": "\n\n❗ برای مرور: {words}\n/wiederholen",
        "nothing_to_review": "🎉 سؤالی برای مرور نمانده؛ همه را آخرین بار درست جواب داده‌اید.",
        "review_start": "🔁 مرور: {n} سؤال که قبلاً اشتباه جواب داده بودید:",
    },
    "en": {
        "score": "🎯 Score: {correct} of {sent} right{praise}\n"
                 "/wiederholen review mistakes · /fortschritt your progress",
        "perfect": " — excellent! 👏",
        "empty": "📊 No practice yet.\nTap \"✏️ Üben\" under a news post in the channel.",
        "progress": "📊 <b>Your progress</b>\n\n"
                    "📚 Lessons practised: {lessons}\n"
                    "✏️ Answers: {answers} — right: {correct} ({percent}%)\n"
                    "📅 Last 7 days: {week} answers\n"
                    "🔥 Days in a row: {streak}",
        "weak": "\n\n❗ To review: {words}\n/wiederholen",
        "nothing_to_review": "🎉 Nothing to review: your latest answer to every question was right.",
        "review_start": "🔁 Review: {n} questions you got wrong before:",
    },
}


def texts(language: str) -> Dict[str, str]:
    return TEXTS.get(language, TEXTS["en"])


def _berlin_day(ts: str) -> date:
    moment = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return moment.astimezone(speech._berlin()).date()


def streak(days: List[date], today: date) -> int:
    """Consecutive days with practice, ending today (or yesterday: today isn't over yet)."""
    have = set(days)
    day = today if today in have else today - timedelta(days=1)
    count = 0
    while day in have:
        count += 1
        day -= timedelta(days=1)
    return count


def due_for_review(answers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Questions (lesson, position) whose latest answer was wrong, most recent first."""
    latest: Dict[tuple, Dict[str, Any]] = {}
    for answer in answers:  # oldest first, so later answers win
        if answer["lesson_id"] is not None and answer["position"] is not None:
            key = (answer["lesson_id"], answer["position"])
            latest.pop(key, None)
            latest[key] = answer
    return [a for a in reversed(list(latest.values())) if not a["correct"]]


def summary(bot_id: Optional[int], user_id: int, today: Optional[date] = None) -> Dict[str, Any]:
    answers = db.learner_answers(bot_id, user_id)
    today = today or datetime.now(speech._berlin()).date()
    week_start = today - timedelta(days=6)
    correct = sum(1 for a in answers if a["correct"])
    weak = []
    for answer in due_for_review(answers):
        if answer["word"] and answer["word"] not in weak:
            weak.append(answer["word"])
    return {
        "answers": len(answers),
        "correct": correct,
        "percent": round(100 * correct / len(answers)) if answers else 0,
        "lessons": len({a["lesson_id"] for a in answers if a["lesson_id"] is not None}),
        "week": sum(1 for a in answers if _berlin_day(a["ts"]) >= week_start),
        "streak": streak([_berlin_day(t) for t in db.learner_activity_times(bot_id, user_id)], today),
        "weak": weak,
        "due": len(due_for_review(answers)),
    }


def format_summary(stats: Dict[str, Any], language: str = "fa") -> str:
    t = texts(language)
    if not stats["answers"]:
        return t["empty"]
    text = t["progress"].format(**stats)
    if stats["weak"]:
        text += t["weak"].format(words="، ".join(html.escape(w) for w in stats["weak"][:WEAK_WORDS_SHOWN])
                                 if language == "fa" else
                                 ", ".join(html.escape(w) for w in stats["weak"][:WEAK_WORDS_SHOWN]))
    return text


def format_score(score: Dict[str, int], language: str = "fa") -> str:
    t = texts(language)
    praise = t["perfect"] if score["sent"] and score["correct"] == score["sent"] else ""
    return t["score"].format(correct=score["correct"], sent=score["sent"], praise=praise)


def review_questions(bot_id: Optional[int], user_id: int, size: int = REVIEW_SIZE,
                     rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
    """Up to `size` wrongly answered questions, rebuilt from their lessons with the options reshuffled."""
    rng = rng or random.Random()
    out = []
    records: Dict[int, Optional[Dict[str, Any]]] = {}
    for answer in due_for_review(db.learner_answers(bot_id, user_id)):
        lesson_id = answer["lesson_id"]
        if lesson_id not in records:
            records[lesson_id] = db.get_lesson(lesson_id)
        exercises = (records[lesson_id] or {}).get("exercises") or []
        if answer["position"] >= len(exercises):
            continue
        exercise = dict(exercises[answer["position"]])
        right = exercise["options"][exercise["answer"]]
        options = list(exercise["options"])
        rng.shuffle(options)
        exercise.update(options=options, answer=options.index(right), lesson_id=lesson_id,
                        position=answer["position"])
        out.append(exercise)
        if len(out) >= size:
            break
    return out

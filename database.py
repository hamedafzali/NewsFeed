"""
Central database manager for panel data: bots (channels), activity log, posts, feeds.

Backed by panel.db — separate from the article store (news_platform.db) so channel
configuration survives article-store rebuilds.
"""
import json
import os
import sqlite3
from datetime import date
from typing import Any, Dict, List, Optional

def _db_path() -> str:
    data_dir = os.getenv("DATA_DIR", "./data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "panel.db")


def _conn():
    c = sqlite3.connect(_db_path())
    c.row_factory = sqlite3.Row
    return c


def _migrate(c):
    """Apply schema migrations safely on existing databases."""
    migrations = [
        "ALTER TABLE global_feeds ADD COLUMN bypass_relevance INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE bots ADD COLUMN target_language TEXT NOT NULL DEFAULT 'fa'",
        "ALTER TABLE posts ADD COLUMN posted INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE posts ADD COLUMN error TEXT",
        "ALTER TABLE bots ADD COLUMN max_posts_per_day INTEGER NOT NULL DEFAULT 2",
        "ALTER TABLE bots ADD COLUMN mode TEXT NOT NULL DEFAULT 'news'",
        "ALTER TABLE bots ADD COLUMN level TEXT NOT NULL DEFAULT 'B1'",
        "ALTER TABLE bots ADD COLUMN run_every_minutes INTEGER NOT NULL DEFAULT 60",
        "ALTER TABLE bots ADD COLUMN post_from_hour INTEGER NOT NULL DEFAULT 8",
        "ALTER TABLE bots ADD COLUMN post_until_hour INTEGER NOT NULL DEFAULT 20",
        "ALTER TABLE posts ADD COLUMN message_id INTEGER",
        "ALTER TABLE posts ADD COLUMN lesson_id INTEGER",
        "ALTER TABLE quiz_polls ADD COLUMN batch TEXT",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except Exception:
            pass  # Column already exists


def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                city_name TEXT NOT NULL,
                country_code TEXT NOT NULL,
                news_language TEXT NOT NULL DEFAULT 'en',
                target_language TEXT NOT NULL DEFAULT 'fa',
                bot_token TEXT NOT NULL,
                telegram_chat_id TEXT NOT NULL,
                openai_api_key TEXT,  -- unused; kept so existing panel.db files still match
                newsapi_key TEXT,     -- unused
                max_posts_per_run INTEGER NOT NULL DEFAULT 2,
                max_posts_per_day INTEGER NOT NULL DEFAULT 2,  -- 0 = no daily limit
                mode TEXT NOT NULL DEFAULT 'news',  -- news | learning
                level TEXT NOT NULL DEFAULT 'B1',   -- CEFR level of learning lessons
                run_every_minutes INTEGER NOT NULL DEFAULT 60,  -- scheduler runs this bot this often
                post_from_hour INTEGER NOT NULL DEFAULT 8,      -- posts only from this hour (Berlin time) ...
                post_until_hour INTEGER NOT NULL DEFAULT 20,    -- ... until this hour
                custom_feeds TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_name TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'api',
                fetched INTEGER NOT NULL DEFAULT 0,
                returned INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS global_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                bypass_relevance INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                title TEXT NOT NULL,
                url TEXT,
                summary_en TEXT,
                summary_fa TEXT,
                sentiment TEXT,
                relevance_score REAL,
                posted INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bot_id) REFERENCES bots(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_posts_bot_url ON posts(bot_id, url);

            -- Lessons of learning channels (JSON), opened in the bot from the post's practice button.
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Analytics. Every AI request (one row per HTTP call, failed ones too).
            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                bot_id INTEGER,
                purpose TEXT,
                provider TEXT,
                model TEXT,
                status INTEGER,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                duration_ms INTEGER,
                run_id INTEGER,
                response TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);

            -- What each bot run did, step by step (fetched, candidates, AI answers, skips, sends).
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                trigger TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                status TEXT,
                fetched INTEGER,
                posted INTEGER,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS run_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                step TEXT NOT NULL,
                summary TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id);

            -- Channel subscribers, one count per bot and (Berlin) day.
            CREATE TABLE IF NOT EXISTS channel_stats (
                bot_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                members INTEGER NOT NULL,
                PRIMARY KEY (bot_id, day)
            );

            -- People who used a learning bot privately. Deleted with /loeschen.
            CREATE TABLE IF NOT EXISTS learners (
                bot_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT,
                language_code TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, user_id)
            );

            -- Üben taps (kind 'open') and similar learner actions.
            CREATE TABLE IF NOT EXISTS practice_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                bot_id INTEGER,
                lesson_id INTEGER,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_practice_user ON practice_events(user_id);

            -- Each quiz sent privately, so its answer can be matched to lesson and word.
            CREATE TABLE IF NOT EXISTS quiz_polls (
                poll_id TEXT PRIMARY KEY,
                bot_id INTEGER,
                lesson_id INTEGER,
                user_id INTEGER NOT NULL,
                position INTEGER,
                kind TEXT,
                word TEXT,
                correct_option INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS quiz_answers (
                poll_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                option INTEGER,
                correct INTEGER,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (poll_id, user_id)
            );

            -- Latest reaction counts of channel posts (Telegram sends totals, not who reacted).
            CREATE TABLE IF NOT EXISTS post_reactions (
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                reactions TEXT NOT NULL,
                total INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, message_id)
            );
        """)
        _migrate(c)


# --- Bots ---

def add_bot(config: Dict[str, Any]) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO bots
               (name, city_name, country_code, news_language, target_language, bot_token,
                telegram_chat_id, max_posts_per_run, max_posts_per_day, custom_feeds, active, mode, level,
                run_every_minutes, post_from_hour, post_until_hour)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                config["name"], config["city_name"], config["country_code"],
                config.get("news_language", "en"), config.get("target_language", "fa"),
                config["bot_token"], config["telegram_chat_id"], config.get("max_posts_per_run", 2),
                config.get("max_posts_per_day", 2), config.get("custom_feeds"), 1 if config.get("active", True) else 0,
                config.get("mode") or "news", config.get("level") or "B1",
                _run_every(config.get("run_every_minutes")),
                _hour(config.get("post_from_hour"), 8), _hour(config.get("post_until_hour"), 20),
            ),
        )
        return cur.lastrowid


def _hour(value, default: int) -> int:
    """An hour of the day for the posting window, 0-24."""
    try:
        return min(24, max(0, int(value)))
    except (TypeError, ValueError):
        return default


def _run_every(value) -> int:
    """Minutes between scheduled runs of a bot: 60 by default, at least 5."""
    try:
        return max(5, int(value))
    except (TypeError, ValueError):
        return 60


def last_run_at(bot_id: int) -> Optional[str]:
    """UTC time ("YYYY-MM-DD HH:MM:SS") the bot last finished a run, manual or scheduled."""
    with _conn() as c:
        row = c.execute("SELECT MAX(created_at) FROM activity WHERE source = ?", (f"bot:{bot_id}",)).fetchone()
        return row[0] if row else None


def get_bots(active_only: bool = False, include_secrets: bool = False) -> List[Dict]:
    with _conn() as c:
        q = "SELECT * FROM bots"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY created_at DESC"
        rows = c.execute(q).fetchall()
        bots = []
        for r in rows:
            b = dict(r)
            # Mask token — show only last 4 chars, never expose the full token in lists
            if b.get("bot_token"):
                b["bot_token_masked"] = "****" + b["bot_token"][-4:]
                if not include_secrets:
                    del b["bot_token"]
            bots.append(b)
        return bots


def get_bot(bot_id: int) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        return dict(row) if row else None


def update_bot(bot_id: int, config: Dict[str, Any]):
    fields = ["name", "city_name", "country_code", "news_language", "target_language",
              "bot_token", "telegram_chat_id", "max_posts_per_run", "max_posts_per_day",
              "custom_feeds", "active", "mode", "level", "run_every_minutes", "post_from_hour", "post_until_hour"]
    updates = {k: config[k] for k in fields if k in config}
    if "run_every_minutes" in updates:
        updates["run_every_minutes"] = _run_every(updates["run_every_minutes"])
    for key, default in (("post_from_hour", 8), ("post_until_hour", 20)):
        if key in updates:
            updates[key] = _hour(updates[key], default)
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as c:
        c.execute(f"UPDATE bots SET {set_clause} WHERE id = ?",
                  list(updates.values()) + [bot_id])


def delete_bot(bot_id: int):
    with _conn() as c:
        c.execute("DELETE FROM bots WHERE id = ?", (bot_id,))


# --- Activity ---

def log_activity(city_name: str, source: str, fetched: int, returned: int,
                 duration_ms: int, error: str = None):
    with _conn() as c:
        c.execute(
            "INSERT INTO activity (city_name, source, fetched, returned, duration_ms, error) VALUES (?,?,?,?,?,?)",
            (city_name, source, fetched, returned, duration_ms, error),
        )


def get_activity(limit: int = 50) -> List[Dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM activity ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_activity_chart(days: int = 7) -> List[Dict]:
    """Return daily fetched/returned totals for the last N days."""
    with _conn() as c:
        rows = c.execute("""
            SELECT
                DATE(created_at) as day,
                SUM(fetched) as fetched,
                SUM(returned) as returned
            FROM activity
            WHERE created_at >= DATE('now', ?)
            GROUP BY DATE(created_at)
            ORDER BY day
        """, (f"-{days} days",)).fetchall()
        return [dict(r) for r in rows]


# --- Lessons ---

def save_lesson(bot_id: Optional[int], data: Dict[str, Any]) -> int:
    with _conn() as c:
        return c.execute("INSERT INTO lessons (bot_id, data) VALUES (?, ?)",
                         (bot_id, json.dumps(data, ensure_ascii=False))).lastrowid


def lesson_bot_id(lesson_id: int) -> Optional[int]:
    with _conn() as c:
        row = c.execute("SELECT bot_id FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        return row["bot_id"] if row else None


def get_lesson(lesson_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT data FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        return json.loads(row["data"]) if row else None


# --- Posts ---

def log_post(bot_id: Optional[int], title: str, url: str, summary_en: str,
             summary_fa: str, sentiment: str = None, relevance_score: float = None,
             posted: bool = True, error: str = None, message_id: Optional[int] = None,
             lesson_id: Optional[int] = None):
    with _conn() as c:
        c.execute(
            """INSERT INTO posts
               (bot_id, title, url, summary_en, summary_fa, sentiment, relevance_score, posted, error,
                message_id, lesson_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (bot_id, title, url, summary_en, summary_fa, sentiment, relevance_score,
             1 if posted else 0, error, message_id, lesson_id),
        )


# Prefix of posts.error for articles deliberately not posted (not news, duplicate story,
# no clean post); unlike failed sends, these are not retried.
SKIPPED = "skipped: "


def has_posted(bot_id: int, url: str) -> bool:
    """True if this article URL was already posted (or deliberately skipped) for this bot."""
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM posts WHERE bot_id = ? AND url = ? AND (posted = 1 OR error LIKE ?) LIMIT 1",
            (bot_id, url, SKIPPED + "%"),
        ).fetchone() is not None


def get_sent_posts(bot_id: int, since: str) -> List[Dict]:
    """Successfully sent posts at or after `since` ("YYYY-MM-DD HH:MM:SS" UTC), newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE bot_id = ? AND posted = 1 AND posted_at >= ? ORDER BY posted_at DESC",
            (bot_id, since),
        ).fetchall()
        return [dict(r) for r in rows]


def count_tries(bot_id: int, since: str) -> int:
    """Articles the bot worked on at or after `since`: sent, skipped or failed."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM posts WHERE bot_id = ? AND posted_at >= ?",
                         (bot_id, since)).fetchone()[0]


def get_posts(bot_id: int = None, limit: int = 50) -> List[Dict]:
    with _conn() as c:
        if bot_id is not None:
            rows = c.execute(
                "SELECT * FROM posts WHERE bot_id = ? ORDER BY posted_at DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM posts ORDER BY posted_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# --- Stats ---

def get_stats() -> Dict[str, Any]:
    today = date.today().isoformat()
    with _conn() as c:
        total_bots = c.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        active_bots = c.execute("SELECT COUNT(*) FROM bots WHERE active = 1").fetchone()[0]
        today_fetched = c.execute(
            "SELECT COALESCE(SUM(fetched),0) FROM activity WHERE DATE(created_at) = ?", (today,)
        ).fetchone()[0]
        today_returned = c.execute(
            "SELECT COALESCE(SUM(returned),0) FROM activity WHERE DATE(created_at) = ?", (today,)
        ).fetchone()[0]
        today_errors = c.execute(
            "SELECT COUNT(*) FROM activity WHERE DATE(created_at) = ? AND error IS NOT NULL", (today,)
        ).fetchone()[0]
        total_posts = c.execute("SELECT COUNT(*) FROM posts WHERE posted = 1").fetchone()[0]
        return {
            "total_bots": total_bots,
            "active_bots": active_bots,
            "fetched_today": today_fetched,
            "returned_today": today_returned,
            "errors_today": today_errors,
            "total_posts": total_posts,
        }


# --- Global Feeds ---

def get_global_feeds(active_only: bool = False) -> List[Dict]:
    with _conn() as c:
        q = "SELECT * FROM global_feeds"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY name"
        return [dict(r) for r in c.execute(q).fetchall()]


def add_global_feed(name: str, url: str, bypass_relevance: int = 0) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO global_feeds (name, url, bypass_relevance) VALUES (?, ?, ?)",
            (name, url, bypass_relevance)
        )
        return cur.lastrowid


def update_global_feed(feed_id: int, name: str = None, url: str = None,
                       active: int = None, bypass_relevance: int = None):
    updates = {}
    if name is not None: updates["name"] = name
    if url is not None: updates["url"] = url
    if active is not None: updates["active"] = active
    if bypass_relevance is not None: updates["bypass_relevance"] = bypass_relevance
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with _conn() as c:
        c.execute(f"UPDATE global_feeds SET {set_clause} WHERE id = ?",
                  list(updates.values()) + [feed_id])


def delete_global_feed(feed_id: int):
    with _conn() as c:
        c.execute("DELETE FROM global_feeds WHERE id = ?", (feed_id,))


# --- Settings ---

def get_setting(key: str, default: str = None) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def get_all_settings() -> Dict[str, str]:
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
        return {r[0]: r[1] for r in rows}


# --- Analytics ---

RESPONSE_KEEP = 12000  # characters of an AI answer kept for the run trace


def log_llm_call(bot_id: Optional[int], purpose: Optional[str], provider: str, model: str, status: int,
                 prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
                 duration_ms: Optional[int] = None, run_id: Optional[int] = None,
                 response: Optional[str] = None, error: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO llm_calls (bot_id, purpose, provider, model, status, prompt_tokens,
                     completion_tokens, duration_ms, run_id, response, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (bot_id, purpose, provider, model, status, prompt_tokens, completion_tokens, duration_ms,
                   run_id, (response or "")[:RESPONSE_KEEP] or None, (error or "")[:500] or None))


def start_run(bot_id: Optional[int], trigger: str = "schedule") -> int:
    with _conn() as c:
        return c.execute("INSERT INTO runs (bot_id, trigger) VALUES (?, ?)", (bot_id, trigger)).lastrowid


def finish_run(run_id: int, status: str, fetched: int = 0, posted: int = 0, error: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute("""UPDATE runs SET finished_at = CURRENT_TIMESTAMP, status = ?, fetched = ?, posted = ?, error = ?
                     WHERE id = ?""", (status, fetched, posted, error, run_id))


def log_step(run_id: Optional[int], step: str, summary: str = "", detail: Any = None) -> None:
    if run_id is None:
        return
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    with _conn() as c:
        c.execute("INSERT INTO run_steps (run_id, step, summary, detail) VALUES (?,?,?,?)",
                  (run_id, step, summary[:500], (detail or "")[:RESPONSE_KEEP] or None))


def prune_traces(days: int = 30) -> None:
    """Run traces and AI answers are bulky: keep them for a month (counts stay)."""
    cutoff = f"-{int(days)} days"
    with _conn() as c:
        c.execute("DELETE FROM run_steps WHERE run_id IN (SELECT id FROM runs WHERE started_at < datetime('now', ?))",
                  (cutoff,))
        c.execute("UPDATE llm_calls SET response = NULL WHERE response IS NOT NULL AND ts < datetime('now', ?)",
                  (cutoff,))


def has_member_count(bot_id: int, day: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM channel_stats WHERE bot_id = ? AND day = ?",
                         (bot_id, day)).fetchone() is not None


def save_member_count(bot_id: int, day: str, members: int) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO channel_stats (bot_id, day, members) VALUES (?,?,?)",
                  (bot_id, day, members))


def bot_id_for_token(token: str) -> Optional[int]:
    with _conn() as c:
        row = c.execute("SELECT id FROM bots WHERE bot_token = ? ORDER BY id LIMIT 1", (token,)).fetchone()
        return row["id"] if row else None


def touch_learner(bot_id: Optional[int], user: Dict[str, Any]) -> bool:
    """Record a private user of a learning bot; True the first time we see them."""
    if bot_id is None or not user.get("id"):
        return False
    with _conn() as c:
        known = c.execute("SELECT 1 FROM learners WHERE bot_id = ? AND user_id = ?",
                          (bot_id, user["id"])).fetchone()
        if known:
            c.execute("""UPDATE learners SET first_name = ?, language_code = ?, last_seen = CURRENT_TIMESTAMP
                         WHERE bot_id = ? AND user_id = ?""",
                      (user.get("first_name"), user.get("language_code"), bot_id, user["id"]))
            return False
        c.execute("INSERT INTO learners (bot_id, user_id, first_name, language_code) VALUES (?,?,?,?)",
                  (bot_id, user["id"], user.get("first_name"), user.get("language_code")))
        return True


def log_practice(bot_id: Optional[int], lesson_id: Optional[int], user_id: int, kind: str = "open") -> None:
    with _conn() as c:
        c.execute("INSERT INTO practice_events (bot_id, lesson_id, user_id, kind) VALUES (?,?,?,?)",
                  (bot_id, lesson_id, user_id, kind))


def save_quiz_poll(poll_id: str, bot_id: Optional[int], lesson_id: Optional[int], user_id: int, position: int,
                   kind: Optional[str], word: Optional[str], correct_option: int, batch: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute("""INSERT OR REPLACE INTO quiz_polls
                     (poll_id, bot_id, lesson_id, user_id, position, kind, word, correct_option, batch)
                     VALUES (?,?,?,?,?,?,?,?,?)""",
                  (poll_id, bot_id, lesson_id, user_id, position, kind, word, correct_option, batch))


def get_quiz_poll(poll_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute("SELECT * FROM quiz_polls WHERE poll_id = ?", (poll_id,)).fetchone()
        return dict(row) if row else None


def batch_score(batch: str, user_id: int) -> Dict[str, int]:
    """How many quizzes of one practice round were sent, answered, and answered right."""
    with _conn() as c:
        row = c.execute("""SELECT COUNT(*) AS sent, COUNT(a.poll_id) AS answered, COALESCE(SUM(a.correct), 0) AS correct
                           FROM quiz_polls p LEFT JOIN quiz_answers a ON a.poll_id = p.poll_id AND a.user_id = p.user_id
                           WHERE p.batch = ? AND p.user_id = ?""", (batch, user_id)).fetchone()
        return dict(row)


def learner_answers(bot_id: Optional[int], user_id: int) -> List[Dict[str, Any]]:
    """A learner's quiz answers on one bot, oldest first."""
    with _conn() as c:
        rows = c.execute("""SELECT p.lesson_id, p.position, p.kind, p.word, a.correct, a.ts
                            FROM quiz_answers a JOIN quiz_polls p ON p.poll_id = a.poll_id
                            WHERE a.user_id = ? AND p.bot_id IS ? ORDER BY a.ts, a.rowid""",
                         (user_id, bot_id)).fetchall()
        return [dict(r) for r in rows]


def learner_activity_times(bot_id: Optional[int], user_id: int) -> List[str]:
    """UTC timestamps of a learner's Üben taps and answers (for the streak)."""
    with _conn() as c:
        rows = c.execute("""SELECT ts FROM practice_events WHERE user_id = ? AND bot_id IS ?
                            UNION ALL
                            SELECT a.ts FROM quiz_answers a JOIN quiz_polls p ON p.poll_id = a.poll_id
                            WHERE a.user_id = ? AND p.bot_id IS ?""", (user_id, bot_id, user_id, bot_id)).fetchall()
        return [r[0] for r in rows]


def save_quiz_answer(poll_id: str, user_id: int, option: Optional[int]) -> Optional[bool]:
    """Store an answer to one of our quizzes; returns whether it was right (None: unknown poll)."""
    with _conn() as c:
        poll = c.execute("SELECT correct_option FROM quiz_polls WHERE poll_id = ?", (poll_id,)).fetchone()
        if not poll:
            return None
        correct = option is not None and option == poll["correct_option"]
        c.execute("INSERT OR REPLACE INTO quiz_answers (poll_id, user_id, option, correct) VALUES (?,?,?,?)",
                  (poll_id, user_id, option, 1 if correct else 0))
        return correct


def save_reactions(chat_id: str, message_id: int, reactions: Dict[str, int]) -> None:
    with _conn() as c:
        c.execute("""INSERT OR REPLACE INTO post_reactions (chat_id, message_id, reactions, total, updated_at)
                     VALUES (?,?,?,?,CURRENT_TIMESTAMP)""",
                  (str(chat_id), message_id, json.dumps(reactions, ensure_ascii=False), sum(reactions.values())))


def delete_learner(user_id: int) -> None:
    """Remove everything stored about one person (all bots)."""
    with _conn() as c:
        c.execute("DELETE FROM quiz_answers WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM quiz_polls WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM practice_events WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM learners WHERE user_id = ?", (user_id,))

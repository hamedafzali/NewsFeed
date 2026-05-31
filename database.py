"""
Central database manager for panel data: bots, activity log, posts.
"""
import os
import sqlite3
from datetime import datetime, date
from typing import Any, Dict, List, Optional

DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "panel.db")


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _migrate(c):
    """Apply schema migrations safely on existing databases."""
    migrations = [
        "ALTER TABLE global_feeds ADD COLUMN bypass_relevance INTEGER NOT NULL DEFAULT 0",
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
                bot_token TEXT NOT NULL,
                telegram_chat_id TEXT NOT NULL,
                openai_api_key TEXT,
                newsapi_key TEXT,
                max_posts_per_run INTEGER NOT NULL DEFAULT 5,
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
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bot_id) REFERENCES bots(id) ON DELETE SET NULL
            );
        """)
        _migrate(c)


# --- Bots ---

def add_bot(config: Dict[str, Any]) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO bots
               (name, city_name, country_code, news_language, bot_token,
                telegram_chat_id, openai_api_key, newsapi_key, max_posts_per_run, custom_feeds, active)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                config["name"], config["city_name"], config["country_code"],
                config.get("news_language", "en"), config["bot_token"],
                config["telegram_chat_id"], config.get("openai_api_key"),
                config.get("newsapi_key"), config.get("max_posts_per_run", 5),
                config.get("custom_feeds"), 1 if config.get("active", True) else 0,
            ),
        )
        return cur.lastrowid


def get_bots() -> List[Dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM bots ORDER BY created_at DESC").fetchall()
        bots = []
        for r in rows:
            b = dict(r)
            # Mask token — show only last 4 chars
            if b.get("bot_token"):
                b["bot_token_masked"] = "****" + b["bot_token"][-4:]
            bots.append(b)
        return bots


def get_bot(bot_id: int) -> Optional[Dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        return dict(row) if row else None


def update_bot(bot_id: int, config: Dict[str, Any]):
    fields = ["name", "city_name", "country_code", "news_language", "bot_token",
              "telegram_chat_id", "openai_api_key", "newsapi_key", "max_posts_per_run",
              "custom_feeds", "active"]
    updates = {k: config[k] for k in fields if k in config}
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


# --- Posts ---

def log_post(bot_id: Optional[int], title: str, url: str, summary_en: str,
             summary_fa: str, sentiment: str = None, relevance_score: float = None):
    with _conn() as c:
        c.execute(
            """INSERT INTO posts
               (bot_id, title, url, summary_en, summary_fa, sentiment, relevance_score)
               VALUES (?,?,?,?,?,?,?)""",
            (bot_id, title, url, summary_en, summary_fa, sentiment, relevance_score),
        )


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
        total_posts = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
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

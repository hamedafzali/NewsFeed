import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from news_platform.models import IntelligenceArticle, Source, SummaryBundle, utc_now


class SQLiteStore:
    def __init__(self, data_dir: str = "./data", db_name: str = "news_platform.db"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, db_name)
        self.init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    reliability REAL NOT NULL DEFAULT 0.7,
                    active INTEGER NOT NULL DEFAULT 1,
                    topic_hint TEXT,
                    last_success_at TEXT,
                    last_error_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    normalized_url TEXT NOT NULL UNIQUE,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    author TEXT,
                    language TEXT NOT NULL DEFAULT 'en',
                    topic TEXT NOT NULL DEFAULT 'world news',
                    published_at TEXT,
                    text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    image_url TEXT,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    people_json TEXT NOT NULL DEFAULT '[]',
                    organizations_json TEXT NOT NULL DEFAULT '[]',
                    locations_json TEXT NOT NULL DEFAULT '[]',
                    summary_tldr TEXT NOT NULL DEFAULT '',
                    summary_bullets_json TEXT NOT NULL DEFAULT '[]',
                    summary_full TEXT NOT NULL DEFAULT '',
                    summary_translated TEXT,
                    relevance_score REAL NOT NULL DEFAULT 0,
                    quality_score REAL NOT NULL DEFAULT 0,
                    extraction_quality REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS processing_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_url TEXT,
                    article_url TEXT,
                    message TEXT,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    stage TEXT NOT NULL,
                    source_url TEXT,
                    article_url TEXT,
                    error_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_articles_source_date ON articles(source_name, published_at);
                CREATE INDEX IF NOT EXISTS idx_articles_topic_date ON articles(topic, published_at);
                CREATE INDEX IF NOT EXISTS idx_articles_language ON articles(language);
                CREATE INDEX IF NOT EXISTS idx_articles_quality ON articles(quality_score);
                CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash);
                CREATE INDEX IF NOT EXISTS idx_logs_stage_status ON processing_logs(stage, status);
                """
            )
            self._seed_default_sources(conn)

    def _seed_default_sources(self, conn: sqlite3.Connection) -> None:
        count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        if count:
            return
        defaults = [
            Source("https://news.google.com/rss/search?q={query}&hl={language}&gl={country}", "Google News", 0.75),
            Source("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World", 0.92, topic_hint="world news"),
            Source("https://www.theverge.com/rss/index.xml", "The Verge", 0.82, topic_hint="tech"),
        ]
        for source in defaults:
            conn.execute(
                "INSERT OR IGNORE INTO sources (name, url, reliability, active, topic_hint) VALUES (?, ?, ?, ?, ?)",
                (source.name, source.url, source.reliability, int(source.active), source.topic_hint),
            )

    def list_sources(self, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sources"
        params: Tuple[Any, ...] = ()
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY name"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def upsert_source(self, source: Source) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (name, url, reliability, active, topic_hint)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    name=excluded.name,
                    reliability=excluded.reliability,
                    active=excluded.active,
                    topic_hint=excluded.topic_hint
                """,
                (source.name or source.url, source.url, source.reliability, int(source.active), source.topic_hint),
            )
            return conn.execute("SELECT id FROM sources WHERE url = ?", (source.url,)).fetchone()[0]

    def set_source_status(self, source_url: str, success: bool, message: str = "") -> None:
        field = "last_success_at" if success else "last_error_at"
        with self.connect() as conn:
            conn.execute(
                f"UPDATE sources SET {field} = ?, last_error = ? WHERE url = ?",
                (utc_now(), None if success else message, source_url),
            )

    def article_exists(self, normalized_url: str, content_hash: str = "") -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM articles WHERE normalized_url = ? OR (? != '' AND content_hash = ?) LIMIT 1",
                (normalized_url, content_hash, content_hash),
            ).fetchone()
            return row is not None

    def upsert_article(self, article: IntelligenceArticle, normalized_url: str) -> Tuple[int, bool]:
        with self.connect() as conn:
            source_row = conn.execute("SELECT id FROM sources WHERE url = ?", (article.source_url,)).fetchone()
            source_id = source_row[0] if source_row else None
            existing = conn.execute(
                "SELECT id FROM articles WHERE normalized_url = ?",
                (normalized_url,),
            ).fetchone()
            payload = (
                source_id,
                article.title,
                article.url,
                normalized_url,
                article.source_name,
                article.source_url,
                article.author,
                article.language,
                article.topic,
                article.published_at,
                article.text,
                article.content_hash,
                article.image_url,
                json.dumps(article.keywords, ensure_ascii=False),
                json.dumps(article.people, ensure_ascii=False),
                json.dumps(article.organizations, ensure_ascii=False),
                json.dumps(article.locations, ensure_ascii=False),
                article.summary.tldr,
                json.dumps(article.summary.bullets, ensure_ascii=False),
                article.summary.full,
                article.summary.translated,
                article.relevance_score,
                article.quality_score,
                article.extraction_quality,
                utc_now(),
            )
            if existing:
                conn.execute(
                    """
                    UPDATE articles SET
                        source_id=?, title=?, url=?, normalized_url=?, source_name=?, source_url=?,
                        author=?, language=?, topic=?, published_at=?, text=?, content_hash=?,
                        image_url=?, keywords_json=?, people_json=?, organizations_json=?, locations_json=?,
                        summary_tldr=?, summary_bullets_json=?, summary_full=?, summary_translated=?,
                        relevance_score=?, quality_score=?, extraction_quality=?, updated_at=?
                    WHERE id=?
                    """,
                    payload + (existing["id"],),
                )
                return existing["id"], False
            cur = conn.execute(
                """
                INSERT INTO articles (
                    source_id, title, url, normalized_url, source_name, source_url,
                    author, language, topic, published_at, text, content_hash, image_url,
                    keywords_json, people_json, organizations_json, locations_json,
                    summary_tldr, summary_bullets_json, summary_full, summary_translated,
                    relevance_score, quality_score, extraction_quality, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                payload,
            )
            return cur.lastrowid, True

    def get_article(self, article_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
            return self._row_to_article(row) if row else None

    def list_articles(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if filters.get("topic"):
            clauses.append("topic = ?")
            params.append(filters["topic"])
        if filters.get("source"):
            clauses.append("source_name LIKE ?")
            params.append(f"%{filters['source']}%")
        if filters.get("language"):
            clauses.append("language = ?")
            params.append(filters["language"])
        if filters.get("q"):
            clauses.append("(title LIKE ? OR text LIKE ? OR keywords_json LIKE ?)")
            needle = f"%{filters['q']}%"
            params.extend([needle, needle, needle])
        if filters.get("min_quality") is not None:
            clauses.append("quality_score >= ?")
            params.append(float(filters["min_quality"]))
        if filters.get("created_after"):
            # created_at is SQLite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC), so it compares as text.
            clauses.append("created_at >= ?")
            params.append(filters["created_after"])
        sql = "SELECT * FROM articles"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(published_at, created_at) DESC, relevance_score DESC LIMIT ? OFFSET ?"
        params.extend([int(filters.get("limit", 50)), int(filters.get("offset", 0))])
        with self.connect() as conn:
            return [self._row_to_article(row) for row in conn.execute(sql, params).fetchall()]

    def stats(self) -> Dict[str, Any]:
        today = date.today().isoformat()
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            today_count = conn.execute("SELECT COUNT(*) FROM articles WHERE DATE(created_at) = ?", (today,)).fetchone()[0]
            errors = conn.execute("SELECT COUNT(*) FROM errors WHERE DATE(created_at) = ?", (today,)).fetchone()[0]
            topics = [dict(row) for row in conn.execute("SELECT topic, COUNT(*) count FROM articles GROUP BY topic ORDER BY count DESC").fetchall()]
            sources = [dict(row) for row in conn.execute("SELECT source_name, COUNT(*) count FROM articles GROUP BY source_name ORDER BY count DESC LIMIT 10").fetchall()]
            latest_log = conn.execute("SELECT created_at FROM processing_logs ORDER BY id DESC LIMIT 1").fetchone()
            return {
                "total_articles": total,
                "articles_today": today_count,
                "errors_today": errors,
                "topics": topics,
                "sources": sources,
                "last_pipeline_event": latest_log[0] if latest_log else None,
            }

    def log(self, stage: str, status: str, message: str = "", request_id: str = "", source_url: str = "", article_url: str = "", duration_ms: int = 0) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO processing_logs (request_id, stage, status, source_url, article_url, message, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (request_id, stage, status, source_url, article_url, message, duration_ms),
            )

    def error(self, stage: str, exc: Exception, request_id: str = "", source_url: str = "", article_url: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO errors (request_id, stage, source_url, article_url, error_type, message) VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, stage, source_url, article_url, type(exc).__name__, str(exc)),
            )

    def recent_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM processing_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def recent_errors(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def _row_to_article(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["keywords"] = json.loads(data.pop("keywords_json") or "[]")
        data["people"] = json.loads(data.pop("people_json") or "[]")
        data["organizations"] = json.loads(data.pop("organizations_json") or "[]")
        data["locations"] = json.loads(data.pop("locations_json") or "[]")
        data["summary"] = {
            "tldr": data.pop("summary_tldr") or "",
            "bullets": json.loads(data.pop("summary_bullets_json") or "[]"),
            "full": data.pop("summary_full") or "",
            "translated": data.pop("summary_translated"),
        }
        return data

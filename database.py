"""Compatibility helpers backed by the v3 SQLiteStore."""

from typing import Any, Dict, List, Optional

from news_platform.config import get_settings
from news_platform.models import Source
from news_platform.storage import SQLiteStore


def _store() -> SQLiteStore:
    return SQLiteStore(get_settings().data_dir)


def init_db():
    _store().init_schema()


def get_stats() -> Dict[str, Any]:
    stats = _store().stats()
    return {
        "total_bots": 0,
        "active_bots": 0,
        "fetched_today": stats["articles_today"],
        "returned_today": stats["articles_today"],
        "errors_today": stats["errors_today"],
        "total_posts": stats["total_articles"],
    }


def get_global_feeds(active_only: bool = False) -> List[Dict]:
    return _store().list_sources(active_only=active_only)


def add_global_feed(name: str, url: str, bypass_relevance: int = 0) -> int:
    return _store().upsert_source(Source(name=name, url=url, active=True, reliability=0.7))


def update_global_feed(feed_id: int, name: str = None, url: str = None, active: int = None, bypass_relevance: int = None):
    sources = _store().list_sources(active_only=False)
    match = next((source for source in sources if source["id"] == feed_id), None)
    if not match:
        return
    _store().upsert_source(
        Source(
            name=name or match["name"],
            url=url or match["url"],
            reliability=match.get("reliability", 0.7),
            active=bool(match["active"] if active is None else active),
            topic_hint=match.get("topic_hint") or "",
        )
    )


def delete_global_feed(feed_id: int):
    with _store().connect() as conn:
        conn.execute("UPDATE sources SET active = 0 WHERE id = ?", (feed_id,))


def get_activity(limit: int = 50) -> List[Dict]:
    return _store().recent_logs(limit)


def get_activity_chart(days: int = 7) -> List[Dict]:
    return []


def log_activity(city_name: str, source: str, fetched: int, returned: int, duration_ms: int, error: str = None):
    _store().log("legacy_activity", "error" if error else "success", message=f"{city_name}:{source} fetched={fetched} returned={returned}", duration_ms=duration_ms)


def log_post(*args, **kwargs):
    return None


def get_posts(bot_id: int = None, limit: int = 50) -> List[Dict]:
    return _store().list_articles({"limit": limit})


def get_bots() -> List[Dict]:
    return []


def get_bot(bot_id: int) -> Optional[Dict]:
    return None


def add_bot(config: Dict[str, Any]) -> int:
    return 0


def update_bot(bot_id: int, config: Dict[str, Any]):
    return None


def delete_bot(bot_id: int):
    return None


def get_setting(key: str, default: str = None) -> Optional[str]:
    return default


def set_setting(key: str, value: str):
    return None


def get_all_settings() -> Dict[str, str]:
    settings = get_settings()
    return {
        "SERVICE_PORT": str(settings.service_port),
        "DATA_DIR": settings.data_dir,
        "AUTH_ENABLED": str(bool(settings.api_key)),
    }

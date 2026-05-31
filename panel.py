"""
Panel Blueprint — management dashboard API + UI.
"""
import os
import time
from flask import Blueprint, jsonify, request, render_template, current_app

import database as db
from processor import NewsProcessor


def _build_bot_sources(bot: dict) -> list:
    """Global feeds + bot's own custom feeds."""
    sources = []
    for f in db.get_global_feeds(active_only=True):
        sources.append({"url": f["url"], "bypass_relevance": bool(f.get("bypass_relevance"))})
    for url in _parse_feeds(bot.get("custom_feeds", "")):
        sources.append({"url": url, "bypass_relevance": False})
    return sources


def _parse_feeds(raw: str) -> list:
    if not raw:
        return []
    return [u.strip() for u in raw.replace(",", "\n").splitlines() if u.strip()]

panel = Blueprint("panel", __name__, url_prefix="/panel")


# ── UI ────────────────────────────────────────────────────────────────────────

@panel.route("/")
def index():
    return render_template("panel.html")


# ── Stats ─────────────────────────────────────────────────────────────────────

@panel.route("/api/stats")
def stats():
    return jsonify(db.get_stats())


@panel.route("/api/activity/chart")
def activity_chart():
    days = int(request.args.get("days", 7))
    return jsonify(db.get_activity_chart(days))


# ── Activity ──────────────────────────────────────────────────────────────────

@panel.route("/api/activity")
def activity():
    limit = int(request.args.get("limit", 50))
    return jsonify(db.get_activity(limit))


# ── Bots ──────────────────────────────────────────────────────────────────────

@panel.route("/api/bots", methods=["GET"])
def list_bots():
    return jsonify(db.get_bots())


@panel.route("/api/bots", methods=["POST"])
def create_bot():
    data = request.get_json() or {}
    required = ["name", "city_name", "country_code", "bot_token", "telegram_chat_id"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400
    bot_id = db.add_bot(data)
    return jsonify({"id": bot_id, "message": "Bot created"}), 201


@panel.route("/api/bots/<int:bot_id>", methods=["GET"])
def get_bot(bot_id):
    bot = db.get_bot(bot_id)
    if not bot:
        return jsonify({"error": "Not found"}), 404
    return jsonify(bot)


@panel.route("/api/bots/<int:bot_id>", methods=["PUT"])
def update_bot(bot_id):
    if not db.get_bot(bot_id):
        return jsonify({"error": "Not found"}), 404
    db.update_bot(bot_id, request.get_json() or {})
    return jsonify({"message": "Updated"})


@panel.route("/api/bots/<int:bot_id>", methods=["DELETE"])
def delete_bot(bot_id):
    if not db.get_bot(bot_id):
        return jsonify({"error": "Not found"}), 404
    db.delete_bot(bot_id)
    return jsonify({"message": "Deleted"})


@panel.route("/api/bots/<int:bot_id>/run", methods=["POST"])
def run_bot(bot_id):
    bot = db.get_bot(bot_id)
    if not bot:
        return jsonify({"error": "Not found"}), 404
    if not bot.get("active"):
        return jsonify({"error": "Bot is inactive"}), 400

    config = {
        "bot_id": str(bot_id),
        "bot_token": bot["bot_token"],
        "telegram_chat_id": bot["telegram_chat_id"],
        "city_name": bot["city_name"],
        "country_code": bot["country_code"],
        "news_language": bot.get("news_language", "en"),
        "openai_api_key": bot.get("openai_api_key"),
        "max_posts_per_run": bot.get("max_posts_per_run", 5),
        "sources": _build_bot_sources(bot),
    }

    start = time.time()
    try:
        processor = NewsProcessor(config)
        result = processor.process_news()
        duration_ms = int((time.time() - start) * 1000)

        db.log_activity(
            city_name=bot["city_name"],
            source=f"bot:{bot_id}",
            fetched=result.processed,
            returned=result.posted,
            duration_ms=duration_ms,
            error=result.error_message,
        )
        for article in result.articles:
            db.log_post(
                bot_id=bot_id,
                title=article.get("title", ""),
                url=article.get("url", ""),
                summary_en=article.get("summary_en", ""),
                summary_fa=article.get("summary_fa", ""),
                sentiment=article.get("sentiment"),
                relevance_score=article.get("relevance_score"),
            )

        return jsonify({
            "status": result.status,
            "fetched": result.processed,
            "posted": result.posted,
            "duration_ms": duration_ms,
            "error": result.error_message,
        })
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        db.log_activity(bot["city_name"], f"bot:{bot_id}", 0, 0, duration_ms, str(e))
        return jsonify({"error": str(e)}), 500


@panel.route("/api/bots/<int:bot_id>/test", methods=["POST"])
def test_bot(bot_id):
    bot = db.get_bot(bot_id)
    if not bot:
        return jsonify({"error": "Not found"}), 404
    config = {
        "bot_id": str(bot_id),
        "bot_token": bot["bot_token"],
        "telegram_chat_id": bot["telegram_chat_id"],
        "city_name": bot["city_name"],
        "country_code": bot["country_code"],
        "news_language": bot.get("news_language", "en"),
    }
    try:
        processor = NewsProcessor(config)
        ok = processor.test_connection()
        return jsonify({"connected": ok})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)})


@panel.route("/api/bots/<int:bot_id>/posts")
def bot_posts(bot_id):
    if not db.get_bot(bot_id):
        return jsonify({"error": "Not found"}), 404
    limit = int(request.args.get("limit", 50))
    return jsonify(db.get_posts(bot_id=bot_id, limit=limit))


# ── Posts ─────────────────────────────────────────────────────────────────────

@panel.route("/api/posts")
def all_posts():
    limit = int(request.args.get("limit", 50))
    return jsonify(db.get_posts(limit=limit))


# ── Global Feeds ─────────────────────────────────────────────────────────────

@panel.route("/api/feeds", methods=["GET"])
def list_feeds():
    return jsonify(db.get_global_feeds())


@panel.route("/api/feeds", methods=["POST"])
def create_feed():
    data = request.get_json() or {}
    if not data.get("url"):
        return jsonify({"error": "url is required"}), 400
    name = data.get("name") or data["url"]
    feed_id = db.add_global_feed(name, data["url"], int(data.get("bypass_relevance", 0)))
    return jsonify({"id": feed_id, "message": "Feed added"}), 201


@panel.route("/api/feeds/<int:feed_id>", methods=["PUT"])
def update_feed(feed_id):
    data = request.get_json() or {}
    br = data.get("bypass_relevance")
    db.update_global_feed(feed_id, name=data.get("name"), url=data.get("url"),
                          active=data.get("active"),
                          bypass_relevance=int(br) if br is not None else None)
    return jsonify({"message": "Updated"})


@panel.route("/api/feeds/<int:feed_id>", methods=["DELETE"])
def delete_feed(feed_id):
    db.delete_global_feed(feed_id)
    return jsonify({"message": "Deleted"})


# ── Settings ──────────────────────────────────────────────────────────────────

@panel.route("/api/settings/app", methods=["GET"])
def get_app_settings():
    return jsonify(db.get_all_settings())


@panel.route("/api/settings/app", methods=["POST"])
def save_app_settings():
    data = request.get_json() or {}
    for key, value in data.items():
        db.set_setting(key, str(value))
    return jsonify({"message": "Saved"})


@panel.route("/api/settings")
def settings():
    return jsonify({
        "LIBRETRANSLATE_URL": os.getenv("LIBRETRANSLATE_URL", ""),
        "OLLAMA_BASE_URL": os.getenv("OLLAMA_BASE_URL", ""),
        "OLLAMA_MODEL": os.getenv("OLLAMA_MODEL", ""),
        "SCHEDULE_INTERVAL_MINUTES": os.getenv("SCHEDULE_INTERVAL_MINUTES", "0"),
        "DATA_DIR": os.getenv("DATA_DIR", "./data"),
        "SERVICE_PORT": os.getenv("SERVICE_PORT", "8003"),
        "BOT_MANAGER_URL": os.getenv("BOT_MANAGER_URL", ""),
    })

"""
Panel Blueprint — management dashboard API + UI.
"""
import os
import time
from flask import Blueprint, jsonify, request, render_template, current_app

import database as db
from processor import NewsProcessor

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
        "newsapi_key": bot.get("newsapi_key"),
        "max_posts_per_run": bot.get("max_posts_per_run", 5),
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


# ── Settings ──────────────────────────────────────────────────────────────────

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

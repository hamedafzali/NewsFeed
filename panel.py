"""
Panel Blueprint — management dashboard API + UI.
"""
import os
import time
from flask import Blueprint, jsonify, request, render_template, current_app

import database as db
from processor import NewsProcessor, run_bot as run_bot_pipeline

panel = Blueprint("panel", __name__, url_prefix="/panel")


# ── UI ────────────────────────────────────────────────────────────────────────

@panel.route("/")
def index():
    resp = render_template("panel.html")
    from flask import make_response
    r = make_response(resp)
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return r


@panel.route("/bot/<int:bot_id>")
def bot_page(bot_id):
    from flask import make_response
    if not db.get_bot(bot_id):
        return make_response("Bot not found", 404)
    r = make_response(render_template("bot.html", bot_id=bot_id))
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return r


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
    if bot.get("bot_token"):
        bot["bot_token_masked"] = "****" + bot["bot_token"][-4:]
        del bot["bot_token"]
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

    start = time.time()
    try:
        result = run_bot_pipeline(bot)
        return jsonify({
            "status": result.status,
            "fetched": result.processed,
            "posted": result.posted,
            "duration_ms": int((time.time() - start) * 1000),
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
        if not processor.publisher:
            return jsonify({"connected": False, "error": "Missing bot token or chat id"})
        if not processor.publisher.test_token():
            return jsonify({"connected": False, "error": "Invalid bot token"})
        chat = processor.publisher.test_chat()
        if not chat.ok:
            return jsonify({"connected": False, "error": f"Chat check failed: {chat.error}"})
        return jsonify({"connected": True})
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
        "AI_POST_WRITER": "Mistral" if os.getenv("MISTRAL_API_KEY") else "off (set MISTRAL_API_KEY)",
        "MISTRAL_MODEL": os.getenv("MISTRAL_MODEL", "") or ("auto" if os.getenv("MISTRAL_API_KEY") else ""),
        "MAX_ARTICLE_AGE_HOURS": os.getenv("MAX_ARTICLE_AGE_HOURS", "36"),
        "SCHEDULE_INTERVAL_MINUTES": os.getenv("SCHEDULE_INTERVAL_MINUTES", "0"),
        "DATA_DIR": os.getenv("DATA_DIR", "./data"),
        "SERVICE_PORT": os.getenv("SERVICE_PORT", "8003"),
        "BOT_MANAGER_URL": os.getenv("BOT_MANAGER_URL", ""),
    })


# ── Bot page: reports and run traces ─────────────────────────────────────────

def _public_bot(bot):
    out = {k: v for k, v in bot.items() if k != "bot_token"}
    if bot.get("bot_token"):
        out["bot_token_masked"] = "****" + bot["bot_token"][-4:]
    return out


@panel.route("/api/bots/<int:bot_id>/report")
def bot_report(bot_id):
    bot = db.get_bot(bot_id)
    if not bot:
        return jsonify({"error": "Not found"}), 404
    days = min(max(int(request.args.get("days", 7)), 1), 365)
    channel = None
    if bot.get("bot_token") and bot.get("telegram_chat_id"):
        from news_platform.publishing.telegram import TelegramPublisher
        channel = TelegramPublisher(bot["bot_token"], str(bot["telegram_chat_id"])).chat_username()
    return jsonify({"bot": _public_bot(bot), "channel": channel, "report": db.bot_report(bot_id, days),
                    "posts": db.bot_post_list(bot_id, int(request.args.get("posts", 30)))})


@panel.route("/api/bots/<int:bot_id>/runs")
def bot_runs(bot_id):
    if not db.get_bot(bot_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify(db.get_runs(bot_id, min(int(request.args.get("limit", 40)), 200)))


@panel.route("/api/runs/<int:run_id>")
def run_detail(run_id):
    run = db.get_run(run_id)
    if not run:
        return jsonify({"error": "Not found"}), 404
    return jsonify(run)

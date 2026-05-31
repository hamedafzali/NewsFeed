"""
News Feed Service - Main Application
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps

import time

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request

import database as db
from panel import panel as panel_blueprint
from service import NewsFeedService


def _build_sources(body: dict) -> list:
    """Build the sources list from global feeds only. No hardcoded sources."""
    city = body.get("city_name", "")
    language = body.get("news_language", "en")
    country = body.get("country_code", "")

    sources = []
    for f in db.get_global_feeds(active_only=True):
        sources.append({
            "url": f["url"],
            "bypass_relevance": bool(f.get("bypass_relevance")),
        })

    # Per-request extra feeds (passed directly in the request body)
    for url in body.get("custom_feeds", []):
        sources.append({"url": url, "bypass_relevance": False})

    return sources

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _require_api_key(f):
    """Reject requests that don't carry the correct X-API-Key header.
    Auth is skipped entirely when API_KEY env var is not set (dev mode)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        expected = os.getenv("API_KEY", "")
        if expected:
            provided = request.headers.get("X-API-Key", "")
            if provided != expected:
                return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def _build_sources(body: dict) -> list:
    """Build ordered source list from global feeds + any per-request overrides."""
    sources = []
    # Global feeds from DB (ordered, active only)
    for f in db.get_global_feeds(active_only=True):
        sources.append({"url": f["url"], "bypass_relevance": bool(f["bypass_relevance"])})
    # Per-request custom feeds (always bypass relevance since caller chose them explicitly)
    for url in body.get("custom_feeds", []):
        if url not in {s["url"] for s in sources}:
            sources.append({"url": url, "bypass_relevance": False})
    return sources


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "news-feed-service-secret-key")

    db.init_db()
    app.register_blueprint(panel_blueprint)

    bot_manager_url = os.getenv("BOT_MANAGER_URL", "http://localhost:5002")
    service_id = os.getenv("SERVICE_ID", "news-feed-1")
    service = NewsFeedService(bot_manager_url, service_id)

    # --- Background scheduler ---
    schedule_interval = int(os.getenv("SCHEDULE_INTERVAL_MINUTES", 0))
    scheduler = None
    if schedule_interval > 0:
        scheduler = BackgroundScheduler()

        def _scheduled_run():
            bots = service.get_active_bots()
            bot_ids = [b["id"] for b in bots]
            if not bot_ids:
                return
            with ThreadPoolExecutor(max_workers=min(len(bot_ids), 5)) as pool:
                list(pool.map(service.process_bot_news, bot_ids))

        scheduler.add_job(_scheduled_run, "interval", minutes=schedule_interval,
                          id="auto_process", replace_existing=True)
        scheduler.start()
        logging.getLogger(__name__).info(
            f"Scheduler started: processing all bots every {schedule_interval} minutes"
        )

    # --- Error handlers ---
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(500)
    def internal_error(error):
        return jsonify({"error": "Internal server error"}), 500

    # --- Health & info ---
    @app.route("/health")
    def health_check():
        return jsonify({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "service": "news-feed-service",
            "service_id": service_id,
            "bot_manager_connected": service.ping_manager(),
            "scheduler_active": scheduler is not None and scheduler.running,
            "version": "2.0.0",
        })

    @app.route("/api/service/info")
    def service_info():
        return jsonify({
            "service_id": service_id,
            "service_type": "news_processor",
            "bot_manager_url": bot_manager_url,
            "timestamp": datetime.utcnow().isoformat(),
            "version": "2.0.0",
        })

    @app.route("/api/service/ping", methods=["POST"])
    def ping_service():
        return jsonify({
            "status": "alive",
            "timestamp": datetime.utcnow().isoformat(),
            "service_id": service_id,
        })

    # --- Stats dashboard (JSON) ---
    @app.route("/api/stats")
    @_require_api_key
    def all_stats():
        """Aggregate stats across all active bots."""
        bots = service.get_active_bots()
        stats = []
        for bot in bots:
            bot_stats = service.get_bot_stats(bot["id"])
            bot_stats["bot_id"] = bot["id"]
            stats.append(bot_stats)
        totals = {
            "total_articles": sum(s.get("total_articles", 0) for s in stats),
            "posted_articles": sum(s.get("posted_articles", 0) for s in stats),
            "unposted_articles": sum(s.get("unposted_articles", 0) for s in stats),
        }
        return jsonify({"bots": stats, "totals": totals})

    # --- Standalone news fetch (no Telegram, no Bot Manager) ---
    @app.route("/api/news", methods=["POST"])
    @_require_api_key
    def fetch_news():
        """Fetch, filter, and summarize news. Returns articles as JSON — no Telegram involved.

        Body (JSON):
          city_name       string  required  e.g. "Tehran"
          country_code    string  required  e.g. "IR"
          news_language   string  required  e.g. "en"
          openai_api_key  string  optional  enables AI summaries + Persian translation
          newsapi_key     string  optional  adds NewsAPI as a second source
          max_results     int     optional  max articles to return (default 5)
        """
        body = request.get_json() or {}
        required = ["city_name", "country_code", "news_language"]
        missing = [f for f in required if not body.get(f)]
        if missing:
            return jsonify({"error": f"Missing required fields: {missing}"}), 400

        max_results = int(body.get("max_results", 5))

        # Build a minimal config — no Telegram fields needed
        config = {
            "bot_id": "api-fetch",
            "bot_token": "unused",
            "telegram_chat_id": "unused",
            "city_name": body["city_name"],
            "country_code": body["country_code"],
            "news_language": body["news_language"],
            "openai_api_key": body.get("openai_api_key"),
            "ollama_base_url": body.get("ollama_base_url"),
            "model": body.get("model"),  # None → processor picks model based on backend
            "newsapi_key": body.get("newsapi_key"),
            "libretranslate_url": body.get("libretranslate_url") or os.getenv("LIBRETRANSLATE_URL"),
            "sources": _build_sources(body),
            "max_posts_per_run": max_results,
        }
        finbert_url = body.get("finbert_url")

        from processor import NewsProcessor
        try:
            t0 = time.time()
            processor = NewsProcessor(config)
            raw_articles = processor._fetch_articles()

            def process_one(article):
                content, quality = processor._extract_content_with_quality(article)
                if article.get("_bypass_relevance"):
                    score = 1.0
                else:
                    score = processor._calculate_relevance(article["title"], content)
                    if score < 0.3:
                        return None
                summaries = processor._summarize_and_translate(
                    content, article["title"], article.get("source", ""), quality,
                    source_lang=article.get("feed_language", "en"),
                )
                return {
                    "title": article["title"],
                    "url": article["url"],
                    "source": article.get("source"),
                    "published_at": article.get("published_at"),
                    "relevance_score": round(score, 2),
                    "content_quality": quality,
                    "summary_en": summaries["summary_en"] if summaries else None,
                    "summary_fa": summaries["summary_fa"] if summaries else None,
                    "sentiment": None,
                    "sentiment_score": None,
                }

            results = []
            candidates = raw_articles[:max_results * 4]  # check 4× candidates in parallel
            with ThreadPoolExecutor(max_workers=min(len(candidates), 4)) as pool:
                for item in pool.map(process_one, candidates):
                    if item is not None:
                        results.append(item)
                    if len(results) >= max_results:
                        break

            # Batch sentiment via FinBERT — single call for all articles
            if finbert_url and results:
                titles = [a["title"] for a in results]
                sentiment_map = processor.get_sentiment(titles, finbert_url)
                for article in results:
                    key = article["title"][:120]
                    if key in sentiment_map:
                        article["sentiment"] = sentiment_map[key]["label"]
                        article["sentiment_score"] = sentiment_map[key]["score"]

            duration_ms = int((time.time() - t0) * 1000)
            db.log_activity(body["city_name"], "api", len(raw_articles), len(results), duration_ms)
            return jsonify({
                "city": body["city_name"],
                "fetched": len(raw_articles),
                "returned": len(results),
                "articles": results,
            })
        except Exception as e:
            db.log_activity(body.get("city_name", "?"), "api", 0, 0, 0, str(e))
            return jsonify({"error": str(e)}), 500

    # --- Direct processing (no Bot Manager needed) ---
    @app.route("/api/process/direct", methods=["POST"])
    @_require_api_key
    def process_direct():
        """Process news using config supplied in the request body.
        Useful for testing without a Bot Manager.

        Required fields: bot_token, telegram_chat_id, city_name, country_code, news_language
        Optional fields: openai_api_key, newsapi_key, max_posts_per_run, bot_id
        """
        config = request.get_json()
        if not config:
            return jsonify({"error": "Request body must be JSON"}), 400

        required = ["bot_token", "telegram_chat_id", "city_name", "country_code", "news_language"]
        missing = [f for f in required if not config.get(f)]
        if missing:
            return jsonify({"error": f"Missing required fields: {missing}"}), 400

        config.setdefault("bot_id", "direct-test")
        config.setdefault("max_posts_per_run", 3)
        dry_run = config.pop("dry_run", False)

        from processor import NewsProcessor
        try:
            processor = NewsProcessor(config)

            if dry_run:
                # Fetch + filter + summarize, skip Telegram posting
                articles = processor._fetch_articles()
                output = []
                for article in articles[:config["max_posts_per_run"]]:
                    if processor._article_exists(article["url"]):
                        continue
                    content = processor._extract_content(article["url"])
                    if not content or len(content) < 200:
                        continue
                    score = processor._calculate_relevance(article["title"], content)
                    if score < 0.3:
                        continue
                    summaries = processor._summarize_and_translate(content)
                    output.append({
                        "title": article["title"],
                        "url": article["url"],
                        "source": article.get("source"),
                        "relevance_score": round(score, 2),
                        "summary_en": summaries["summary_en"] if summaries else None,
                        "summary_fa": summaries["summary_fa"] if summaries else None,
                    })
                return jsonify({
                    "dry_run": True,
                    "city": config["city_name"],
                    "fetched": len(articles),
                    "would_post": len(output),
                    "articles": output,
                })

            result = processor.process_news()
            return jsonify({
                "bot_id": config["bot_id"],
                "city": config["city_name"],
                "processed": result.processed,
                "posted": result.posted,
                "duration": result.duration,
                "status": result.status,
                "error_message": result.error_message,
                "articles": [
                    {"title": a.get("title"), "url": a.get("url"), "source": a.get("source")}
                    for a in result.articles
                ],
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Bot processing ---
    @app.route("/api/process/<bot_id>", methods=["POST"])
    @_require_api_key
    def process_bot_news(bot_id):
        result = service.process_bot_news(bot_id)
        if "error" in result:
            return jsonify(result), 500
        return jsonify(result)

    @app.route("/api/test/<bot_id>", methods=["POST"])
    @_require_api_key
    def test_bot_connection(bot_id):
        success = service.test_bot_connection(bot_id)
        if success:
            return jsonify({"status": "connected", "message": "Connection successful"})
        return jsonify({"status": "failed", "message": "Connection failed"}), 500

    @app.route("/api/stats/<bot_id>")
    @_require_api_key
    def get_bot_stats(bot_id):
        stats = service.get_bot_stats(bot_id)
        if "error" in stats:
            return jsonify(stats), 404
        return jsonify(stats)

    @app.route("/api/bots")
    @_require_api_key
    def get_active_bots():
        return jsonify(service.get_active_bots())

    @app.route("/api/process/batch", methods=["POST"])
    @_require_api_key
    def process_batch():
        """Process multiple bots concurrently."""
        data = request.get_json() or {}
        bot_ids = data.get("bot_ids", [])

        if not bot_ids:
            bots = service.get_active_bots()
            bot_ids = [b["id"] for b in bots]

        results = []
        max_workers = min(len(bot_ids), int(os.getenv("BATCH_MAX_WORKERS", 5)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(service.process_bot_news, bid): bid for bid in bot_ids}
            for future in as_completed(futures):
                results.append(future.result())

        return jsonify({"processed": len(results), "results": results})

    return app


if __name__ == "__main__":
    port = int(os.getenv("SERVICE_PORT", 5003))
    app = create_app()
    print(f"News Feed Service starting on port {port}")
    print(f"Bot Manager: {os.getenv('BOT_MANAGER_URL', 'http://localhost:5002')}")
    print(f"Service ID: {os.getenv('SERVICE_ID', 'news-feed-1')}")
    print(f"Auth: {'enabled' if os.getenv('API_KEY') else 'disabled (set API_KEY to enable)'}")
    app.run(debug=os.getenv("DEBUG", "false").lower() == "true", host="0.0.0.0", port=port)

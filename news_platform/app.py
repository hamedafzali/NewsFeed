import os
import uuid

from flask import Flask, g, jsonify, redirect, render_template, request

from news_platform.api.v1 import create_api_blueprint, envelope
from news_platform.config import get_settings
from news_platform.pipeline import NewsPipeline
from news_platform.storage import SQLiteStore
from news_platform.utils.logging import configure_logging


def create_app() -> Flask:
    configure_logging()
    settings = get_settings()
    app = Flask(__name__, template_folder="../templates")
    app.config["API_KEY"] = settings.api_key
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "news-platform-dev-secret")

    store = SQLiteStore(settings.data_dir)
    pipeline = NewsPipeline(settings, store)
    app.extensions["settings"] = settings
    app.extensions["store"] = store
    app.extensions["pipeline"] = pipeline

    app.register_blueprint(create_api_blueprint())

    @app.before_request
    def request_context():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    @app.get("/")
    def root():
        return redirect("/dashboard")

    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html", auth_enabled=bool(settings.api_key))

    @app.get("/health")
    def legacy_health():
        return jsonify({"status": "healthy", "service": settings.service_name, "version": settings.service_version})

    @app.get("/ready")
    def legacy_ready():
        return jsonify({"status": "ready", "database": "ok"})

    @app.post("/api/news")
    def legacy_news():
        body = request.get_json(silent=True) or {}
        query = body.get("query") or body.get("city_name") or ""
        result = pipeline.refresh(
            query=query,
            language=body.get("news_language", body.get("language", "en")),
            country=body.get("country_code", body.get("country", "US")),
            limit=int(body.get("max_results", body.get("limit", 10))),
            skip_web=bool(body.get("skip_scraping", True)),
            translate=bool(body.get("translate", False)),
            request_id=g.request_id,
        )
        return jsonify({
            "city": query,
            "fetched": result.fetched,
            "returned": len(result.articles),
            "articles": [article.to_dict() for article in result.articles],
        })

    @app.post("/api/process/direct")
    def legacy_process_direct():
        body = request.get_json(silent=True) or {}
        result = pipeline.refresh(
            query=body.get("city_name", body.get("query", "")),
            language=body.get("news_language", "en"),
            country=body.get("country_code", "US"),
            limit=int(body.get("max_posts_per_run", 10)),
            skip_web=bool(body.get("skip_scraping", True)),
            translate=bool(body.get("translate", False)),
            request_id=g.request_id,
        )
        return jsonify({
            "status": "success",
            "processed": result.processed,
            "posted": 0,
            "dry_run": True,
            "articles": [article.to_dict() for article in result.articles],
        })

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/v1"):
            return envelope({"error": "Not found"}, status="error", status_code=404)
        return jsonify({"error": "Not found"}), 404

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("SERVICE_PORT", "8003"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("DEBUG", "false").lower() == "true")

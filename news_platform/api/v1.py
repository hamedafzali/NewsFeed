import uuid
from functools import wraps
from typing import Any, Callable, Dict

from flask import Blueprint, Response, current_app, g, jsonify, request

from news_platform.models import Source


class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def envelope(data: Any = None, meta: Dict[str, Any] | None = None, status: str = "success", status_code: int = 200) -> Response:
    payload = {"status": status, "data": data if data is not None else {}, "meta": meta or {}}
    if getattr(g, "request_id", None):
        payload["meta"]["request_id"] = g.request_id
    return jsonify(payload), status_code


def require_api_key(fn: Callable):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config.get("API_KEY", "")
        if expected:
            provided = request.headers.get("X-API-Key", "")
            if provided != expected:
                raise ApiError("Unauthorized", 401)
        return fn(*args, **kwargs)

    return wrapper


def create_api_blueprint() -> Blueprint:
    bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

    @bp.before_request
    def attach_request_id():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    @bp.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return envelope({"error": error.message, "details": error.details}, status="error", status_code=error.status_code)

    @bp.errorhandler(Exception)
    def handle_unexpected(error: Exception):
        current_app.extensions["store"].error("api", error, request_id=getattr(g, "request_id", ""))
        return envelope({"error": "Internal server error"}, status="error", status_code=500)

    @bp.get("/health")
    @require_api_key
    def health():
        settings = current_app.extensions["settings"]
        return envelope({"service": settings.service_name, "version": settings.service_version, "status": "healthy"})

    @bp.get("/ready")
    @require_api_key
    def ready():
        store = current_app.extensions["store"]
        stats = store.stats()
        return envelope({"ready": True, "database": "ok", "stats": stats})

    @bp.get("/articles")
    @require_api_key
    def articles():
        filters = {
            "topic": request.args.get("topic"),
            "source": request.args.get("source"),
            "language": request.args.get("language"),
            "q": request.args.get("q"),
            "min_quality": request.args.get("min_quality"),
            "limit": request.args.get("limit", 50),
            "offset": request.args.get("offset", 0),
        }
        return envelope(current_app.extensions["store"].list_articles(filters), meta={"filters": {k: v for k, v in filters.items() if v not in (None, "")}})

    @bp.get("/articles/<int:article_id>")
    @require_api_key
    def article(article_id: int):
        item = current_app.extensions["store"].get_article(article_id)
        if not item:
            raise ApiError("Article not found", 404)
        return envelope(item)

    @bp.post("/refresh")
    @require_api_key
    def refresh():
        body = request.get_json(silent=True) or {}
        query = str(body.get("query") or body.get("city_name") or "")
        language = str(body.get("language") or body.get("news_language") or "en")
        country = str(body.get("country") or body.get("country_code") or "US")
        limit = _int(body.get("limit") or body.get("max_results") or 25, "limit", 1, 200)
        skip_web = bool(body.get("skip_web", body.get("skip_scraping", True)))
        translate = bool(body.get("translate", False))
        result = current_app.extensions["pipeline"].refresh(
            query=query,
            language=language,
            country=country,
            limit=limit,
            skip_web=skip_web,
            translate=translate,
            request_id=g.request_id,
        )
        return envelope(result.to_dict(), meta={"query": query, "language": language, "country": country})

    @bp.post("/process")
    @require_api_key
    def process():
        body = request.get_json(silent=True) or {}
        if body.get("article_id"):
            return envelope(current_app.extensions["pipeline"].process_article(int(body["article_id"]), request_id=g.request_id))
        return refresh()

    @bp.get("/stats")
    @require_api_key
    def stats():
        store = current_app.extensions["store"]
        return envelope({
            "stats": store.stats(),
            "logs": store.recent_logs(25),
            "errors": store.recent_errors(25),
        })

    @bp.get("/sources")
    @require_api_key
    def sources():
        return envelope(current_app.extensions["store"].list_sources(active_only=False))

    @bp.post("/sources")
    @require_api_key
    def add_source():
        body = request.get_json(silent=True) or {}
        if not body.get("url"):
            raise ApiError("url is required", 400)
        source = Source(
            url=body["url"],
            name=body.get("name", body["url"]),
            reliability=float(body.get("reliability", 0.7)),
            active=bool(body.get("active", True)),
            topic_hint=body.get("topic_hint", ""),
        )
        source_id = current_app.extensions["store"].upsert_source(source)
        return envelope({"id": source_id}, status_code=201)

    return bp


def _int(value: Any, field: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception as exc:
        raise ApiError(f"{field} must be an integer", 400) from exc
    if parsed < minimum or parsed > maximum:
        raise ApiError(f"{field} must be between {minimum} and {maximum}", 400)
    return parsed

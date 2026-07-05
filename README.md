# News Intelligence Platform

A production-oriented Flask news engine that ingests RSS feeds, extracts clean article text, deduplicates, ranks, summarizes, stores, and exposes articles through a secure versioned API plus a dashboard.

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Service and dashboard run on:

- Dashboard: `http://localhost:8003/dashboard`
- Health: `http://localhost:8003/health`
- API health: `http://localhost:8003/api/v1/health`

If `API_KEY` is set, all `/api/v1/*` endpoints require `X-API-Key`.

## API

All v1 API responses use:

```json
{
  "status": "success",
  "data": {},
  "meta": {}
}
```

### Endpoints

- `GET /api/v1/health`
- `GET /api/v1/ready`
- `GET /api/v1/articles`
- `GET /api/v1/articles/{id}`
- `POST /api/v1/refresh`
- `POST /api/v1/process`
- `GET /api/v1/stats`
- `GET /api/v1/sources`
- `POST /api/v1/sources`

### Refresh Example

```bash
curl -X POST http://localhost:8003/api/v1/refresh \
  -H "Content-Type: application/json" \
  -d '{"query":"Tübingen","language":"de","country":"DE","limit":10}'
```

## Architecture

```text
news_platform/
├── api/          # Versioned API, auth, error envelope
├── pipeline/     # Ingestion, extraction, enrichment, ranking, orchestration
├── storage/      # SQLite implementation with PostgreSQL-ready table boundaries
├── utils/        # Logging and URL normalization
├── app.py        # Flask app factory
└── models.py     # Typed pipeline data contracts
```

## Storage

SQLite tables:

- `articles`
- `sources`
- `topics`
- `processing_logs`
- `errors`

Indexes cover source/date, topic/date, language, quality, and content hash.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
python app.py
```

## Legacy Compatibility

The old endpoints still exist for transitional callers:

- `POST /api/news`
- `POST /api/process/direct`

New integrations should use `/api/v1/*`.

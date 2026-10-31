# News Intelligence Platform

A Flask news engine that ingests RSS feeds, extracts clean article text, deduplicates, ranks, summarizes, translates (en/de/fa via LibreTranslate + MyMemory), and **posts articles to Telegram channels**. Channels ("bots") are managed in a web panel; a background scheduler runs all active channels automatically.

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Service runs on:

- **Management panel**: `http://localhost:8003/panel/` — add channels, feeds, run/test bots, see posting history
- Article dashboard: `http://localhost:8003/dashboard`
- Health: `http://localhost:8003/health`
- API health: `http://localhost:8003/api/v1/health`

If `API_KEY` is set, all `/api/v1/*` endpoints require `X-API-Key`. (`API_KEY` is *not* the Telegram bot token — bot tokens are configured per-channel in the panel.)

## Feeding a Telegram channel

1. Create a bot with @BotFather, copy its token.
2. Add the bot as **administrator** to your channel (with "Post messages" permission).
3. Open the panel → Add Bot: set city/country, source language (feeds), channel language (fa/en/de), token, and the channel `@username` or `-100…` chat id.
4. Click **Test** (verifies token *and* channel access), then **Run**.
5. Set `SCHEDULE_INTERVAL_MINUTES` (default 30) to post automatically; already-posted articles are never re-sent per channel.

Each run takes items from every active feed in turn, keeps only articles published within `MAX_ARTICLE_AGE_HOURS` (default 36), drops Google News redirects, videos and live tickers, ranks newest-first, and posts one article per story (titles overlapping an earlier pick or a recent post are skipped).

### Post writing (optional Mistral)

Set `MISTRAL_API_KEY` in the deployment environment (e.g. the container manager's project variables) to have Mistral write each post: a short headline and 2–3 sentence summary in the channel language, from the full article text. `MISTRAL_MODEL` pins a model; empty picks the best one the key can use and skips models that are out of quota. Without a key, or if Mistral fails, posts fall back to translating the RSS teaser with LibreTranslate/MyMemory. API keys are never entered in the panel.

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

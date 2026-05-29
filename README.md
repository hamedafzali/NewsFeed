# NewsFeed

A REST microservice that fetches, filters, and summarizes city-specific news using AI. Supports posting to Telegram channels or returning results directly via API.

## What it does

1. Fetches recent news for a city from Google News RSS and NewsAPI
2. Filters articles by relevance using LLM scoring (or keyword fallback)
3. Summarizes each article and translates to Persian
4. Posts to a Telegram channel — or returns results as JSON

## Quick Start

```bash
docker compose up -d
```

Service runs on `http://localhost:5003`.

## API

### Get news as JSON (no Telegram required)

```bash
curl -X POST http://localhost:5003/api/news \
  -H "Content-Type: application/json" \
  -d '{
    "city_name": "Tehran",
    "country_code": "IR",
    "news_language": "en",
    "max_results": 5
  }'
```

**Response:**
```json
{
  "city": "Tehran",
  "fetched": 20,
  "returned": 3,
  "articles": [
    {
      "title": "...",
      "url": "...",
      "source": "BBC",
      "published_at": "...",
      "relevance_score": 0.85,
      "summary_en": null,
      "summary_fa": null
    }
  ]
}
```

### With AI summaries (OpenAI)

```bash
curl -X POST http://localhost:5003/api/news \
  -H "Content-Type: application/json" \
  -d '{
    "city_name": "Tehran",
    "country_code": "IR",
    "news_language": "en",
    "openai_api_key": "sk-...",
    "max_results": 5
  }'
```

### With AI summaries (Ollama — local, free)

```bash
# First: install and run Ollama on your machine
brew install ollama && ollama pull llama3.2 && ollama serve

# Then call the API
curl -X POST http://localhost:5003/api/news \
  -H "Content-Type: application/json" \
  -d '{
    "city_name": "Tehran",
    "country_code": "IR",
    "news_language": "en",
    "ollama_base_url": "http://host.docker.internal:11434/v1",
    "model": "llama3.2",
    "max_results": 5
  }'
```

### Post to Telegram

```bash
curl -X POST http://localhost:5003/api/process/direct \
  -H "Content-Type: application/json" \
  -d '{
    "bot_token": "YOUR_BOT_TOKEN",
    "telegram_chat_id": "YOUR_CHAT_ID",
    "city_name": "Tehran",
    "country_code": "IR",
    "news_language": "en",
    "openai_api_key": "sk-...",
    "max_posts_per_run": 3
  }'
```

### Health check

```bash
curl http://localhost:5003/health
```

## Request parameters for `/api/news`

| Parameter | Required | Description |
|---|---|---|
| `city_name` | Yes | City to fetch news for (e.g. `"Tehran"`) |
| `country_code` | Yes | ISO country code (e.g. `"IR"`, `"DE"`) |
| `news_language` | Yes | Language code (e.g. `"en"`, `"fa"`) |
| `max_results` | No | Max articles to return (default: `5`) |
| `openai_api_key` | No | Enables AI summaries via OpenAI |
| `ollama_base_url` | No | Use a local Ollama instance instead of OpenAI |
| `model` | No | Model name (default: `gpt-4o-mini` or your Ollama model) |
| `newsapi_key` | No | Adds NewsAPI as a second news source |

## Configuration (docker-compose.yml)

| Variable | Default | Description |
|---|---|---|
| `BOT_MANAGER_URL` | `http://localhost:5002` | Bot Manager service URL |
| `SERVICE_ID` | `news-feed-1` | Unique service identifier |
| `API_KEY` | _(empty)_ | Set to enable auth on all `/api/*` endpoints |
| `SERVICE_API_KEY` | _(empty)_ | Key used to register with Bot Manager |
| `SCHEDULE_INTERVAL_MINUTES` | `0` | Auto-run all bots on this interval (0 = disabled) |
| `BATCH_MAX_WORKERS` | `5` | Max concurrent bots during batch runs |
| `DATA_DIR` | `/app/data` | Directory for per-bot SQLite databases |

## Running tests

```bash
docker exec news-feed python -m pytest tests/ -v
```

## Architecture

```
Google News RSS  ──┐
NewsAPI          ──┤──▶  Fetch  ──▶  Filter  ──▶  Summarize  ──▶  /api/news response
                         (relevance scoring)      (LLM or fallback)
                                                       │
                                                       └──▶  Telegram channel (optional)
```

## AI options

| Option | Cost | Speed | Persian quality |
|---|---|---|---|
| OpenAI `gpt-4o-mini` | ~$0.001/article | Fast | Excellent |
| Ollama `llama3.2` | Free | Slower | Good |
| Ollama `qwen2.5` | Free | Slower | Better for Persian |
| None | Free | Instant | No summaries |

## Project structure

```
news-feed/
├── app.py            # Flask routes + scheduler
├── processor.py      # Fetch, filter, summarize, post logic
├── service.py        # Bot Manager integration
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── tests/
    └── test_processor.py
```

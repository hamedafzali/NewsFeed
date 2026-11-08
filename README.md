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
5. Set `SCHEDULE_INTERVAL_MINUTES` to anything above 0 to post automatically; each bot then runs every **Run every** minutes set on it in the panel (default 60), and **Posts per day** spaces its posts out over the bot's posting hours (**Post from** / **Post until**, Berlin time, default 08–20; outside them the scheduler doesn't run the bot at all, so nothing is posted at night and no AI requests are made). The panel's Run button still runs a bot at any time. Already-posted articles are never re-sent per channel.

Each run takes items from every active feed in turn, keeps only articles published within `MAX_ARTICLE_AGE_HOURS` (default 36), drops Google News redirects, videos and live tickers, ranks newest-first, and posts one article per story (titles overlapping an earlier pick or a recent post are skipped).

### Post writing (optional Mistral)

Set `MISTRAL_API_KEY` in the deployment environment (e.g. the container manager's project variables) to have Mistral write each post: a short headline and 2–3 sentence summary in the channel language, from the full article text. `MISTRAL_MODEL` pins a model; empty picks the best one the key can use and skips models that are out of quota. Without a key, or if Mistral fails, posts fall back to translating the RSS teaser with LibreTranslate/MyMemory. API keys are never entered in the panel.

More free providers can be added: set `GEMINI_API_KEY` and/or `GROQ_API_KEY` and list the order in `LLM_PROVIDERS` (e.g. `gemini,groq,mistral`). Each request goes to the first provider that has a key and answers; if one fails or runs out of quota the next takes over. Learning lessons are fact-checked by the next provider in the list (or `LLM_CHECK_PROVIDERS`), so a different model checks what the first one wrote. `GEMINI_MODEL` / `GROQ_MODEL` override the default models.

A learning channel posts each lesson as one message: the news photo (from the feed, or the article page's `og:image`) as a short video with the slow audio, the lesson text as caption, and a practice button. The video is built with ffmpeg (installed in the Docker image); without a photo or ffmpeg the lesson goes out as an audio post.

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

`panel.db` also records, for the bot pages and reports:

- `runs` / `run_steps`: each bot run step by step (fetched, candidates, article, skip, format, sent).
- `llm_calls`: every AI request (bot, run, purpose lesson/check/post, provider, model, status, tokens, time, answer).
  Run steps and AI answers are pruned after 30 days; the counts stay.
- `posts.message_id` / `posts.lesson_id`: the link to each channel post.
- `channel_stats`: the channel's subscriber count, once a day.
- `lesson_opens`: how often each lesson's Üben button was used — a count only.
- Nothing about the people who use the bots is stored: no Telegram ids, names or answers (quizzes are
  anonymous).
- `post_reactions`: latest reaction counts of channel posts (learning bots, which poll their updates).

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

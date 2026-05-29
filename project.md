# News Feed Service

## 🎯 Project Overview
News processing and Telegram posting microservice. Handles news fetching, content extraction, AI summarization, translation, and posting to Telegram channels.

## 🏗️ Architecture
- **Type**: REST API Microservice
- **Port**: 5003
- **Database**: Per-bot SQLite databases
- **Framework**: Flask
- **Dependencies**: Bot Manager Service

## 📁 Project Structure
```
news-feed/
├── project.md          # This file
├── requirements.txt    # Python dependencies
├── app.py             # Main Flask application
├── service.py         # Service management class
├── processor.py       # News processing logic
└── Dockerfile         # Container configuration
```

## 🚀 Quick Start
```bash
cd news-feed
pip install -r requirements.txt
export BOT_MANAGER_URL=http://localhost:5002
python3 app.py
```
Service starts on http://localhost:5003

## 📡 API Endpoints

### News Processing
- `POST /api/process/{bot_id}` - Process news for specific bot
- `POST /api/test/{bot_id}` - Test bot connection
- `GET /api/stats/{bot_id}` - Get bot statistics
- `POST /api/process/batch` - Process multiple bots
- `GET /api/bots` - Get active bots

### Service Management
- `GET /health` - Service health check
- `GET /api/service/info` - Service information
- `POST /api/service/ping` - Ping service

## 🔧 Configuration
Environment Variables:
- `BOT_MANAGER_URL` - Bot Manager service URL (required)
- `SERVICE_ID` - Unique service identifier (default: news-feed-1)
- `SERVICE_PORT` - Service port (default: 5003)
- `SECRET_KEY` - Flask secret key

## 📊 Processing Pipeline

### 1. News Fetching
- Google News RSS feeds
- NewsAPI integration (optional)
- City and language targeting

### 2. Content Processing
- Article content extraction
- Relevance scoring
- Duplicate detection

### 3. AI Enhancement
- OpenAI summarization (optional)
- Persian translation (optional)
- Content filtering

### 4. Telegram Posting
- Formatted message creation
- Channel posting
- Error handling and retries

## 🗄️ Database Structure
Each bot gets its own SQLite database: `bot_{bot_id}.db`
```sql
CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    source TEXT,
    published_at TIMESTAMP,
    summary_en TEXT,
    summary_fa TEXT,
    relevance_score REAL,
    posted BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 🐳 Docker Deployment
```bash
docker build -t news-feed .
docker run -p 5003:5003 \
  -e BOT_MANAGER_URL=http://host.docker.internal:5002 \
  -v $(pwd)/data:/app/data \
  news-feed
```

## 🔄 External Dependencies
- **Bot Manager Service** - Bot configuration and run recording
- **Telegram Bot API** - Message posting
- **OpenAI API** - Summarization and translation (optional)
- **NewsAPI** - Additional news sources (optional)

## 📈 Key Features
- ✅ Multi-source news fetching
- ✅ Intelligent content extraction
- ✅ AI-powered summarization
- ✅ Persian translation
- ✅ Relevance filtering
- ✅ Duplicate detection
- ✅ Telegram posting
- ✅ Per-bot databases
- ✅ Service registration
- ✅ Health monitoring

## 🎯 Responsibilities
1. **News Fetching** - Collect articles from multiple sources
2. **Content Processing** - Extract and filter relevant content
3. **AI Enhancement** - Summarize and translate articles
4. **Telegram Posting** - Publish formatted messages to channels
5. **Service Communication** - Register with Bot Manager and report results

## 🔍 Monitoring
- Health check endpoint: `/health`
- Processing metrics tracking
- Error logging and reporting
- Service registration status

## 📝 Notes
- Stateless service design
- Automatic service registration
- Per-bot database isolation
- Configurable AI integration
- Robust error handling

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    service_name: str = "news-intelligence-platform"
    service_version: str = "3.0.0"
    service_port: int = 8003
    data_dir: str = "./data"
    database_url: str = ""
    api_key: str = ""
    default_language: str = "en"
    request_timeout_seconds: int = 12
    max_feed_entries: int = 40
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    enable_auth_for_panel: bool = True


def get_settings() -> Settings:
    return Settings(
        service_port=int(os.getenv("SERVICE_PORT", "8003")),
        data_dir=os.getenv("DATA_DIR", "./data"),
        database_url=os.getenv("DATABASE_URL", ""),
        api_key=os.getenv("API_KEY", ""),
        default_language=os.getenv("DEFAULT_LANGUAGE", "en"),
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "12")),
        max_feed_entries=int(os.getenv("MAX_FEED_ENTRIES", "40")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        enable_auth_for_panel=os.getenv("ENABLE_PANEL_AUTH", "true").lower() == "true",
    )

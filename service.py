"""
News Feed Service - Main Service Class
"""
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from processor import NewsProcessor


class NewsFeedService:
    """Coordinates news processing for bots via the Bot Manager."""

    def __init__(self, bot_manager_url: str = "http://localhost:5002",
                 service_id: str = "news-feed-1"):
        self.bot_manager_url = bot_manager_url
        self.service_id = service_id
        self.logger = logging.getLogger(__name__)
        self._register_with_manager()

    def _register_with_manager(self):
        api_key = os.getenv("SERVICE_API_KEY", "")
        try:
            response = requests.post(
                f"{self.bot_manager_url}/api/services",
                json={
                    "id": self.service_id,
                    "name": "News Feed Service",
                    "service_type": "news_processor",
                    "endpoint_url": f"http://localhost:{os.getenv('SERVICE_PORT', 5003)}",
                    "api_key": api_key,
                },
                timeout=10,
            )
            if response.status_code == 201:
                self.logger.info("Registered with Bot Manager")
            else:
                self.logger.warning(f"Registration returned {response.status_code}: {response.text}")
        except Exception as e:
            self.logger.warning(f"Registration failed (will retry on next ping): {e}")

    def process_bot_news(self, bot_id: str) -> Dict[str, Any]:
        try:
            bot_config = self._get_bot_config(bot_id)
            if not bot_config:
                return {"error": "Bot not found", "bot_id": bot_id}

            self._update_bot_status(bot_id, "running")
            processor = NewsProcessor(bot_config)
            result = processor.process_news()
            self._record_bot_run(bot_id, result)

            status = "idle" if result.status == "success" else "error"
            self._update_bot_status(bot_id, status, result.error_message)

            return {
                "bot_id": bot_id,
                "processed": result.processed,
                "posted": result.posted,
                "duration": result.duration,
                "status": result.status,
                "error_message": result.error_message,
            }
        except Exception as e:
            self.logger.error(f"Processing error for bot {bot_id}: {e}")
            self._update_bot_status(bot_id, "error", str(e))
            return {"error": str(e), "bot_id": bot_id}

    def _get_bot_config(self, bot_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = requests.get(
                f"{self.bot_manager_url}/api/bots/{bot_id}", timeout=10
            )
            if response.status_code == 200:
                bot_data = response.json()
                config = bot_data["config"]
                config["bot_id"] = bot_id
                return config
        except Exception as e:
            self.logger.error(f"Error fetching bot config for {bot_id}: {e}")
        return None

    def _update_bot_status(self, bot_id: str, status: str, error_message: str = None):
        try:
            data: Dict[str, Any] = {"status": status}
            if error_message:
                data["error_message"] = error_message
            response = requests.put(
                f"{self.bot_manager_url}/api/bots/{bot_id}/status",
                json=data,
                timeout=10,
            )
            if response.status_code != 200:
                self.logger.warning(f"Status update failed: {response.text}")
        except Exception as e:
            self.logger.warning(f"Status update error for bot {bot_id}: {e}")

    def _record_bot_run(self, bot_id: str, result):
        try:
            run_data = {
                "run_time": datetime.utcnow().isoformat(),
                "processed": result.processed,
                "posted": result.posted,
                "duration": result.duration,
                "status": result.status,
                "error_message": result.error_message,
                "metadata": {
                    "service_id": self.service_id,
                    "articles_count": len(result.articles),
                },
            }
            response = requests.post(
                f"{self.bot_manager_url}/api/bots/{bot_id}/runs",
                json=run_data,
                timeout=10,
            )
            if response.status_code != 201:
                self.logger.warning(f"Run recording failed: {response.text}")
        except Exception as e:
            self.logger.warning(f"Run recording error for bot {bot_id}: {e}")

    def test_bot_connection(self, bot_id: str) -> bool:
        try:
            bot_config = self._get_bot_config(bot_id)
            if not bot_config:
                return False
            return NewsProcessor(bot_config).test_connection()
        except Exception as e:
            self.logger.error(f"Connection test error for bot {bot_id}: {e}")
            return False

    def get_bot_stats(self, bot_id: str) -> Dict[str, Any]:
        try:
            bot_config = self._get_bot_config(bot_id)
            if not bot_config:
                return {"error": "Bot not found"}
            return NewsProcessor(bot_config).get_stats()
        except Exception as e:
            self.logger.error(f"Stats error for bot {bot_id}: {e}")
            return {"error": str(e)}

    def ping_manager(self) -> bool:
        try:
            response = requests.post(
                f"{self.bot_manager_url}/api/services/{self.service_id}/ping",
                timeout=5,
            )
            return response.status_code == 200
        except Exception:
            return False

    def get_active_bots(self) -> List[Dict[str, Any]]:
        try:
            response = requests.get(
                f"{self.bot_manager_url}/api/bots?active_only=true", timeout=10
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.error(f"Error fetching active bots: {e}")
        return []

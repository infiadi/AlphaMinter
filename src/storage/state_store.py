import logging
from typing import Set, List
import requests
from config.settings import WORKER_URL, INTERNAL_SECRET, NSE_HEADERS

logger = logging.getLogger(__name__)

class StateStore:
    """
    Direct Cloudflare D1 Bridge:
    - Queries the live Cloudflare Worker (/api/silenced-list) on initialization.
    - Protected with X-Internal-Secret authentication header.
    - Zero local file state or Git commits required.
    - O(1) set lookups during Bhavcopy & PEAD scans.
    """
    def __init__(self, worker_url: str = WORKER_URL, secret: str = INTERNAL_SECRET):
        self.worker_url = worker_url.rstrip("/") if worker_url else ""
        self.secret = secret
        self.silenced_set: Set[str] = set()
        self.sync_with_cloud()

    def sync_with_cloud(self):
        """Fetches the live list of silenced tickers directly from Cloudflare D1."""
        if not self.worker_url:
            logger.warning("No WORKER_URL configured in environment.")
            return

        api_url = f"{self.worker_url}/api/silenced-list"
        logger.info(f"Syncing silenced tickers from Cloudflare Worker: {api_url}")
        
        headers = dict(NSE_HEADERS)
        if self.secret:
            headers["X-Internal-Secret"] = self.secret

        try:
            resp = requests.get(api_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                cloud_silenced = [s.strip().upper() for s in data.get("silenced", [])]
                self.silenced_set = set(cloud_silenced)
                logger.info(f"Successfully synced {len(self.silenced_set)} silenced tickers from Cloudflare D1.")
            elif resp.status_code == 401:
                logger.error("Cloudflare Worker authentication failed: Unauthorized (invalid X-Internal-Secret).")
            else:
                logger.warning(f"Worker returned HTTP {resp.status_code}.")
        except Exception as e:
            logger.error(f"Failed to reach Cloudflare Worker: {e}")

    def is_ticker_silenced(self, symbol: str) -> bool:
        """Instant O(1) check if ticker is permabanned or in active 30-day cooldown."""
        return symbol.strip().upper() in self.silenced_set

    def get_deep_dive_queue(self) -> List[str]:
        """Fetches pending deep-dive requests from Cloudflare Worker /api/deep-dive-queue."""
        if not self.worker_url:
            return []
        api_url = f"{self.worker_url}/api/deep-dive-queue"
        headers = dict(NSE_HEADERS)
        if self.secret:
            headers["X-Internal-Secret"] = self.secret
        try:
            resp = requests.get(api_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return [s.strip().upper() for s in data.get("queued", [])]
        except Exception as e:
            logger.warning(f"Failed to fetch deep dive queue from Worker: {e}")
        return []

    def is_already_alerted_today(self, symbol: str, signal_type: str) -> bool:
        return False

    def log_alert(self, symbol: str, signal_type: str, details: str = ""):
        pass

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import requests
from config.settings import STOCKS_STATE_PATH, TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

class StateStore:
    """
    Ultra-Lean State & Cooldown Store:
    - Zero Cloudflare / Zero external database dependencies.
    - Uses human-readable, Git-diffable JSON (data/stocks_state.json).
    - Drains Telegram's getUpdates queue directly before each scan to ingest 1-tap button clicks.
    """
    def __init__(self, file_path: Path = STOCKS_STATE_PATH):
        self.file_path = file_path
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            initial_state = {
                "_metadata": {
                    "last_updated": datetime.now().isoformat(),
                    "last_telegram_offset": 0
                },
                "stocks": {},
                "alerts_today": {}
            }
            self._save(initial_state)

    def _load(self) -> Dict:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading state from {self.file_path}: {e}")
            return {"_metadata": {"last_telegram_offset": 0}, "stocks": {}, "alerts_today": {}}

    def _save(self, state: Dict):
        state["_metadata"]["last_updated"] = datetime.now().isoformat()
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving state to {self.file_path}: {e}")

    def drain_telegram_updates(self, bot_token: str = TELEGRAM_BOT_TOKEN) -> int:
        """
        Drains Telegram getUpdates queue to process any [Watchlist], [Drop 30D], or [Permaban]
        buttons clicked on your phone over the last 24 hours.
        Acknowledges callbacks so loading spinners stop.
        """
        if not bot_token or bot_token == "your_telegram_bot_token_here":
            logger.info("Telegram bot token not configured; skipping getUpdates drain.")
            return 0

        state = self._load()
        last_offset = state.get("_metadata", {}).get("last_telegram_offset", 0)
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
        params = {"offset": last_offset + 1, "timeout": 2}

        logger.info(f"Draining Telegram getUpdates (offset: {last_offset + 1})...")
        updates_processed = 0
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                updates = data.get("result", [])
                max_update_id = last_offset

                for u in updates:
                    update_id = u.get("update_id", 0)
                    if update_id > max_update_id:
                        max_update_id = update_id

                    # Handle 1-Tap Inline Callback Buttons
                    if "callback_query" in u:
                        cb = u["callback_query"]
                        cb_id = cb.get("id")
                        cb_data = cb.get("data", "")
                        
                        # Expected format: "DROP:SYMBOL", "WATCH:SYMBOL", "BAN:SYMBOL"
                        if ":" in cb_data:
                            action, symbol = cb_data.split(":", 1)
                            symbol = symbol.strip().upper()
                            today = datetime.now().date()
                            ack_text = ""

                            if action == "DROP":
                                cooldown = (today + timedelta(days=30)).isoformat()
                                state["stocks"][symbol] = {
                                    "status": "DROPPED_TEMP",
                                    "cooldown_until": cooldown,
                                    "updated_at": today.isoformat()
                                }
                                ack_text = f"❌ Dropped {symbol} for 30 Days"
                            elif action == "WATCH":
                                state["stocks"][symbol] = {
                                    "status": "WATCHLIST",
                                    "updated_at": today.isoformat()
                                }
                                ack_text = f"⭐ Added {symbol} to Watchlist"
                            elif action == "BAN":
                                state["stocks"][symbol] = {
                                    "status": "PERMABAN",
                                    "updated_at": today.isoformat()
                                }
                                ack_text = f"🚫 Permabanned {symbol}"

                            # Acknowledge callback so button click completes
                            try:
                                requests.post(
                                    f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                                    json={"callback_query_id": cb_id, "text": ack_text},
                                    timeout=5
                                )
                            except Exception:
                                pass

                            logger.info(f"Processed button tap: {ack_text}")
                            updates_processed += 1

                # Update offset to mark processed updates as acknowledged
                if updates:
                    state["_metadata"]["last_telegram_offset"] = max_update_id
                    self._save(state)

        except Exception as e:
            logger.error(f"Error draining Telegram getUpdates: {e}")

        logger.info(f"Finished getUpdates drain. Processed {updates_processed} button actions.")
        return updates_processed

    def is_ticker_silenced(self, symbol: str) -> bool:
        """Returns True if the stock is dropped (cooldown active) or permabanned."""
        symbol = symbol.strip().upper()
        today = datetime.now().date().isoformat()
        state = self._load()
        stock_info = state.get("stocks", {}).get(symbol)

        if not stock_info:
            return False

        status = stock_info.get("status")
        if status == "PERMABAN":
            return True

        if status == "DROPPED_TEMP":
            cooldown = stock_info.get("cooldown_until")
            if cooldown and cooldown >= today:
                return True

        return False

    def is_already_alerted_today(self, symbol: str, signal_type: str) -> bool:
        symbol = symbol.strip().upper()
        today = datetime.now().date().isoformat()
        state = self._load()
        today_alerts = state.get("alerts_today", {}).get(today, {})
        return symbol in today_alerts.get(signal_type, [])

    def log_alert(self, symbol: str, signal_type: str, details: str = ""):
        symbol = symbol.strip().upper()
        today = datetime.now().date().isoformat()
        state = self._load()
        if "alerts_today" not in state:
            state["alerts_today"] = {}
        if today not in state["alerts_today"]:
            # Clean up old dates to keep file lean
            state["alerts_today"] = {today: {}}
        if signal_type not in state["alerts_today"][today]:
            state["alerts_today"][today][signal_type] = []

        if symbol not in state["alerts_today"][today][signal_type]:
            state["alerts_today"][today][signal_type].append(symbol)

        self._save(state)

    def set_ticker_status(self, symbol: str, status: str, days_cooldown: int = 30):
        symbol = symbol.strip().upper()
        today = datetime.now().date()
        state = self._load()
        data = {"status": status, "updated_at": today.isoformat()}
        if status == "DROPPED_TEMP":
            data["cooldown_until"] = (today + timedelta(days=days_cooldown)).isoformat()

        state["stocks"][symbol] = data
        self._save(state)

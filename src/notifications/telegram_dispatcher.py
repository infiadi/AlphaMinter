import logging
import requests
from typing import Optional, List, Dict
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

class TelegramDispatcher:
    def __init__(self, bot_token: str = TELEGRAM_BOT_TOKEN, chat_id: str = TELEGRAM_CHAT_ID):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id and self.bot_token != "your_telegram_bot_token_here")

    def send_message(self, text: str, reply_markup: Optional[Dict] = None) -> bool:
        """Sends an HTML formatted message to Telegram."""
        if not self.is_configured():
            logger.warning("[TelegramDispatcher] Not configured. Printing alert to stdout:")
            print("\n" + "="*50 + "\n[TELEGRAM ALERT PREVIEW]\n" + text + "\n" + "="*50 + "\n")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("Telegram alert delivered successfully.")
                return True
            else:
                logger.error(f"Telegram API error {resp.status_code}: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def build_triage_buttons(self, symbol: str) -> Dict:
        """
        Builds 1-tap inline triage buttons:
        - Row 1: TradingView Chart Link + Screener Link
        - Row 2: 1-Tap Callbacks (Watchlist, Drop 30D, Permaban)
        """
        symbol = symbol.strip().upper()
        return {
            "inline_keyboard": [
                [
                    {"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"},
                    {"text": "📊 Screener", "url": f"https://www.screener.in/company/{symbol}/"}
                ],
                [
                    {"text": "⭐ Watchlist", "callback_data": f"WATCH:{symbol}"},
                    {"text": "❌ Drop 30D", "callback_data": f"DROP:{symbol}"},
                    {"text": "🚫 Permaban", "callback_data": f"BAN:{symbol}"}
                ]
            ]
        }

    def send_stock_alert(self, title: str, symbol: str, company: str, metrics: List[str], thesis: str, buttons: bool = True):
        """Standardized high-clarity stock alert card for Android."""
        formatted_metrics = "\n".join([f"• {m}" for m in metrics])
        html_msg = (
            f"<b>{title}</b>\n\n"
            f"<b>Ticker:</b> <code>{symbol}</code> ({company})\n"
            f"<b>Key Signals:</b>\n{formatted_metrics}\n\n"
            f"<b>Setup Note:</b>\n<i>{thesis}</i>"
        )
        markup = self.build_triage_buttons(symbol) if buttons else None
        return self.send_message(html_msg, reply_markup=markup)

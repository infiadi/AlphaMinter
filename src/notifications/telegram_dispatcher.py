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
        """Sends an HTML formatted message to Telegram with automatic length safety."""
        if not self.is_configured():
            logger.warning("[TelegramDispatcher] Not configured. Printing alert to stdout:")
            print("\n" + "="*50 + "\n[TELEGRAM ALERT PREVIEW]\n" + text + "\n" + "="*50 + "\n")
            return False

        # Telegram hard limits message length to 4096 UTF-8 characters.
        # If text exceeds 4000 chars, cleanly split into two parts.
        if len(text) > 4000:
            split_idx = text.rfind("\n\n", 0, 3800)
            if split_idx == -1:
                split_idx = text.rfind("\n", 0, 3800)
            if split_idx == -1:
                split_idx = 3800
            part1 = text[:split_idx].strip()
            part2 = text[split_idx:].strip()

            p1_ok = self._send_single_message(part1, reply_markup=None)
            p2_ok = self._send_single_message(part2, reply_markup=reply_markup)
            return p1_ok or p2_ok

        return self._send_single_message(text, reply_markup=reply_markup)

    def _send_single_message(self, text: str, reply_markup: Optional[Dict] = None) -> bool:
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

            err_desc = resp.text.lower()
            if "message is too long" in err_desc:
                logger.warning("Message too long for Telegram. Trimming to 3800 chars and retrying...")
                payload["text"] = text[:3800] + "\n\n<i>[Content truncated to fit Telegram limit]</i>"
                resp_retry = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
                return resp_retry.status_code == 200

            if "can't parse entities" in err_desc:
                logger.warning("HTML parsing error from Telegram. Retrying without HTML parse mode...")
                payload.pop("parse_mode", None)
                resp_retry = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
                return resp_retry.status_code == 200

            logger.error(f"Telegram API error {resp.status_code}: {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def build_triage_buttons(self, symbol: str) -> Dict:
        """
        Builds 1-tap inline triage buttons:
        - Row 1: External Links (TradingView, Screener, Yahoo Finance)
        - Row 2: High-Conviction Action (⭐ Watchlist)
        - Row 3: Triage / Filtering (❌ Drop 30D, 🚫 Permaban)
        """
        symbol = symbol.strip().upper()
        return {
            "inline_keyboard": [
                [
                    {"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"},
                    {"text": "📊 Screener", "url": f"https://www.screener.in/company/{symbol}/"},
                    {"text": "🟣 Yahoo", "url": f"https://finance.yahoo.com/quote/{symbol}.NS"}
                ],
                [
                    {"text": "⭐ Watchlist", "callback_data": f"WATCH:{symbol}"}
                ],
                [
                    {"text": "❌ Drop 30D", "callback_data": f"DROP:{symbol}"},
                    {"text": "🚫 Permaban", "callback_data": f"BAN:{symbol}"}
                ]
            ]
        }

    def send_deep_dive_report(self, symbol: str, html_text: str) -> bool:
        """Sends an institutional deep dive alert card to Telegram."""
        buttons = {
            "inline_keyboard": [
                [
                    {"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"},
                    {"text": "📊 Screener", "url": f"https://www.screener.in/company/{symbol}/"}
                ],
                [
                    {"text": "⭐ Add Watchlist", "callback_data": f"WATCH:{symbol}"},
                    {"text": "❌ Drop 30D", "callback_data": f"DROP:{symbol}"}
                ]
            ]
        }
        return self.send_message(html_text, reply_markup=buttons)

    def send_monthly_delta_card(self, symbol: str, html_text: str) -> bool:
        """Sends a 30-day monthly delta health-check alert card to Telegram."""
        buttons = {
            "inline_keyboard": [
                [
                    {"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"},
                    {"text": "📊 Screener", "url": f"https://www.screener.in/company/{symbol}/"}
                ]
            ]
        }
        return self.send_message(html_text, reply_markup=buttons)

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

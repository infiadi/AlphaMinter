import os
from pathlib import Path
from dotenv import load_dotenv

# Load local .env if available
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Paths
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STOCKS_STATE_PATH = DATA_DIR / "stocks_state.json"

# Bhavcopy Delivery Scanner Thresholds (Hardened Guards)
MIN_DELIVERY_PCT = float(os.getenv("MIN_DELIVERY_PCT", "65.0"))
MIN_DELIVERY_TURNOVER_CR = float(os.getenv("MIN_DELIVERY_TURNOVER_CR", "5.0"))  # Min ₹5 Crore Delivery Turnover
MIN_VOLUME_SPIKE_X = float(os.getenv("MIN_VOLUME_SPIKE_X", "2.5"))
MAX_DAILY_PRICE_CHANGE_PCT = float(os.getenv("MAX_DAILY_PRICE_CHANGE_PCT", "4.0"))  # Tight range accumulation

# PEAD (Post-Earnings Announcement Drift) Thresholds
MIN_PAT_GROWTH_PCT = float(os.getenv("MIN_PAT_GROWTH_PCT", "30.0"))
MIN_REVENUE_GROWTH_PCT = float(os.getenv("MIN_REVENUE_GROWTH_PCT", "15.0"))
MIN_OPM_EXPANSION_BPS = float(os.getenv("MIN_OPM_EXPANSION_BPS", "150.0"))
MIN_REACTION_PRICE_PCT = float(os.getenv("MIN_REACTION_PRICE_PCT", "4.0"))
MIN_REACTION_VOLUME_X = float(os.getenv("MIN_REACTION_VOLUME_X", "3.0"))
MIN_REACTION_DELIV_PCT = float(os.getenv("MIN_REACTION_DELIV_PCT", "50.0"))

# Buyback Arbitrage Thresholds
RETAIL_INVESTMENT_LIMIT_SAFE = float(os.getenv("RETAIL_INVESTMENT_LIMIT_SAFE", "195000.0"))  # ₹1.95L buffer

# Request Headers
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com"
}

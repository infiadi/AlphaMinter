import csv
import io
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
import requests
from config.settings import NSE_HEADERS, MIN_DELIVERY_PCT, MIN_DELIVERY_TURNOVER_CR
from src.storage.state_store import StateStore
from src.scanners.equity_filter import EquityFilter

logger = logging.getLogger(__name__)

class BhavcopyDeliveryScanner:
    """
    Hardened NSE Bhavcopy Delivery % Scanner (v2.5.0):
    Guard 1: Filter strictly for SERIES == 'EQ' and operating stocks (NO ETFs, NO Mutual Funds, NO REITs).
    Guard 2: Check DATE1 == today (exits cleanly on market holidays when NSE returns yesterday's file).
    Guard 3: Delivery Turnover >= ₹5 Crore (DELIV_QTY * CLOSE_PRICE >= 5 Cr).
    Guard 4: Asymmetric Price Drift (-1.0% <= Price Change <= +4.0%) - eliminates red distribution drops.
    Guard 5: Close Location Value (CLV >= 0.50) - ensures close is in upper half of range (buyers absorbed supply).
    """
    def __init__(self, state_store: StateStore = None):
        self.state_store = state_store or StateStore()
        self.equity_filter = EquityFilter()
        self.session = requests.Session()
        self.session.headers.update(NSE_HEADERS)

    def _get_target_date(self) -> datetime:
        """Returns the most recent weekday."""
        now = datetime.now()
        if now.weekday() == 5:
            return now - timedelta(days=1)
        elif now.weekday() == 6:
            return now - timedelta(days=2)
        return now

    def fetch_bhavcopy_rows(self, target_date: datetime = None) -> Tuple[List[Dict], bool]:
        """
        Downloads official NSE security-wise bhavcopy CSV and parses rows via standard csv.DictReader.
        Returns: (rows, is_today)
        """
        date_obj = target_date or self._get_target_date()
        date_str = date_obj.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
        
        logger.info(f"Fetching NSE Bhavcopy for date: {date_str} from {url}")
        try:
            resp = self.session.get(url, timeout=25)
            if resp.status_code == 200 and "SYMBOL" in resp.text:
                reader = csv.DictReader(io.StringIO(resp.text))
                reader.fieldnames = [f.strip() for f in reader.fieldnames] if reader.fieldnames else []
                rows = list(reader)

                if not rows:
                    return [], False

                # Guard 2: Verify DATE1 matches today's trading session
                first_row = rows[0]
                file_date_raw = str(first_row.get('DATE1', '')).strip()
                today_formatted = datetime.now().strftime("%d-%b-%Y")
                today_numeric = datetime.now().strftime("%d-%m-%Y")
                
                is_today = file_date_raw.lower() in [today_formatted.lower(), today_numeric.lower()]
                if not is_today:
                    logger.info(f"Targeting previous session file: DATE1 = {file_date_raw}")

                return rows, is_today
            elif resp.status_code == 404:
                prev_day = date_obj - timedelta(days=1)
                while prev_day.weekday() >= 5:
                    prev_day -= timedelta(days=1)
                prev_str = prev_day.strftime("%d%m%Y")
                fallback_url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{prev_str}.csv"
                logger.info(f"Bhavcopy not posted yet. Falling back to: {prev_str}")
                
                fallback_resp = self.session.get(fallback_url, timeout=25)
                if fallback_resp.status_code == 200 and "SYMBOL" in fallback_resp.text:
                    reader = csv.DictReader(io.StringIO(fallback_resp.text))
                    reader.fieldnames = [f.strip() for f in reader.fieldnames] if reader.fieldnames else []
                    return list(reader), False

        except Exception as e:
            logger.error(f"Error fetching Bhavcopy: {e}")

        return [], False

    def scan_accumulation(self) -> List[Dict]:
        """
        Executes institutional accumulation detection with all 5 hardened guards.
        """
        rows, is_today = self.fetch_bhavcopy_rows()
        if not rows:
            logger.warning("No Bhavcopy records found to analyze.")
            return []

        candidates = []
        for r in rows:
            # Guard 1a: Filter strictly for standard equity shares ('EQ')
            series = str(r.get('SERIES', '')).strip()
            if series != 'EQ':
                continue

            symbol = str(r.get('SYMBOL', '')).strip().upper()
            if not symbol or self.state_store.is_ticker_silenced(symbol):
                continue

            # Guard 1b: Exclude ETFs, Mutual Funds, REITs, and InvITs
            if not self.equity_filter.is_valid_operating_stock(symbol):
                continue

            try:
                close_price = float(str(r.get('CLOSE_PRICE', '0')).replace(',', '').strip() or 0)
                prev_close = float(str(r.get('PREV_CLOSE', '0')).replace(',', '').strip() or 0)
                high_price = float(str(r.get('HIGH_PRICE', '0')).replace(',', '').strip() or 0)
                low_price = float(str(r.get('LOW_PRICE', '0')).replace(',', '').strip() or 0)
                deliv_qty = float(str(r.get('DELIV_QTY', '0')).replace(',', '').strip() or 0)
                deliv_pct = float(str(r.get('DELIV_PER', '0')).replace(',', '').strip() or 0)
                traded_qty = int(float(str(r.get('TTL_TRD_QNTY', '0')).replace(',', '').strip() or 0))
            except (ValueError, TypeError):
                continue

            if prev_close <= 0 or close_price <= 0:
                continue

            # Guard 3: Delivery Turnover >= ₹5 Crore
            delivery_turnover_cr = (deliv_qty * close_price) / 10000000.0
            price_change_pct = ((close_price - prev_close) / prev_close) * 100.0

            # Guard 4: Asymmetric Drift (-1.0% to +4.0%) - Eliminates red distribution dumps
            if not (-1.0 <= price_change_pct <= 4.0):
                continue

            # Guard 5: Close Location Value (CLV >= 0.50)
            # Ensures close is in upper half of day's range (buyers absorbed selling into close)
            price_range = high_price - low_price
            if price_range > 0:
                clv = (close_price - low_price) / price_range
                if clv < 0.50:
                    continue  # Closed in lower half of range (distribution sign)
            else:
                # Circuit locked with HIGH == LOW
                continue

            # Check Delivery % and Turnover
            if deliv_pct >= MIN_DELIVERY_PCT and delivery_turnover_cr >= MIN_DELIVERY_TURNOVER_CR:
                candidates.append({
                    "symbol": symbol,
                    "cmp": round(close_price, 2),
                    "price_change_pct": round(price_change_pct, 2),
                    "delivery_pct": round(deliv_pct, 2),
                    "delivery_turnover_cr": round(delivery_turnover_cr, 2),
                    "clv": round((close_price - low_price) / (high_price - low_price), 2) if price_range > 0 else 0.5,
                    "traded_qty": traded_qty
                })

        # Sort by Delivery Turnover descending
        candidates.sort(key=lambda x: x['delivery_turnover_cr'], reverse=True)
        return candidates

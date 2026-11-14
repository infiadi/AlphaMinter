import logging
import io
import csv
import re
from typing import Set
import requests
from config.settings import NSE_HEADERS

logger = logging.getLogger(__name__)

# Strict regex targeting only instrument suffixes and prefixes (avoids false-flagging GOLDIAM, GOLDTECH, etc.)
BLOCKED_PATTERN = re.compile(
    r"(BEES|IETF|ETF|REIT|INVIT)$|^(LIQUID|NIFTY|SENSEX)|^(EMBASSY|BROOKFIELD|MINDSPACE|BIRET|PGINVIT)", 
    re.IGNORECASE
)

class EquityFilter:
    """
    Guarantees that scanners process ONLY genuine operating equity companies:
    1. Uses official NSE EQUITY_L.csv as the primary allowlist.
    2. Fallback: Strict regex pattern matching instrument suffixes/prefixes (BEES, ETF, REIT, etc.).
    """
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(NSE_HEADERS)
        self.operating_equities: Set[str] = set()
        self.fetch_nse_equity_master()

    def is_blocked_instrument_pattern(self, symbol: str) -> bool:
        """Returns True ONLY for ETFs, Index funds, REITs, and InvITs."""
        return bool(BLOCKED_PATTERN.search(symbol.strip().upper()))

    def fetch_nse_equity_master(self) -> Set[str]:
        """
        Downloads official NSE listed operating equity master (EQUITY_L.csv).
        This file contains ONLY companies incorporated under the Companies Act.
        """
        if self.operating_equities:
            return self.operating_equities

        url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
        try:
            resp = self.session.get(url, timeout=12)
            if resp.status_code == 200 and "SYMBOL" in resp.text:
                reader = csv.DictReader(io.StringIO(resp.text))
                reader.fieldnames = [f.strip() for f in reader.fieldnames] if reader.fieldnames else []
                
                valid_symbols = set()
                for row in reader:
                    symbol = str(row.get("SYMBOL", "")).strip().upper()
                    if symbol and not self.is_blocked_instrument_pattern(symbol):
                        valid_symbols.add(symbol)
                
                self.operating_equities = valid_symbols
                logger.info(f"Loaded {len(self.operating_equities)} operating equities from NSE EQUITY_L.csv")
                return self.operating_equities
        except Exception as e:
            logger.warning(f"Could not reach NSE EQUITY_L.csv: {e}. Falling back to strict regex filter.")

        return set()

    def is_valid_operating_stock(self, symbol: str) -> bool:
        """Verifies ticker is an operating company (no ETF, no MF, no REIT)."""
        sym = symbol.strip().upper()
        
        # Primary allowlist check if available
        if self.operating_equities:
            return sym in self.operating_equities
            
        # Fallback regex filter if network was unavailable
        return not self.is_blocked_instrument_pattern(sym)

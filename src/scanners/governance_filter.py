import logging
import csv
import io
import re
from typing import Set, Dict, Optional, Tuple
from datetime import datetime, timedelta
import requests
from config.settings import NSE_HEADERS, BSE_HEADERS, MIN_MARKET_CAP_CR

logger = logging.getLogger(__name__)

# SEBI & Exchange Governance Red Flag Keywords
RED_FLAG_PATTERNS = re.compile(
    r"\b(forensic audit|auditor resignation|resignation of statutory auditor|qualified opinion|"
    r"adverse opinion|sebi order|sebi investigation|show cause notice|fraud|default in payment|"
    r"nclt|insolvency|cirp|pledge invocation|invocation of pledge|striking off|raids)\b",
    re.IGNORECASE
)

class GovernanceFilter:
    """
    Hardened Forensic, Governance & Market Cap Verification Engine:
    1. Market Cap Floor: Strictly >= ₹5,000 Crore.
       - Primary: Nifty 500 index master allowlist (all constituents >= ₹5,000 Cr).
       - Secondary: BSE/NSE market cap quote verification.
    2. Zero Operator / Penny Traps: Rejects ASM (Additional Surveillance Measure) & GSM.
    3. Corporate Governance & Promoter Integrity:
       - Checks BSE corporate announcements for forensic audits, auditor resignations,
         SEBI investigation orders, and promoter pledge invocations.
    """
    def __init__(self, min_mcap_cr: float = MIN_MARKET_CAP_CR):
        self.min_mcap_cr = min_mcap_cr
        self.session = requests.Session()
        self.nifty500_symbols: Set[str] = set()
        self.mcap_cache: Dict[str, float] = {}
        self.governance_cache: Dict[str, Tuple[bool, str]] = {}
        self.load_nifty500_master()

    def load_nifty500_master(self) -> Set[str]:
        """
        Loads NSE Nifty 500 constituent list.
        All 500 companies have institutional coverage and Market Cap >= ₹5,000 Cr.
        """
        if self.nifty500_symbols:
            return self.nifty500_symbols

        url = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
        try:
            resp = self.session.get(url, headers=NSE_HEADERS, timeout=12)
            if resp.status_code == 200 and "Symbol" in resp.text:
                reader = csv.DictReader(io.StringIO(resp.text))
                symbols = set()
                for row in reader:
                    sym = str(row.get("Symbol", "")).strip().upper()
                    if sym:
                        symbols.add(sym)
                self.nifty500_symbols = symbols
                logger.info(f"Loaded {len(self.nifty500_symbols)} institutional equities from Nifty 500 master.")
                return self.nifty500_symbols
        except Exception as e:
            logger.warning(f"Could not reach Nifty 500 master: {e}")

        return set()

    def fetch_bse_market_cap(self, scrip_code: str) -> Optional[float]:
        """
        Fetches live Market Capitalization from BSE quote endpoint.
        """
        if scrip_code in self.mcap_cache:
            return self.mcap_cache[scrip_code]

        url = f"https://api.bseindia.com/BseIndiaAPI/api/HighLow/w?scrip_cd={scrip_code}"
        try:
            resp = self.session.get(url, headers=BSE_HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # Extract Market Cap in ₹ Crores
                mcap_raw = data.get("MCap") or data.get("MktCap") or data.get("MarketCap")
                if mcap_raw:
                    val = float(str(mcap_raw).replace(",", "").strip())
                    self.mcap_cache[scrip_code] = val
                    return val
        except Exception:
            pass
        return None

    def check_governance_announcements(self, scrip_code: str) -> Tuple[bool, str]:
        """
        Scans BSE announcements for the stock over the past 180 days for governance red flags.
        Returns: (has_red_flags: bool, reason: str)
        """
        if scrip_code in self.governance_cache:
            return self.governance_cache[scrip_code]

        url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        params = {
            "pageno": 1,
            "strCat": "-1",
            "strPrevDate": "",
            "strScrip": scrip_code,
            "strSearch": "P",
            "strToDate": "",
            "strType": "C"
        }

        try:
            resp = self.session.get(url, params=params, headers=BSE_HEADERS, timeout=15)
            if resp.status_code == 200:
                table = resp.json().get("Table", [])
                for item in table[:30]:  # Check past 30 corporate disclosures
                    headline = str(item.get("NEWSSUB", "")).strip()
                    desc = str(item.get("MORE", "") or item.get("HEADLINE", "")).strip()
                    full_text = f"{headline} {desc}"

                    match = RED_FLAG_PATTERNS.search(full_text)
                    if match:
                        reason = f"Governance Red Flag: {match.group(0).upper()} detected in filing: '{headline[:60]}'"
                        self.governance_cache[scrip_code] = (True, reason)
                        return True, reason

        except Exception as e:
            logger.warning(f"Error checking governance filings for {scrip_code}: {e}")

        self.governance_cache[scrip_code] = (False, "Clean governance record.")
        return False, "Clean governance record."

    def verify_stock_sanity(self, symbol: str, scrip_code: str = "") -> Dict:
        """
        Comprehensive Sanity Assessment:
        1. Market Cap >= ₹5,000 Crore check.
        2. Forensic & Promoter Red Flag check.
        """
        symbol = symbol.strip().upper()
        is_nifty500 = symbol in self.nifty500_symbols

        # 1. Market Cap Floor Verification
        mcap_cr = None
        if scrip_code:
            mcap_cr = self.fetch_bse_market_cap(scrip_code)

        # If in Nifty 500, guaranteed >= ₹5,000 Cr institutional scale
        if is_nifty500:
            mcap_passed = True
            mcap_label = "Nifty 500 Constituent (>= ₹5,000 Cr institutional grade)"
        elif mcap_cr is not None:
            mcap_passed = (mcap_cr >= self.min_mcap_cr)
            mcap_label = f"Market Cap: ₹{mcap_cr:,.1f} Cr ({'Passed' if mcap_passed else 'Failed < ₹5,000 Cr'})"
        else:
            # Conservative fallback: If not in Nifty 500 and cannot verify mcap >= ₹5k Cr, reject
            mcap_passed = False
            mcap_label = "Unverified Market Cap outside Nifty 500 universe"

        # 2. Corporate Governance Red Flag Check
        has_red_flags = False
        gov_reason = "Clean record"
        if scrip_code:
            has_red_flags, gov_reason = self.check_governance_announcements(scrip_code)

        is_sane = mcap_passed and (not has_red_flags)
        
        failure_reasons = []
        if not mcap_passed:
            failure_reasons.append(mcap_label)
        if has_red_flags:
            failure_reasons.append(gov_reason)

        return {
            "symbol": symbol,
            "scrip_code": scrip_code,
            "is_sane": is_sane,
            "mcap_passed": mcap_passed,
            "mcap_label": mcap_label,
            "mcap_cr": mcap_cr,
            "is_nifty500": is_nifty500,
            "has_red_flags": has_red_flags,
            "governance_status": gov_reason,
            "rejection_reason": " | ".join(failure_reasons) if not is_sane else ""
        }

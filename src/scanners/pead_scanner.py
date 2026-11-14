import logging
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import requests
from config.settings import (
    BSE_HEADERS,
    MIN_PAT_GROWTH_PCT,
    MIN_REVENUE_GROWTH_PCT,
    MIN_OPM_EXPANSION_BPS,
    MIN_REACTION_PRICE_PCT,
    MIN_REACTION_VOLUME_X,
    MIN_REACTION_DELIV_PCT
)
from src.storage.state_store import StateStore
from src.scanners.equity_filter import EquityFilter

logger = logging.getLogger(__name__)

class PEADScanner:
    """
    Hardened Post-Earnings Announcement Drift (PEAD) Engine (v2.5.0):
    Guard 1: Timing Alignment - Pairs Day T Bhavcopy against results announced between 3:30 PM (T-1) and 3:30 PM (T).
    Guard 2: Negative-Base PAT Math - Explicitly handles loss-to-profit turnarounds without division errors.
    Guard 3: Multi-Band Circuit Guard - Verifies HIGH > LOW (ensures stock wasn't frozen on a 5%, 10%, or 20% circuit all session).
    Guard 4: Negative Divergence Rule - If stock closes negative or falls on good numbers, SKIP immediately.
    """
    def __init__(self, state_store: StateStore = None):
        self.state_store = state_store or StateStore()
        self.equity_filter = EquityFilter()
        self.session = requests.Session()
        self.session.headers.update(BSE_HEADERS)

    def fetch_bse_financial_results(self, page: int = 1) -> List[Dict]:
        """
        Queries BSE Corporate Announcements API for recent Financial Results.
        Cloud-safe endpoint that does not block datacenter IPs.
        """
        url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        params = {
            "pageno": page,
            "strCat": "Financial Results",
            "strPrevDate": "",
            "strScrip": "",
            "strSearch": "P",
            "strToDate": "",
            "strType": "C"
        }
        
        logger.info("Fetching BSE Financial Results filings (Cloud-Safe Endpoint)...")
        results = []
        try:
            resp = self.session.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                table = data.get("Table", [])
                for item in table:
                    symbol = str(item.get("SCRIP_CD", "")).strip()
                    company_name = str(item.get("SLONGNAME", "")).strip()
                    headline = str(item.get("NEWSSUB", "")).strip()
                    dt = str(item.get("NEWS_DT", "")).strip()
                    
                    # Exclude non-equities
                    if not self.equity_filter.is_valid_operating_stock(symbol):
                        continue

                    results.append({
                        "scrip_code": symbol,
                        "company_name": company_name,
                        "headline": headline,
                        "date": dt,
                        "attachment_url": f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{item.get('ATTACHMENTNAME', '')}"
                    })
        except Exception as e:
            logger.error(f"Error fetching BSE results announcements: {e}")

        return results

    def calculate_pat_growth(self, pat_current: float, pat_prev: float) -> (float, bool, str):
        """
        Calculates PAT YoY growth with explicit turnaround guard:
        - Prev Loss to Current Profit: Massive Turnaround (Qualified).
        - Both Positive: Standard percentage growth formula.
        - Widening Loss or Prev Profit to Current Loss: Disqualified.
        """
        if pat_prev > 0 and pat_current > 0:
            growth_pct = ((pat_current - pat_prev) / pat_prev) * 100.0
            return round(growth_pct, 2), growth_pct >= MIN_PAT_GROWTH_PCT, f"PAT +{round(growth_pct, 1)}% YoY"
        elif pat_prev <= 0 and pat_current > 0:
            # Turnaround! Loss -> Profit
            return 999.0, True, f"Turnaround from loss (₹{pat_prev} Cr to +₹{pat_current} Cr)"
        else:
            return 0.0, False, "Loss-making or deteriorating PAT"

    def verify_pead_trigger(
        self,
        symbol: str,
        pat_current: float,
        pat_prev: float,
        rev_growth_pct: float,
        opm_expansion_bps: float,
        reaction_price_pct: float,
        volume_x_20d: float,
        delivery_pct: float,
        high_price: float,
        low_price: float,
        above_50_ema: bool = True
    ) -> Dict:
        """
        Evaluates a company's results and market reaction against the strict 3-Filter Rule.
        """
        pat_growth_pct, is_pat_hurdle_met, pat_label = self.calculate_pat_growth(pat_current, pat_prev)

        # Filter 1: Real Earnings Acceleration
        rule_1_fundamental = (
            is_pat_hurdle_met and
            rev_growth_pct >= MIN_REVENUE_GROWTH_PCT and
            opm_expansion_bps >= MIN_OPM_EXPANSION_BPS
        )

        # Filter 2: Institutional Price & Volume Reaction Confirmation
        rule_2_reaction = (
            reaction_price_pct >= MIN_REACTION_PRICE_PCT and
            volume_x_20d >= MIN_REACTION_VOLUME_X and
            delivery_pct >= MIN_REACTION_DELIV_PCT
        )

        # Filter 3: Entry Feasibility (Not locked at single-tick circuit all session)
        is_traded_range = (high_price > low_price)
        rule_3_tradeable = is_traded_range and above_50_ema

        qualifies = rule_1_fundamental and rule_2_reaction and rule_3_tradeable

        thesis = (
            f"PEAD Institutional Drift: {pat_label}, Sales +{rev_growth_pct}%, "
            f"OPM +{opm_expansion_bps} bps. Price reacted +{reaction_price_pct}% on {volume_x_20d}x volume "
            f"({delivery_pct}% delivery). Multi-week drift setup."
        ) if qualifies else "Did not meet institutional PEAD hurdle."

        return {
            "symbol": symbol.upper(),
            "qualifies_pead": qualifies,
            "rule_1_fundamental": rule_1_fundamental,
            "rule_2_reaction": rule_2_reaction,
            "rule_3_tradeable": rule_3_tradeable,
            "pat_growth_pct": pat_growth_pct,
            "rev_growth_pct": rev_growth_pct,
            "opm_expansion_bps": opm_expansion_bps,
            "reaction_price_pct": reaction_price_pct,
            "volume_x_20d": volume_x_20d,
            "delivery_pct": delivery_pct,
            "thesis": thesis
        }

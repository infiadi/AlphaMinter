import logging
from typing import List, Dict, Optional
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

logger = logging.getLogger(__name__)

class PEADScanner:
    """
    Hardened Post-Earnings Announcement Drift (PEAD) Engine:
    Uses the cloud-safe BSE Corporate Announcements API (does not block GitHub Actions / Azure IPs).
    Applies the 3-Filter PEAD Trigger Rule:
      1. Real Earnings Acceleration: PAT YoY >= 30%, Rev YoY >= 15%, OPM Expansion >= 150 bps.
      2. Institutional Reaction Confirmation: Price >= +4%, Vol >= 3x 20D avg, Deliv % >= 50%.
      3. Entry Feasibility: Not locked in upper circuit, trading above 50 EMA.
    """
    def __init__(self, state_store: StateStore = None):
        self.state_store = state_store or StateStore()
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

    def verify_pead_trigger(
        self,
        symbol: str,
        pat_growth_pct: float,
        rev_growth_pct: float,
        opm_expansion_bps: float,
        reaction_price_pct: float,
        volume_x_20d: float,
        delivery_pct: float,
        is_circuit_locked: bool = False,
        above_50_ema: bool = True
    ) -> Dict:
        """
        Evaluates a company's results and market reaction against the strict 3-Filter Rule.
        """
        # Filter 1: Real Earnings Acceleration
        rule_1_fundamental = (
            pat_growth_pct >= MIN_PAT_GROWTH_PCT and
            rev_growth_pct >= MIN_REVENUE_GROWTH_PCT and
            opm_expansion_bps >= MIN_OPM_EXPANSION_BPS
        )

        # Filter 2: Institutional Price & Volume Reaction Confirmation
        rule_2_reaction = (
            reaction_price_pct >= MIN_REACTION_PRICE_PCT and
            volume_x_20d >= MIN_REACTION_VOLUME_X and
            delivery_pct >= MIN_REACTION_DELIV_PCT
        )

        # Filter 3: Entry Feasibility
        rule_3_tradeable = (not is_circuit_locked) and above_50_ema

        qualifies = rule_1_fundamental and rule_2_reaction and rule_3_tradeable

        reasons = []
        if not rule_1_fundamental:
            reasons.append(f"Fundamental growth below threshold (PAT: {pat_growth_pct}%, Rev: {rev_growth_pct}%, OPM: +{opm_expansion_bps} bps)")
        if not rule_2_reaction:
            reasons.append(f"Price/Volume reaction unconfirmed (Price: {reaction_price_pct}%, Vol: {volume_x_20d}x, Deliv: {delivery_pct}%)")
        if not rule_3_tradeable:
            reasons.append("Stock is either locked in upper circuit or below 50 EMA")

        thesis = (
            f"PEAD Institutional Drift: PAT +{pat_growth_pct}% YoY, Sales +{rev_growth_pct}%, "
            f"OPM +{opm_expansion_bps} bps. Price reacted +{reaction_price_pct}% on {volume_x_20d}x volume "
            f"({delivery_pct}% delivery). Multi-week drift setup."
        ) if qualifies else " | ".join(reasons)

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

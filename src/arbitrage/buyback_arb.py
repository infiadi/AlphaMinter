import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from config.settings import RETAIL_INVESTMENT_LIMIT_SAFE
from src.storage.state_store import StateStore

logger = logging.getLogger(__name__)

class BuybackArbitrageCalculator:
    """
    Hardened Share Buyback Tender Arbitrage Engine:
    SEBI mandates 15% reservation for small shareholders (holding <= ₹2 Lakhs on Record Date).
    Safe Sizing: floor(195,000 / CMP) providing a ₹5,000 buffer against price fluctuation.
    Cutoff: Flags T-2 trading days prior to Record Date under T+1 settlement.
    """
    def __init__(self, state_store: StateStore = None):
        self.state_store = state_store or StateStore()

    def calculate_retail_arbitrage(
        self,
        symbol: str,
        cmp: float,
        buyback_price: float,
        record_date_str: Optional[str] = None,
        expected_acceptance_ratio_pct: float = 50.0,
        expected_days_to_payout: int = 45
    ) -> Dict:
        """
        Calculates retail buyback metrics:
        - Safe retail shares = floor(195,000 / CMP)
        - Final buy cutoff date (Record Date minus 2 trading days)
        - Expected profit and annualized return
        - Breakeven exit price on unaccepted shares
        """
        if cmp <= 0 or buyback_price <= cmp:
            return {"symbol": symbol, "is_viable": False, "reason": "CMP >= Buyback price"}

        # Safe retail share sizing with ₹5k buffer
        safe_investment_cap = RETAIL_INVESTMENT_LIMIT_SAFE
        max_shares = int(safe_investment_cap // cmp)
        total_investment = max_shares * cmp

        acceptance_ratio = expected_acceptance_ratio_pct / 100.0
        accepted_shares = int(max_shares * acceptance_ratio)
        unaccepted_shares = max_shares - accepted_shares

        # Cash received from tender
        tender_payout = accepted_shares * buyback_price
        
        # Premium spread
        spread_pct = ((buyback_price - cmp) / cmp) * 100.0

        # Breakeven price on remaining shares so capital is protected
        if unaccepted_shares > 0:
            breakeven_price = (total_investment - tender_payout) / unaccepted_shares
            breakeven_drop_allowed_pct = ((cmp - breakeven_price) / cmp) * 100.0
        else:
            breakeven_price = 0.0
            breakeven_drop_allowed_pct = 100.0

        # Expected gain assuming unaccepted shares sold at CMP
        expected_gain = (accepted_shares * (buyback_price - cmp))
        expected_return_pct = (expected_gain / total_investment) * 100.0
        
        # Annualized return
        annualized_return_pct = (expected_return_pct * (365.0 / expected_days_to_payout))

        # Calculate T-2 Buy Cutoff if record date is known
        final_buy_cutoff = None
        days_remaining_to_buy = None
        if record_date_str:
            try:
                rec_date = datetime.strptime(record_date_str, "%Y-%m-%d").date()
                # Subtract 2 calendar days (adjust for weekend)
                cutoff = rec_date - timedelta(days=2)
                if cutoff.weekday() == 6: # Sunday -> Friday
                    cutoff -= timedelta(days=2)
                elif cutoff.weekday() == 5: # Saturday -> Friday
                    cutoff -= timedelta(days=1)
                final_buy_cutoff = cutoff.isoformat()
                days_remaining_to_buy = (cutoff - datetime.now().date()).days
            except Exception as e:
                logger.warning(f"Failed to parse record date {record_date_str}: {e}")

        is_viable = (expected_return_pct >= 6.0 and spread_pct >= 12.0)

        return {
            "symbol": symbol.upper(),
            "cmp": cmp,
            "buyback_price": buyback_price,
            "spread_pct": round(spread_pct, 2),
            "safe_retail_shares": max_shares,
            "total_investment": round(total_investment, 2),
            "assumed_acceptance_pct": expected_acceptance_ratio_pct,
            "accepted_shares": accepted_shares,
            "unaccepted_shares": unaccepted_shares,
            "expected_gain": round(expected_gain, 2),
            "expected_return_pct": round(expected_return_pct, 2),
            "annualized_return_pct": round(annualized_return_pct, 2),
            "breakeven_price": round(breakeven_price, 2),
            "max_drop_cushion_pct": round(breakeven_drop_allowed_pct, 2),
            "record_date": record_date_str,
            "final_buy_cutoff_t2": final_buy_cutoff,
            "days_remaining_to_buy": days_remaining_to_buy,
            "is_viable": is_viable
        }

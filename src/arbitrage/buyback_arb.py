import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from config.settings import RETAIL_INVESTMENT_LIMIT_SAFE, STCG_TAX_RATE
from src.storage.state_store import StateStore

logger = logging.getLogger(__name__)

class BuybackArbitrageCalculator:
    """
    Hardened Share Buyback Tender Arbitrage Engine (v2.5.0):
    1. SEBI 15% Small Shareholder Reservation: Holdings <= ₹2 Lakhs on Record Date.
    2. Safe Sizing: floor(185,000 / CMP) providing a ~8% cushion against pre-record-date appreciation.
    3. Category Filter: Strictly 'TENDER_OFFER' only (Open-Market buybacks have no small-shareholder reservation).
    4. 3-Tier Acceptance Matrix: Models Conservative (30%), Base (50%), and Bull (75%) acceptance scenarios.
    5. Post-Tax Calculation: Factors in 20% Short-Term Capital Gains (STCG) tax drag under Indian tax law.
    6. Cutoff: Flags T-2 trading days prior to Record Date under T+1 settlement.
    """
    def __init__(self, state_store: StateStore = None):
        self.state_store = state_store or StateStore()

    def calculate_retail_arbitrage(
        self,
        symbol: str,
        cmp: float,
        buyback_price: float,
        buyback_type: str = "TENDER_OFFER",
        record_date_str: Optional[str] = None,
        expected_acceptance_ratio_pct: float = 50.0,
        expected_days_to_payout: int = 45
    ) -> Dict:
        """
        Calculates retail buyback metrics:
        - Strict Guard: Only 'TENDER_OFFER' is viable (Open Market has NO 15% retail reservation).
        - Safe retail shares = floor(185,000 / CMP)
        - Final buy cutoff date (Record Date minus 2 trading days)
        - 3-tier acceptance matrix (30%, 50%, 75%) with post-tax STCG returns
        - Breakeven exit price on unaccepted shares
        """
        # Guard: Reject Open Market buybacks (SEBI reintroduced without retail reservation)
        if buyback_type.upper() != "TENDER_OFFER":
            return {
                "symbol": symbol.upper(),
                "is_viable": False,
                "reason": f"Skipped: Buyback type '{buyback_type}' has no SEBI 15% Small Shareholder reservation."
            }

        if cmp <= 0 or buyback_price <= cmp:
            return {"symbol": symbol, "is_viable": False, "reason": "CMP >= Buyback price"}

        # Safe retail share sizing with ₹1.85L ceiling (~8% cushion to avoid ₹2L boundary breach)
        safe_investment_cap = RETAIL_INVESTMENT_LIMIT_SAFE
        max_shares = int(safe_investment_cap // cmp)
        total_investment = max_shares * cmp

        # Premium spread
        spread_pct = ((buyback_price - cmp) / cmp) * 100.0

        # Build 3-Tier Acceptance Matrix: 30% (Bear/Conservative), 50% (Base), 75% (Bull)
        acceptance_scenarios = {}
        for pct in [30.0, 50.0, 75.0]:
            acc_sh = int(max_shares * (pct / 100.0))
            unacc_sh = max_shares - acc_sh
            gross_gain = acc_sh * (buyback_price - cmp)
            net_gain = gross_gain * (1.0 - STCG_TAX_RATE)
            net_return_pct = (net_gain / total_investment) * 100.0 if total_investment > 0 else 0.0
            net_annualized_pct = net_return_pct * (365.0 / expected_days_to_payout)
            
            if unacc_sh > 0:
                tender_payout = acc_sh * buyback_price
                be_price = (total_investment - tender_payout) / unacc_sh
                drop_cushion_pct = ((cmp - be_price) / cmp) * 100.0
            else:
                be_price = 0.0
                drop_cushion_pct = 100.0

            acceptance_scenarios[f"{int(pct)}%"] = {
                "accepted_shares": acc_sh,
                "unaccepted_shares": unacc_sh,
                "gross_gain_rs": round(gross_gain, 2),
                "net_gain_rs": round(net_gain, 2),
                "net_return_pct": round(net_return_pct, 2),
                "net_annualized_pct": round(net_annualized_pct, 2),
                "breakeven_price": round(be_price, 2),
                "drop_cushion_pct": round(drop_cushion_pct, 2)
            }

        # Base case (50% acceptance) calculations
        base_scenario = acceptance_scenarios.get("50%")
        
        # Calculate T-2 Buy Cutoff if record date is known
        final_buy_cutoff = None
        days_remaining_to_buy = None
        if record_date_str:
            try:
                rec_date = datetime.strptime(record_date_str, "%Y-%m-%d").date()
                cutoff = rec_date - timedelta(days=2)
                if cutoff.weekday() == 6:   # Sunday -> Friday
                    cutoff -= timedelta(days=2)
                elif cutoff.weekday() == 5: # Saturday -> Friday
                    cutoff -= timedelta(days=1)
                final_buy_cutoff = cutoff.isoformat()
                days_remaining_to_buy = (cutoff - datetime.now().date()).days
            except Exception as e:
                logger.warning(f"Failed to parse record date {record_date_str}: {e}")

        # Viable if base scenario net return >= 5.0% and spread >= 12.0%
        is_viable = (base_scenario["net_return_pct"] >= 5.0 and spread_pct >= 12.0)

        return {
            "symbol": symbol.upper(),
            "cmp": cmp,
            "buyback_price": buyback_price,
            "spread_pct": round(spread_pct, 2),
            "safe_retail_shares": max_shares,
            "total_investment": round(total_investment, 2),
            "base_case": base_scenario,
            "scenarios": acceptance_scenarios,
            "record_date": record_date_str,
            "final_buy_cutoff_t2": final_buy_cutoff,
            "days_remaining_to_buy": days_remaining_to_buy,
            "is_viable": is_viable
        }

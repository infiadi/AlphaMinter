import os
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import re
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.local.custom_screener_parser import CustomScreenerParser
from src.scanners.equity_filter import EquityFilter
from src.storage.state_store import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("prepare_screener_batch")

CONFIG_PATH = BASE_DIR / "config" / "screens.json"
HISTORY_PATH = BASE_DIR / "data" / "reviewed_history.json"
QUEUES_DIR = BASE_DIR / "data" / "prepared_queues"


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing config file: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_history() -> Dict[str, Any]:
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Error loading {HISTORY_PATH}: {e}")
    return {}


def is_on_review_cooldown(
    symbol: str,
    current_screen_count: int,
    history: Dict[str, Any],
    cooldown_days: int = 45
) -> Tuple[bool, str]:
    """
    Checks if stock was reviewed within cooldown_days:
    If current_screen_count > historical_screen_count, allows re-evaluation (confluence increased).
    Otherwise returns True to skip.
    """
    if cooldown_days <= 0:
        return False, "Cooldown disabled (0 days)"

    record = history.get(symbol.upper())
    if not record:
        return False, "New ticker (never reviewed)"

    last_reviewed_str = record.get("last_reviewed_at")
    if not last_reviewed_str:
        return False, "No previous review timestamp"

    try:
        # Handle ISO timestamp or YYYY-MM-DD
        if "T" in last_reviewed_str:
            last_dt = datetime.fromisoformat(last_reviewed_str.replace("Z", "+00:00")).replace(tzinfo=None)
        else:
            last_dt = datetime.strptime(last_reviewed_str[:10], "%Y-%m-%d")

        days_ago = (datetime.now() - last_dt).days
        prior_hits = record.get("screen_count", 1)

        if days_ago < cooldown_days:
            if current_screen_count > prior_hits:
                return False, f"Cooldown bypassed: screen confluence increased ({prior_hits} -> {current_screen_count})"
            return True, f"Reviewed {days_ago} days ago (cooldown {cooldown_days}d, hits: {prior_hits})"
    except Exception as e:
        logger.warning(f"Error parsing date for {symbol}: {e}")

    return False, "Cooldown expired"


def extract_screen_metric(screen_metrics: Dict[str, Any], keywords: List[str]) -> Optional[float]:
    """Extracts a numeric metric from screen table columns by fuzzy matching header keywords."""
    for k, v in screen_metrics.items():
        k_clean = re.sub(r"[^a-zA-Z0-9%]", "", k).lower()
        for kw in keywords:
            kw_clean = re.sub(r"[^a-zA-Z0-9%]", "", kw).lower()
            if kw_clean in k_clean and isinstance(v, (int, float)):
                return float(v)
    return None


def evaluate_screen_level_flags(
    cand: Dict[str, Any],
    global_filters: Dict[str, Any],
    screen_overrides_list: List[Dict[str, Any]]
) -> Tuple[List[str], Dict[str, str]]:
    """
    Evaluates visible screen columns (Market Cap, ROCE/ROE, Qtr Profit Var, Sales Growth, 3Y/5Y Trends)
    directly at the screen level with ZERO extra network requests.
    Supports:
    - Financial stock dynamics (evaluates ROE instead of ROCE)
    - Multi-year CAGR buffer: 1 weak quarter does not drop a strong multi-year compounder
    - Market cap floor (₹5,000 Cr default)
    """
    flags: List[str] = []
    reasons: Dict[str, str] = {}
    m = cand.get("screen_metrics", {})
    if not m:
        return flags, reasons

    exempt_flags = set()
    for ov in screen_overrides_list:
        for ex in ov.get("exempt_flags", []):
            exempt_flags.add(ex)

    # 1. Market Cap (>= ₹5,000 Cr default)
    global_min_mcap = float(global_filters.get("min_market_cap_cr", 5000))
    screen_mcaps = [ov.get("min_market_cap_cr") for ov in screen_overrides_list if ov.get("min_market_cap_cr") is not None]
    effective_min_mcap = max(screen_mcaps) if screen_mcaps else global_min_mcap
    mcap = extract_screen_metric(m, ["mar cap", "market cap"])
    if mcap is not None and mcap < effective_min_mcap:
        flags.append("NEG_LOW_MCAP")
        reasons["NEG_LOW_MCAP"] = f"Screen M.Cap ₹{mcap:.0f} Cr < minimum required ₹{effective_min_mcap:.0f} Cr"

    # Identify if Financial Institution (Bank, NBFC, Housing, AMC, Insurance)
    cand_name = cand.get("name", "")
    cand_sym = cand.get("symbol", "")
    is_fin_screen = any(kw in f"{cand_name} {cand_sym}".lower() for kw in [
        "bank", "finance", "nbfc", "housing", "fin", "capital", "insurance", "amc", "asset management", "securities", "invest", "holding"
    ])

    # 2. Capital Efficiency (ROCE for non-financials, ROE for financials)
    if "NEG_ROCE" not in exempt_flags:
        if is_fin_screen:
            min_roe = float(global_filters.get("min_roe_financials_pct", 10.0))
            roe = extract_screen_metric(m, ["roe"])
            if roe is not None and roe < min_roe:
                flags.append("NEG_ROCE")
                reasons["NEG_ROCE"] = f"Financial stock ROE {roe}% < {min_roe}% threshold"
        else:
            global_min_roce = float(global_filters.get("min_roce_pct", 10.0))
            screen_roces = [ov.get("min_roce_pct") for ov in screen_overrides_list if ov.get("min_roce_pct") is not None]
            effective_min_roce = max(screen_roces) if screen_roces else global_min_roce
            roce = extract_screen_metric(m, ["roce"])
            if roce is not None and roce < effective_min_roce:
                flags.append("NEG_ROCE")
                reasons["NEG_ROCE"] = f"Screen ROCE {roce}% < {effective_min_roce}%"

    # Multi-year growth resilience check:
    # 1 YoY/QoQ negative quarter does not make a stock bad if 3Y or 5Y CAGR is strong!
    profit_3y = extract_screen_metric(m, ["profit var 3yrs", "profit var 3"])
    sales_3y = extract_screen_metric(m, ["sales var 3yrs", "sales var 3"])
    profit_5y = extract_screen_metric(m, ["profit var 5yrs", "profit var 5"])
    sales_5y = extract_screen_metric(m, ["sales var 5yrs", "sales var 5"])

    has_multi_year_cushion = (
        (profit_3y is not None and profit_3y >= 15.0) or
        (profit_5y is not None and profit_5y >= 15.0) or
        (sales_3y is not None and sales_3y >= 12.0) or
        (sales_5y is not None and sales_5y >= 15.0)
    )

    # 3. Latest Qtr YoY PAT Growth
    if "NEG_LATEST_QTR_PAT" not in exempt_flags:
        global_min_pat = float(global_filters.get("min_latest_qtr_yoy_pat_pct", -20.0))
        pat_growth = extract_screen_metric(m, ["qtr profit var", "profit var%"])
        if pat_growth is not None and pat_growth < global_min_pat:
            if has_multi_year_cushion:
                logger.info(f"Screen {cand_sym}: Transient Qtr PAT dip ({pat_growth}%) cushioned by multi-year CAGR (3Y Profit: {profit_3y}%). Not penalized.")
            else:
                flags.append("NEG_LATEST_QTR_PAT")
                reasons["NEG_LATEST_QTR_PAT"] = f"Screen Qtr PAT growth {pat_growth}% < {global_min_pat}% (no multi-year cushion)"

    # 4. Latest Qtr YoY Sales Growth
    if "NEG_LATEST_QTR_SALES" not in exempt_flags:
        global_min_sales = float(global_filters.get("min_latest_qtr_yoy_sales_pct", -10.0))
        # Prioritize exact quarterly ratio if available: 'Sales latest quarter / Sales preceding year quarter'
        sales_ratio = extract_screen_metric(m, ["sales latest quarter", "sales preceding year"])
        if sales_ratio is not None:
            sales_growth = round((sales_ratio - 1.0) * 100.0, 2)
        else:
            sales_growth = extract_screen_metric(m, ["sales growth"])

        if sales_growth is not None and sales_growth < global_min_sales:
            if has_multi_year_cushion:
                logger.info(f"Screen {cand_sym}: Transient Qtr Sales dip ({sales_growth:.1f}%) cushioned by multi-year CAGR (3Y Sales: {sales_3y}%). Not penalized.")
            else:
                flags.append("NEG_LATEST_QTR_SALES")
                reasons["NEG_LATEST_QTR_SALES"] = f"Screen Sales growth {sales_growth:.1f}% < {global_min_sales}% (no multi-year cushion)"

    # 5. Screen-Level Cash Conversion (3Y CFO / Net Profit)
    # Evaluates cash conversion directly at the screen table with 0 extra requests
    if "FORENSIC_LOW_CFO" not in exempt_flags and not is_fin_screen:
        min_cfo_to_pat = float(global_filters.get("min_cfo_to_pat_3y", 0.60))
        screen_cfos = [ov.get("min_cfo_to_pat_3y") for ov in screen_overrides_list if ov.get("min_cfo_to_pat_3y") is not None]
        effective_min_cfo = max(screen_cfos) if screen_cfos else min_cfo_to_pat
        cfo_pat_screen = extract_screen_metric(m, ["operating cash flow 3years", "cash flow 3years / net profit", "cash flow 3years"])
        if cfo_pat_screen is not None:
            if cfo_pat_screen < 0:
                flags.append("FORENSIC_LOW_CFO")
                reasons["FORENSIC_LOW_CFO"] = f"Screen 3Y CFO/PAT negative ({cfo_pat_screen:.2f}x). Cash drain / working capital bleed."
            elif cfo_pat_screen < effective_min_cfo:
                flags.append("FORENSIC_LOW_CFO")
                reasons["FORENSIC_LOW_CFO"] = f"Screen 3Y CFO/PAT {cfo_pat_screen:.2f}x < required {effective_min_cfo:.2f}x."

    # 6. Screen-Level Other Income / PBT
    # Evaluates non-core treasury dependency directly at the screen table with 0 extra requests
    if "FORENSIC_OTHER_INC" not in exempt_flags and not is_fin_screen:
        other_inc_pbt = extract_screen_metric(m, ["other income last year", "other income / profit before tax", "other income"])
        if other_inc_pbt is not None and other_inc_pbt > 0.20:
            flags.append("FORENSIC_OTHER_INC")
            reasons["FORENSIC_OTHER_INC"] = f"Screen Other Income/PBT {other_inc_pbt:.2f}x > 0.20 threshold (earnings quality / treasury reliance)."

    return flags, reasons


def evaluate_negative_flags(
    stock_candidate: Dict[str, Any],
    global_filters: Dict[str, Any],
    screen_overrides_list: List[Dict[str, Any]]
) -> Tuple[List[str], Dict[str, Any]]:
    """
    Evaluates the 7 deterministic negative checks against global filters & screen overrides:
    1. NEG_ROCE: roce_pct < effective_min_roce
    2. NEG_CASH_CONVERSION: cfo_to_pat_3y < effective_min_cfo_to_pat
    3. NEG_HIGH_DEBT: debt_to_equity > max_debt_to_equity (skipped for Banks/NBFC)
    4. NEG_PROMOTER_PLEDGE: promoter_pledge_pct > max_promoter_pledge_pct OR promoter_qoq_change_pct < -2.0
    5. NEG_LATEST_QTR_PAT: latest_qtr_yoy_pat_pct < min_latest_qtr_yoy_pat_pct
    6. NEG_LATEST_QTR_SALES: latest_qtr_yoy_sales_pct < min_latest_qtr_yoy_sales_pct
    7. NEG_OPM_CONTRACTION: latest_qtr_yoy_opm_bps < -300
    """
    flags: List[str] = []
    reasons: Dict[str, str] = {}

    dossier = stock_candidate.get("dossier", {})
    metrics = dossier.get("derived_metrics", {})
    top_ratios = dossier.get("top_ratios", {})

    # Collect all exempt flags across matched screens
    exempt_flags = set()
    for ov in screen_overrides_list:
        for ex in ov.get("exempt_flags", []):
            exempt_flags.add(ex)

    is_fin = metrics.get("is_financial_sector", False)

    # 0. NEG_NET_LOSS: Absolute loss-making or cash-burning traps (e.g. Vodafone Idea)
    pl = dossier.get("profit_loss", {}).get("data", {})
    net_profit_row = pl.get("Net Profit") or []
    latest_pat = net_profit_row[-1] if net_profit_row else None
    pat_3y_sum = sum([v for v in net_profit_row[-3:] if v is not None]) if len(net_profit_row) >= 3 else None
    if (latest_pat is not None and latest_pat <= 0) or (pat_3y_sum is not None and pat_3y_sum <= 0):
        flags.append("NEG_NET_LOSS")
        reasons["NEG_NET_LOSS"] = f"Negative net profit (Latest PAT: ₹{latest_pat} Cr, 3Y Sum: ₹{pat_3y_sum} Cr). Disqualified as loss-making / value trap."

    # 1. NEG_ROCE (Evaluated via ROE for Banks/NBFCs)
    if "NEG_ROCE" not in exempt_flags:
        global_min_roce = float(global_filters.get("min_roce_pct", 10.0))
        screen_roces = [ov.get("min_roce_pct") for ov in screen_overrides_list if ov.get("min_roce_pct") is not None]
        effective_min_roce = max(screen_roces) if screen_roces else global_min_roce

        roce = metrics.get("roce_pct") or top_ratios.get("ROCE")
        if is_fin:
            # Banks operate with high leverage, so ROE is the primary capital efficiency metric
            roe = metrics.get("roe_pct") or top_ratios.get("ROE")
            if roe is not None and roe < 12.0:
                flags.append("NEG_ROCE")
                reasons["NEG_ROCE"] = f"Financial institution ROE {roe}% < 12.0%"
        elif roce is not None and roce < effective_min_roce:
            flags.append("NEG_ROCE")
            reasons["NEG_ROCE"] = f"ROCE {roce}% < minimum required {effective_min_roce}%"

    # 2. Cash Conversion: MERGED into FORENSIC_LOW_CFO in evaluate_forensic_flags()
    # to eliminate double-counting on a single cash-flow conversion issue.

    # 3. NEG_HIGH_DEBT (Skipped if bank/finance/nbfc)
    if "NEG_HIGH_DEBT" not in exempt_flags:
        if not metrics.get("is_financial_sector", False):
            global_max_debt = float(global_filters.get("max_debt_to_equity", 1.5))
            screen_debts = [ov.get("max_debt_to_equity") for ov in screen_overrides_list if ov.get("max_debt_to_equity") is not None]
            effective_max_debt = min(screen_debts) if screen_debts else global_max_debt

            debt_eq = metrics.get("debt_to_equity")
            if debt_eq is not None and debt_eq > effective_max_debt:
                flags.append("NEG_HIGH_DEBT")
                reasons["NEG_HIGH_DEBT"] = f"Debt/Equity {debt_eq:.2f} > ceiling {effective_max_debt:.2f}"

    # 4. NEG_PROMOTER_PLEDGE
    if "NEG_PROMOTER_PLEDGE" not in exempt_flags:
        global_max_pledge = float(global_filters.get("max_promoter_pledge_pct", 10.0))
        pledge_pct = metrics.get("promoter_pledge_pct", 0.0) or 0.0
        promoter_qoq = metrics.get("promoter_qoq_change_pct")

        if pledge_pct > global_max_pledge:
            flags.append("NEG_PROMOTER_PLEDGE")
            reasons["NEG_PROMOTER_PLEDGE"] = f"Promoter pledge {pledge_pct}% > max allowed {global_max_pledge}%"
        elif promoter_qoq is not None and promoter_qoq < -3.0:
            flags.append("NEG_PROMOTER_PLEDGE")
            reasons["NEG_PROMOTER_PLEDGE"] = f"Promoter holding declined by {abs(promoter_qoq)}% QoQ (>-3.0% threshold)"

    # Multi-year growth resilience check:
    # 1 YoY/QoQ negative quarter does not make a stock bad if 3Y or 5Y CAGR is strong!
    growth = dossier.get("compounded_growth", {})
    def parse_growth(section: str, period: str) -> Optional[float]:
        val = growth.get(section, {}).get(period, "")
        if isinstance(val, str) and "%" in val:
            try:
                return float(val.replace("%", "").strip())
            except ValueError:
                pass
        return None

    profit_3y_cagr = parse_growth("Compounded Profit Growth", "3 Years")
    profit_5y_cagr = parse_growth("Compounded Profit Growth", "5 Years")
    sales_3y_cagr = parse_growth("Compounded Sales Growth", "3 Years")
    sales_5y_cagr = parse_growth("Compounded Sales Growth", "5 Years")

    has_multi_year_cushion = (
        (profit_3y_cagr is not None and profit_3y_cagr >= 15.0) or
        (profit_5y_cagr is not None and profit_5y_cagr >= 15.0) or
        (sales_3y_cagr is not None and sales_3y_cagr >= 12.0) or
        (sales_5y_cagr is not None and sales_5y_cagr >= 15.0)
    )

    # 5. NEG_LATEST_QTR_PAT
    if "NEG_LATEST_QTR_PAT" not in exempt_flags:
        global_min_pat = float(global_filters.get("min_latest_qtr_yoy_pat_pct", -20.0))
        pat_growth = metrics.get("latest_qtr_yoy_pat_pct")
        if pat_growth is not None and pat_growth < global_min_pat:
            if has_multi_year_cushion:
                logger.info(f"{stock_candidate.get('symbol')}: Qtr PAT dip ({pat_growth}%) cushioned by multi-year CAGR (3Y Profit: {profit_3y_cagr}%). Not penalized.")
            else:
                flags.append("NEG_LATEST_QTR_PAT")
                reasons["NEG_LATEST_QTR_PAT"] = f"Latest Qtr YoY PAT growth {pat_growth}% < min allowed {global_min_pat}%"

    # 6. NEG_LATEST_QTR_SALES
    if "NEG_LATEST_QTR_SALES" not in exempt_flags:
        global_min_sales = float(global_filters.get("min_latest_qtr_yoy_sales_pct", -10.0))
        sales_growth = metrics.get("latest_qtr_yoy_sales_pct")
        if sales_growth is not None and sales_growth < global_min_sales:
            if has_multi_year_cushion:
                logger.info(f"{stock_candidate.get('symbol')}: Qtr Sales dip ({sales_growth}%) cushioned by multi-year CAGR. Not penalized.")
            else:
                flags.append("NEG_LATEST_QTR_SALES")
                reasons["NEG_LATEST_QTR_SALES"] = f"Latest Qtr YoY Sales growth {sales_growth}% < min allowed {global_min_sales}%"

    # 7. NEG_OPM_CONTRACTION
    if "NEG_OPM_CONTRACTION" not in exempt_flags:
        yoy_opm_bps = metrics.get("latest_qtr_yoy_opm_bps")
        if yoy_opm_bps is not None and yoy_opm_bps < -300:
            if has_multi_year_cushion:
                logger.info(f"{stock_candidate.get('symbol')}: OPM contraction ({yoy_opm_bps:.0f} bps) buffered by strong 3Y/5Y growth. Not penalized.")
            else:
                flags.append("NEG_OPM_CONTRACTION")
                reasons["NEG_OPM_CONTRACTION"] = f"Operating Margin contracted by {abs(yoy_opm_bps):.0f} bps YoY (>300 bps contraction)"

    # Also check Market Cap floor (>= ₹5,000 Cr default)
    global_min_mcap = float(global_filters.get("min_market_cap_cr", 5000))
    screen_mcaps = [ov.get("min_market_cap_cr") for ov in screen_overrides_list if ov.get("min_market_cap_cr") is not None]
    effective_min_mcap = max(screen_mcaps) if screen_mcaps else global_min_mcap

    mcap = metrics.get("market_cap_cr") or top_ratios.get("Market Cap")
    if mcap is not None and mcap < effective_min_mcap:
        flags.append("NEG_LOW_MCAP")
        reasons["NEG_LOW_MCAP"] = f"Market Cap ₹{mcap:.0f} Cr < minimum required ₹{effective_min_mcap:.0f} Cr"

    return flags, reasons


def evaluate_forensic_flags(
    stock_candidate: Dict[str, Any],
    global_filters: Optional[Dict[str, Any]] = None,
    screen_overrides_list: Optional[List[Dict[str, Any]]] = None
) -> Tuple[List[str], Dict[str, str]]:
    """
    Evaluates 4 deterministic forensic accounting flags with BFSI auto-exemption:
    1. FORENSIC_LOW_CFO: Unified cash-flow conversion gate (3Y CFO/EBITDA < 0.65 or CFO/PAT < 0.60)
    2. FORENSIC_DEBTOR_BLOAT: Debtor Days jumped > 15 days vs 3Y average AND latest Debtor Days > 60d
    3. FORENSIC_CWIP_TRAP: CWIP/Net Fixed Assets > 0.30 for 2+ consecutive years (exempt: Power/Utilities/Telecom/Infra)
    4. FORENSIC_OTHER_INC: Trailing 12-Month Other Income / PBT > 0.20

    Returns:
        (forensic_flags, forensic_flag_reasons)
    """
    flags: List[str] = []
    reasons: Dict[str, str] = {}

    dossier = stock_candidate.get("dossier", {})
    metrics = dossier.get("derived_metrics", {})

    # BFSI Auto-Exemption:
    # Banks, NBFCs, Housing Finance, AMCs, Insurance, and Broking firms
    # follow fundamentally different cash-flow and working-capital accounting.
    if metrics.get("is_financial_sector", False):
        return flags, reasons

    # 1. FORENSIC_LOW_CFO (Unified cash conversion check - replaces duplicate NEG_CASH_CONVERSION)
    global_min_cfo = float(global_filters.get("min_cfo_to_pat_3y", 0.60)) if global_filters else 0.60
    screen_cfos = [ov.get("min_cfo_to_pat_3y") for ov in (screen_overrides_list or []) if ov.get("min_cfo_to_pat_3y") is not None]
    effective_min_cfo = max(screen_cfos) if screen_cfos else global_min_cfo

    cfo_ebitda = metrics.get("cfo_to_ebitda_3y")
    cfo_pat = metrics.get("cfo_to_pat_3y")
    is_low_cfo = False
    if cfo_pat is not None and cfo_pat < 0:
        is_low_cfo = True
    elif cfo_pat is not None and cfo_pat < effective_min_cfo:
        if cfo_ebitda is None or cfo_ebitda < 0.65:
            is_low_cfo = True

    if is_low_cfo:
        flags.append("FORENSIC_LOW_CFO")
        reasons["FORENSIC_LOW_CFO"] = (
            f"Poor cash conversion: 3Y CFO/PAT={cfo_pat if cfo_pat is not None else 'N/A'} (<{effective_min_cfo:.2f}) "
            f"and CFO/EBITDA={cfo_ebitda if cfo_ebitda is not None else 'N/A'} (<0.65)"
        )

    # 2. FORENSIC_DEBTOR_BLOAT (from static #ratios table)
    debtor_delta = metrics.get("debtor_days_delta")
    debtor_latest = metrics.get("debtor_days_latest")
    if debtor_delta is not None and debtor_delta > 15.0 and debtor_latest is not None and debtor_latest > 60.0:
        flags.append("FORENSIC_DEBTOR_BLOAT")
        reasons["FORENSIC_DEBTOR_BLOAT"] = (
            f"Debtor days jumped by {debtor_delta:+.1f} days vs 3Y average to {debtor_latest:.0f} days (>60d)"
        )

    # 3. FORENSIC_CWIP_TRAP (Persistent CWIP without asset conversion)
    cwip_latest = metrics.get("cwip_ratio_latest")
    cwip_prior = metrics.get("cwip_ratio_prior")

    # Exempt capital-intensive infrastructure / power / utilities / telecom
    sec_ind = f"{dossier.get('sector', '')} {dossier.get('industry', '')} {stock_candidate.get('name', '')}".lower()
    is_infra_exempt = any(k in sec_ind for k in ["power", "utilities", "telecom", "infrastructure", "infra", "port", "airport", "railway"])

    if not is_infra_exempt:
        if cwip_latest is not None and cwip_latest > 0.30 and cwip_prior is not None and cwip_prior > 0.30:
            flags.append("FORENSIC_CWIP_TRAP")
            reasons["FORENSIC_CWIP_TRAP"] = (
                f"Persistent CWIP bloat: CWIP/Net Fixed Assets > 30% for 2+ consecutive years "
                f"({cwip_prior*100:.0f}% -> {cwip_latest*100:.0f}%)"
            )

    # 4. FORENSIC_OTHER_INC (Non-core profit distortion)
    other_inc_ratio = metrics.get("other_income_to_pbt_ttm")
    if other_inc_ratio is not None and other_inc_ratio > 0.20:
        flags.append("FORENSIC_OTHER_INC")
        reasons["FORENSIC_OTHER_INC"] = (
            f"High non-operating other income: {other_inc_ratio*100:.1f}% of PBT (>20% threshold)"
        )

    return flags, reasons


def fetch_technofundamental_batch(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetches OHLCV and calculates Mark Minervini trend stage & Mansfield RS (50D) vs benchmark.
    Guardrails:
    - Index fallback: ^CRSLDX (Nifty 500) -> ^NSEI (Nifty 50)
    - Ticker fallback: <SYMBOL>.NS -> <SYMBOL>.BO (for BSE-only stocks)
    """
    results: Dict[str, Dict[str, Any]] = {}
    if not symbols:
        return results

    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        logger.warning("yfinance or pandas not installed. Technofundamental metrics skipped.")
        for s in symbols:
            results[s] = {"trend_stage": "UNKNOWN", "mansfield_rs_50d": None, "technical_summary": "yfinance not installed"}
        return results

    # 1. Fetch Benchmark Index: ^CRSLDX with fallback to ^NSEI
    bench_df = pd.DataFrame()
    try:
        bench_df = yf.download("^CRSLDX", period="1y", progress=False)
        if bench_df.empty or len(bench_df) < 50:
            logger.info("Index ^CRSLDX unavailable or short; falling back to ^NSEI.")
            bench_df = yf.download("^NSEI", period="1y", progress=False)
    except Exception as e:
        logger.warning(f"Error downloading ^CRSLDX: {e}; falling back to ^NSEI.")
        try:
            bench_df = yf.download("^NSEI", period="1y", progress=False)
        except Exception as e2:
            logger.warning(f"Error downloading ^NSEI fallback: {e2}")

    bench_close = pd.Series(dtype=float)
    if not bench_df.empty and "Close" in bench_df:
        bench_close = bench_df["Close"].squeeze()

    # 2. Fetch Stock Price Histories with .NS -> .BO Fallback
    for sym in symbols:
        stock_df = pd.DataFrame()
        # Primary: NSE
        try:
            stock_df = yf.download(f"{sym}.NS", period="1y", progress=False)
        except Exception:
            stock_df = pd.DataFrame()

        # Fallback: BSE
        if stock_df.empty or len(stock_df) < 20:
            try:
                logger.info(f"{sym}.NS returned no/little data; falling back to {sym}.BO (BSE).")
                stock_df = yf.download(f"{sym}.BO", period="1y", progress=False)
            except Exception as e:
                logger.warning(f"Failed to fetch {sym}.BO: {e}")

        if stock_df.empty or len(stock_df) < 20:
            results[sym] = {
                "trend_stage": "UNKNOWN",
                "mansfield_rs_50d": None,
                "technical_summary": "Insufficient price data on NSE/BSE"
            }
            continue

        try:
            close = stock_df["Close"].squeeze()
            cmp = float(close.iloc[-1])
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            ema150 = float(close.ewm(span=150, adjust=False).mean().iloc[-1]) if len(close) >= 150 else None
            ema200_s = close.ewm(span=200, adjust=False).mean() if len(close) >= 200 else None
            ema200 = float(ema200_s.iloc[-1]) if ema200_s is not None else None

            ema200_rising = False
            if ema200_s is not None and len(ema200_s) >= 20:
                ema200_rising = float(ema200_s.iloc[-1]) >= float(ema200_s.iloc[-20])

            # Classify Trend Stage
            if ema150 is not None and ema200 is not None:
                if cmp > ema50 > ema150 > ema200 and ema200_rising:
                    stage = "STAGE_2_UPTREND"
                elif cmp < ema50 < ema200 and not ema200_rising:
                    stage = "STAGE_4_DOWNTREND"
                elif cmp >= ema20:
                    stage = "STAGE_1_BASE"
                else:
                    stage = "CONSOLIDATION_PULLBACK"
            else:
                stage = "STAGE_1_BASE" if cmp >= ema20 else "CONSOLIDATION_PULLBACK"

            # Compute Mansfield RS (50D)
            mansfield_rs = None
            if not bench_close.empty and len(bench_close) >= 50:
                comb = pd.DataFrame({"stock": close, "bench": bench_close}).dropna()
                if len(comb) >= 50:
                    ratio = comb["stock"] / comb["bench"]
                    sma50 = ratio.rolling(50).mean()
                    if not pd.isna(sma50.iloc[-1]) and sma50.iloc[-1] != 0:
                        mansfield_rs = round(float(((ratio.iloc[-1] / sma50.iloc[-1]) - 1.0) * 100.0), 2)

            summary = f"Stage: {stage} | CMP: {cmp:.1f} vs EMA20: {ema20:.1f}, EMA50: {ema50:.1f}"
            if ema200 is not None:
                summary += f", EMA200: {ema200:.1f} ({'Rising' if ema200_rising else 'Falling'})"
            if mansfield_rs is not None:
                summary += f" | Mansfield RS (50D): {mansfield_rs:+.2f}%"

            results[sym] = {
                "trend_stage": stage,
                "mansfield_rs_50d": mansfield_rs,
                "cmp": round(cmp, 1),
                "ema20": round(ema20, 1),
                "ema50": round(ema50, 1),
                "ema200": round(ema200, 1) if ema200 else None,
                "ema200_rising": ema200_rising,
                "technical_summary": summary
            }
        except Exception as e:
            logger.warning(f"Error calculating technicals for {sym}: {e}")
            results[sym] = {
                "trend_stage": "UNKNOWN",
                "mansfield_rs_50d": None,
                "technical_summary": f"Calculation error: {e}"
            }

    return results


def generate_analyst_prompt(candidate: Dict[str, Any]) -> str:
    """Pre-renders an institutional, ready-to-run BEAR-FIRST prompt for @equity-analyst."""
    sym = candidate["symbol"]
    name = candidate["name"]
    dossier = candidate.get("dossier", {})
    metrics = dossier.get("derived_metrics", {})
    top_ratios = dossier.get("top_ratios", {})
    growth = dossier.get("compounded_growth", {})
    screens = candidate.get("matched_screens", [])
    rubrics = candidate.get("rubrics", [])
    flags = candidate.get("negative_flags", [])
    forensic_flags = candidate.get("forensic_flags", [])
    needs_review = candidate.get("needs_agent_review", False)
    review_reason = candidate.get("review_reason", "")
    sm = candidate.get("screen_metrics", {})
    tech = candidate.get("technicals", {})

    down_52w = sm.get("Down%") or sm.get("Down %") or "N/A"
    down_ath = sm.get("Down ATH%") or sm.get("Down ATH %") or "N/A"

    prompt = f"""You are analyzing candidate stock {sym} ({name}).
Evaluate this single stock using its pre-prepared quantitative dossier below.
Follow the STRICT BEAR-FIRST EVALUATION PROTOCOL.

### CANDIDATE DOSSIER:
- Symbol: {sym} | Company: {name}
- Matched Screens: {', '.join(screens)} (Hits: {candidate.get('screen_count', 1)})
- Rubrics: {' '.join(rubrics)}
- Market Cap: ₹{metrics.get('market_cap_cr', top_ratios.get('Market Cap', 'N/A'))} Cr | CMP: ₹{metrics.get('current_price', top_ratios.get('Current Price', 'N/A'))}
- Drawdown: Down from 52W High: {down_52w}% | Down from ATH: {down_ath}%
- Valuation & Quality: Stock P/E: {metrics.get('pe', top_ratios.get('Stock P/E', 'N/A'))}x | ROCE: {metrics.get('roce_pct', top_ratios.get('ROCE', 'N/A'))}% | ROE: {metrics.get('roe_pct', top_ratios.get('ROE', 'N/A'))}%
- Cash Conversion: 3Y CFO / PAT: {metrics.get('cfo_to_pat_3y', 'N/A')}x | 3Y CFO / EBITDA: {metrics.get('cfo_to_ebitda_3y', 'N/A')}x
- Working Capital: Debtor Days: {metrics.get('debtor_days_latest', 'N/A')} (Delta vs 3Y Avg: {metrics.get('debtor_days_delta', 'N/A')}d)
- Capex & Leverage: CWIP / Net Block: {metrics.get('cwip_ratio_latest', 'N/A')}x | Debt to Equity: {metrics.get('debt_to_equity', 'N/A')}
- Non-Core Income: Other Income / PBT: {metrics.get('other_income_to_pbt_ttm', 'N/A')}
- Quarterly Sales Expansion: {metrics.get('latest_qtr_yoy_sales_pct', 'N/A')}% YoY (Quarterly Ratio: {metrics.get('sales_qtr_ratio', 'N/A')})
- Promoter: Pledge: {metrics.get('promoter_pledge_pct', 0.0)}% | QoQ Change: {metrics.get('promoter_qoq_change_pct', 'N/A')}%
- Compounded Sales Growth: {json.dumps(growth.get('Compounded Sales Growth', {}))}
- Compounded Profit Growth: {json.dumps(growth.get('Compounded Profit Growth', {}))}
- Technofundamental Profile: {tech.get('technical_summary', 'N/A')}
- Flags: Standard Flags={flags or 'None'} | Forensic Flags={forensic_flags or 'None'}
- Review Context: {review_reason if needs_review else 'Standard quant qualification'}

### STRICT BEAR-FIRST EVALUATION PROTOCOL:
You must structure your evaluation into 4 sequential sections:

SECTION 1: FORENSIC & BEAR KILL-SWITCH CRITIQUE (FIRST)
- Actively search for reasons to KILL this investment thesis.
- Multiple Derating Risk: If P/E > 50x, will multiple compression destroy equity returns even if earnings grow at 20%?
- Balance Sheet & Working Capital: Are receivables or inventories outpacing revenue? Is CWIP inflating without asset turnover?
- Sector Cyclicality: If buying a bruised stock, distinguish between a structural value trap vs a cyclical/transient pullback.

SECTION 2: CONCALL Q&A & GUIDANCE AUDIT (TARGETED WEB SEARCH)
- Perform 1 or 2 targeted web searches via search_web:
  - "{sym} concall highlights OR Q&A OR guidance"
  - "{sym} quarterly results red flags"
- Analyze management commentary and analyst Q&A: Did analysts press management on margin compression, capex delays, or client losses? Did management give direct quantitative commitments or vague evasions?

SECTION 3: THE 20%+ CAGR GROWTH MATH
- Only if the company survives the Bear audit, evaluate which of the 4 alpha pathways realistically achieves >= 20% CAGR total return:
  1. Valuation Expansion / PE Rerating on solid compounder
  2. Earnings Compounding with Derating Stress-Test
  3. Capex Inflection & Operating Leverage
  4. Market Beta & Cyclical Proxy
- Explicitly detail the return math: (EPS growth % + P/E change % = Total CAGR).

SECTION 4: FINAL VERDICT & CONVICTION
- Assign one of: STRONG_BUY_WATCH, ACCUMULATE_ON_DIP, HOLD_WATCH, or REJECT, with conviction score (1-10).
- Define 2-3 concrete thesis invalidation triggers (kill-switches).

CRITICAL: At the end of your response, output a markdown code block tagged ```json containing the complete structured JSON record matching this schema:
```json
{{
  "timestamp": "{datetime.now().isoformat()}",
  "symbol": "{sym}",
  "name": "{name}",
  "verdict": "STRONG_BUY_WATCH|ACCUMULATE_ON_DIP|HOLD_WATCH|REJECT",
  "conviction_score": 8,
  "alpha_pathway": "PE_RERATING|EARNINGS_COMPOUNDER|CAPEX_INFLECTION|CYCLICAL_MARKET_PROXY",
  "expected_cagr_3y": "20-25%",
  "return_math_breakdown": "...",
  "margin_of_safety": "...",
  "screen_count": {candidate.get('screen_count', 1)},
  "matched_screens": {json.dumps(screens)},
  "negative_flags": {json.dumps(flags)},
  "forensic_flags": {json.dumps(forensic_flags)},
  "trend_stage": "{tech.get('trend_stage', 'UNKNOWN')}",
  "mansfield_rs_50d": {tech.get('mansfield_rs_50d') if tech.get('mansfield_rs_50d') is not None else 'null'},
  "thesis": "...",
  "guidance_summary": "...",
  "bear_kill_switches": ["..."],
  "risks_summary": "...",
  "key_catalysts": ["..."]
}}
```
"""
    return prompt


def run_stage_1_prep(limit: Optional[int] = None, refresh_screens: bool = False) -> Dict[str, Any]:
    """
    Executes Stage 1 data preparation:
    1. Loads screens configuration.
    2. Crawls each private/public screen with 2.0s pacing.
    3. Merges screen hits into deduplicated candidates.
    4. Applies EquityFilter (NSE listed operating equities only).
    5. Applies Cloudflare D1 silenced list (PERMABAN + active DROP30).
    6. Applies 45-day review cooldown from reviewed_history.json.
    7. Prioritizes highest confluence (screen_count descending).
    8. Fetches full Screener tables for surviving tickers (up to limit if specified).
    9. Scores the 7 negative parameters.
    10. Drops tickers exceeding max_negative_flags_allowed.
    11. Writes timestamped queue & rejected files.
    """
    start_time = datetime.now()
    timestamp_str = start_time.strftime("%Y-%m-%d_%H%M%S")
    logger.info(f"=== Starting Stage 1: Screener Batch Preparation ({timestamp_str}) ===")

    config = load_config()
    settings = config.get("settings", {})
    global_filters = config.get("global_filters", {})
    screens = config.get("screens", [])

    request_delay = float(settings.get("request_delay_sec", 2.0))
    max_pages = int(settings.get("max_pages_per_screen", 10))
    cooldown_days = int(settings.get("review_cooldown_days", 45))
    max_neg_flags = int(settings.get("max_negative_flags_allowed", 1))

    parser = CustomScreenerParser(request_delay_sec=request_delay)
    equity_filter = EquityFilter()
    state_store = StateStore()
    history = load_history()

    # Step A: Crawl all screens and merge hits
    merged_candidates: Dict[str, Dict[str, Any]] = {}

    for screen in screens:
        screen_id = str(screen.get("id", ""))
        screen_name = screen.get("name", f"Screen {screen_id}")
        rubric = screen.get("rubric", "")
        overrides = screen.get("conditional_overrides", {})

        stocks = parser.parse_saved_screen(screen, max_pages=max_pages, use_cache=not refresh_screens)
        for st in stocks:
            sym = st["symbol"].upper()
            if sym not in merged_candidates:
                merged_candidates[sym] = {
                    "symbol": sym,
                    "name": st["name"],
                    "screen_count": 0,
                    "matched_screens": [],
                    "rubrics": [],
                    "conditional_overrides": [],
                    "screen_metrics": {}
                }

            cand = merged_candidates[sym]
            if screen_name not in cand["matched_screens"]:
                cand["matched_screens"].append(screen_name)
                cand["screen_count"] += 1
                if rubric:
                    cand["rubrics"].append(f"[{screen_name}]: {rubric}")
                if overrides:
                    cand["conditional_overrides"].append(overrides)
                cand["screen_metrics"].update(st.get("columns", {}))

    total_unique_screen_hits = len(merged_candidates)
    logger.info(f"Total unique tickers discovered across {len(screens)} screen(s): {total_unique_screen_hits}")
    if total_unique_screen_hits == 0:
        logger.warning(
            "⚠️ 0 screen hits discovered! If this is unexpected, verify internet connectivity to "
            "https://www.screener.in and ensure that unsandboxed network permission ('Always Allow') is granted."
        )

    # Step B: Filter out Non-Equities, D1 Silenced, and Cooldown
    surviving_initial_gate: Dict[str, Dict[str, Any]] = {}
    excluded_stats = {
        "non_equity": 0,
        "d1_silenced": 0,
        "cooldown": 0
    }

    for sym, cand in merged_candidates.items():
        # Check 1: Non-equity filter (ETFs, InvITs, REITs, etc.)
        if not equity_filter.is_valid_operating_stock(sym):
            logger.info(f"Excluding {sym}: Non-operating equity or instrument pattern.")
            excluded_stats["non_equity"] += 1
            continue

        # Check 2: Cloudflare D1 Silenced List (PERMABAN + active DROP30)
        if state_store.is_ticker_silenced(sym):
            logger.info(f"Excluding {sym}: Silenced in Cloudflare D1 (Permaban / 30D Drop).")
            excluded_stats["d1_silenced"] += 1
            continue

        # Check 3: 45-day Cooldown in reviewed_history.json
        on_cooldown, cooldown_msg = is_on_review_cooldown(sym, cand["screen_count"], history, cooldown_days)
        if on_cooldown:
            logger.info(f"Excluding {sym}: {cooldown_msg}.")
            excluded_stats["cooldown"] += 1
            continue

        surviving_initial_gate[sym] = cand

    logger.info(
        f"Initial Gate Results: Surviving={len(surviving_initial_gate)} | "
        f"Excluded: Non-Equity={excluded_stats['non_equity']}, D1 Silenced={excluded_stats['d1_silenced']}, "
        f"45D Cooldown={excluded_stats['cooldown']}"
    )

    # Step C: PASS 1 - Instant Screen-Level Quant Gate (0 network requests)
    qualified_candidates: List[Dict[str, Any]] = []
    rejected_candidates: List[Dict[str, Any]] = []
    screen_passed_candidates: List[Dict[str, Any]] = []

    for sym, cand in surviving_initial_gate.items():
        s_flags, s_reasons = evaluate_screen_level_flags(
            cand=cand,
            global_filters=global_filters,
            screen_overrides_list=cand.get("conditional_overrides", [])
        )
        if len(s_flags) > max_neg_flags:
            logger.info(
                f"SCREEN-LEVEL REJECTION {sym} ({cand['name']}): {len(s_flags)} negative flags > allowed {max_neg_flags}. "
                f"Flags: {s_flags}. Dropped upfront without fetching full dossier."
            )
            rejected_candidates.append({
                "symbol": sym,
                "name": cand["name"],
                "screen_count": cand["screen_count"],
                "matched_screens": cand["matched_screens"],
                "negative_flags": s_flags,
                "reasons": s_reasons,
                "screen_metrics": cand.get("screen_metrics", {})
            })
        else:
            cand["screen_flags"] = s_flags
            cand["screen_flag_reasons"] = s_reasons
            screen_passed_candidates.append(cand)

    logger.info(
        f"Screen-Level Pass Results: Qualified for Deep Analysis={len(screen_passed_candidates)} | "
        f"Instant Screen Rejections={len(rejected_candidates)}"
    )

    # Step D: Dynamic Multi-Screen & Quality Allocation for Deep Dossier Fetching
    # Generic algorithm that dynamically adapts to ANY array of screens:
    # 1. Multi-Screen Confluence: candidates appearing in >= 2 screens are prioritized first.
    # 2. Per-Screen Balanced Allocation: distributes quota across every screen configured in screens.json.
    # 3. Dynamic Quality Sorting:
    #    - If screen rubric/name reflects Value/Bluechip: prioritizes Market Cap + Drawdown from ATH.
    #    - If screen rubric/name reflects Growth/Momentum: prioritizes 3Y Profit CAGR + Qtr Sales Expansion.
    #    - Default/General: prioritizes ROCE/ROE + Cash Conversion.
    # 4. Fallback / Single-Screen: if only 1 screen is configured, prioritizes by composite quality score.

    max_dossiers = int(settings.get("max_dossiers_to_fetch", 30))
    effective_limit = limit if (limit and limit > 0) else max_dossiers
    selected_dict: Dict[str, Dict[str, Any]] = {}

    # Priority 1: Multi-Screen Confluence (candidates appearing in >= 2 screens)
    multi_screen = [c for c in screen_passed_candidates if c.get("screen_count", 1) >= 2]
    multi_screen.sort(
        key=lambda c: (
            c.get("screen_count", 1),
            len(c.get("screen_flags", [])) == 0,
            extract_screen_metric(c.get("screen_metrics", {}), ["roce", "roe"]) or 0.0
        ),
        reverse=True
    )
    for c in multi_screen:
        if len(selected_dict) < effective_limit:
            selected_dict[c["symbol"]] = c

    # Helper function to dynamically sort candidates for any given screen
    def sort_screen_candidates(cands: List[Dict[str, Any]], screen_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        s_text = f"{screen_info.get('name', '')} {screen_info.get('rubric', '')}".lower()
        is_value = any(k in s_text for k in ["bruised", "bluechip", "discount", "dividend", "value", "graham"])
        is_growth = any(k in s_text for k in ["growth", "momentum", "pead", "cagr", "super"])

        if is_value:
            return sorted(
                cands,
                key=lambda c: (
                    len(c.get("screen_flags", [])) == 0,
                    extract_screen_metric(c.get("screen_metrics", {}), ["mar cap", "market cap"]) or 0.0,
                    extract_screen_metric(c.get("screen_metrics", {}), ["down ath", "down %"]) or 0.0
                ),
                reverse=True
            )
        elif is_growth:
            return sorted(
                cands,
                key=lambda c: (
                    len(c.get("screen_flags", [])) == 0,
                    extract_screen_metric(c.get("screen_metrics", {}), ["profit var 3yrs", "profit var 3"]) or 0.0,
                    extract_screen_metric(c.get("screen_metrics", {}), ["sales latest quarter", "sales preceding year"]) or 0.0,
                    extract_screen_metric(c.get("screen_metrics", {}), ["roce", "roe"]) or 0.0
                ),
                reverse=True
            )
        else:
            return sorted(
                cands,
                key=lambda c: (
                    len(c.get("screen_flags", [])) == 0,
                    extract_screen_metric(c.get("screen_metrics", {}), ["roce", "roe"]) or 0.0,
                    extract_screen_metric(c.get("screen_metrics", {}), ["operating cash flow 3years", "cash flow 3years"]) or 0.0
                ),
                reverse=True
            )

    # Priority 2: Per-Screen Proportional Allocation across all configured screens
    num_screens = max(len(screens), 1)
    quota_per_screen = max(10, effective_limit // num_screens)

    for screen_info in screens:
        s_name = screen_info.get("name", "")
        matching = [
            c for c in screen_passed_candidates
            if s_name in c.get("matched_screens", []) and c["symbol"] not in selected_dict
        ]
        sorted_matching = sort_screen_candidates(matching, screen_info)
        for c in sorted_matching[:quota_per_screen]:
            if c["symbol"] not in selected_dict and len(selected_dict) < effective_limit:
                selected_dict[c["symbol"]] = c

    # Priority 3: Backlog Fill (if quota remains, fill from any remaining candidates sorted by quality)
    if len(selected_dict) < effective_limit:
        remaining = [c for c in screen_passed_candidates if c["symbol"] not in selected_dict]
        remaining.sort(
            key=lambda c: (
                len(c.get("screen_flags", [])) == 0,
                extract_screen_metric(c.get("screen_metrics", {}), ["roce", "roe"]) or 0.0,
                extract_screen_metric(c.get("screen_metrics", {}), ["profit var 3yrs", "profit var 3"]) or 0.0
            ),
            reverse=True
        )
        for c in remaining:
            if len(selected_dict) >= effective_limit:
                break
            selected_dict[c["symbol"]] = c

    candidates_to_fetch = list(selected_dict.values())
    if limit and limit > 0:
        candidates_to_fetch = candidates_to_fetch[:limit]

    logger.info(
        f"Dynamic Audit Queue: {len(candidates_to_fetch)} candidates selected for deep dossier analysis "
        f"across {len(screens)} configured screen(s) (Multi-screen={len(multi_screen)})"
    )

    # Fetch technofundamental metrics (Minervini Stage + Mansfield RS 50D) in batch
    symbols_to_fetch = [c["symbol"] for c in candidates_to_fetch]
    logger.info(f"Computing technofundamental trends and Mansfield RS for {len(symbols_to_fetch)} symbols...")
    tech_data = fetch_technofundamental_batch(symbols_to_fetch)

    # Step E: PASS 2 - Deep Financial Dossier Fetch & Multi-Year Indicator Audit
    for idx, cand in enumerate(candidates_to_fetch, 1):
        sym = cand["symbol"]
        logger.info(f"[{idx}/{len(candidates_to_fetch)}] Fetching full dossier for {sym} (Screen Hits: {cand['screen_count']})...")
        dossier = parser.fetch_company_dossier(sym)
        if not dossier:
            logger.warning(f"Skipping {sym}: Unable to retrieve company dossier.")
            continue

        cand["dossier"] = dossier
        derived = dossier.get("derived_metrics", {})
        sm = cand.get("screen_metrics", {})

        # Populate missing derived metrics using screen metrics fallback (from new formula columns)
        if derived.get("cfo_to_pat_3y") is None:
            cfo_from_screen = extract_screen_metric(sm, ["operating cash flow 3years", "cash flow 3years"])
            if cfo_from_screen is not None:
                derived["cfo_to_pat_3y"] = cfo_from_screen

        if derived.get("other_income_to_pbt_ttm") is None:
            other_inc_from_screen = extract_screen_metric(sm, ["other income last year", "other income / profit before tax"])
            if other_inc_from_screen is not None:
                derived["other_income_to_pbt_ttm"] = other_inc_from_screen

        sales_ratio = extract_screen_metric(sm, ["sales latest quarter", "sales preceding year"])
        if sales_ratio is not None:
            derived["sales_qtr_ratio"] = sales_ratio
            if derived.get("latest_qtr_yoy_sales_pct") is None:
                derived["latest_qtr_yoy_sales_pct"] = round((sales_ratio - 1.0) * 100.0, 2)

        # Attach technofundamental metrics
        cand["technicals"] = tech_data.get(sym, {})
        derived["trend_stage"] = cand["technicals"].get("trend_stage", "UNKNOWN")
        derived["mansfield_rs_50d"] = cand["technicals"].get("mansfield_rs_50d")

        # Evaluate deep negative flags (CFO/PAT, Debt/Equity, QoQ/YoY margins, promoter changes)
        flags, flag_reasons = evaluate_negative_flags(
            stock_candidate=cand,
            global_filters=global_filters,
            screen_overrides_list=cand.get("conditional_overrides", [])
        )

        # Evaluate deterministic forensic flags (CFO/EBITDA, Debtor Days, CWIP, Other Income)
        f_flags, f_reasons = evaluate_forensic_flags(
            stock_candidate=cand,
            global_filters=global_filters,
            screen_overrides_list=cand.get("conditional_overrides", [])
        )
        cand["forensic_flags"] = f_flags
        cand["forensic_flag_reasons"] = f_reasons

        # Multi-flag forensic rejection: >= 2 forensic flags is fatal
        if len(f_flags) >= 2:
            logger.warning(
                f"FORENSIC REJECTION {sym} ({cand['name']}): {len(f_flags)} forensic flags >= 2. "
                f"Forensic Flags: {f_flags}"
            )
            rejected_candidates.append({
                "symbol": sym,
                "name": cand["name"],
                "screen_count": cand["screen_count"],
                "matched_screens": cand["matched_screens"],
                "negative_flags": flags,
                "forensic_flags": f_flags,
                "reasons": {**flag_reasons, **f_reasons},
                "top_ratios": dossier.get("top_ratios", {}),
                "derived_metrics": dossier.get("derived_metrics", {})
            })
            continue

        # If exactly 1 forensic flag, merge into candidate warning flags for visibility
        if len(f_flags) == 1:
            flags.extend(f_flags)
            flag_reasons.update(f_reasons)

        # Screen-Conditional Technofundamental Policy:
        # Pure breakout/momentum screens require non-Stage 4.
        # But if a stock is discounted >= 25% from ATH, or screen targets discounted/value/consolidation setups:
        # Trading below 200 EMA is expected during market consolidation. Do NOT penalize with a negative kill-switch flag.
        matched_screens = cand.get("matched_screens", [])
        is_value_screen = any(
            kw in s.lower()
            for s in matched_screens
            for kw in ["bruised", "discount", "dividend", "value", "graham"]
        )
        down_ath = extract_screen_metric(cand.get("screen_metrics", {}), ["down ath", "down from ath", "discounted from atm"]) or 0.0
        is_discounted_setup = (
            down_ath >= 25.0
            or is_value_screen
            or ("NEG_STAGE_4_DOWNTREND" in exempt_flags)
            or any("discount" in s.lower() for s in matched_screens)
        )

        trend_stage = cand["technicals"].get("trend_stage", "UNKNOWN")

        if trend_stage == "STAGE_4_DOWNTREND":
            if is_discounted_setup:
                logger.info(f"{sym}: Down {down_ath:.1f}% from ATH below 200 EMA during market consolidation. Tagged as STAGE_4_DISCOUNT (not penalized as a negative flag).")
            else:
                logger.info(f"{sym}: Flagging pure momentum candidate due to STAGE_4_DOWNTREND.")
                flags.append("NEG_STAGE_4_DOWNTREND")
                flag_reasons["NEG_STAGE_4_DOWNTREND"] = (
                    "Stage 4 downtrend (CMP < EMA50 < EMA200 with falling 200 EMA) not permitted on pure momentum screens"
                )

        cand["negative_flags"] = flags
        cand["negative_flag_reasons"] = flag_reasons

        if len(flags) > max_neg_flags:
            # Check for Soft-Gate Exemption (Large-Cap / Bruised Bluechip with cyclical headwinds)
            mcap_val = dossier.get("derived_metrics", {}).get("market_cap_cr") or dossier.get("top_ratios", {}).get("Market Cap") or 0.0
            is_bluechip = mcap_val >= 25000 or any("bluechip" in s.lower() for s in cand.get("matched_screens", []))
            has_fatal = "NEG_NET_LOSS" in flags or "NEG_LOW_MCAP" in flags
            pledge_pct = dossier.get("derived_metrics", {}).get("promoter_pledge_pct", 0.0) or 0.0

            if is_bluechip and not has_fatal and pledge_pct <= 5.0 and len(flags) <= 2:
                logger.info(
                    f"SOFT-GATE EXEMPTION for {sym} ({cand['name']}): Market Cap ₹{mcap_val:.0f} Cr bluechip with {flags}. "
                    f"Passing to AI Subagent for qualitative review on Sector Cyclicality vs Margin of Safety."
                )
                cand["needs_agent_review"] = True
                cand["review_reason"] = f"Bluechip experiencing temporary sector/cyclical headwinds ({', '.join(flags)}). Agent to evaluate Margin of Safety."
                cand["negative_flags"] = flags
                cand["negative_flag_reasons"] = flag_reasons
                qualified_candidates.append(cand)
                continue

            logger.warning(
                f"REJECTED {sym} ({cand['name']}): {len(flags)} negative flags > allowed {max_neg_flags}. "
                f"Flags: {flags}"
            )
            rejected_candidates.append({
                "symbol": sym,
                "name": cand["name"],
                "screen_count": cand["screen_count"],
                "matched_screens": cand["matched_screens"],
                "negative_flags": flags,
                "forensic_flags": f_flags,
                "reasons": flag_reasons,
                "top_ratios": dossier.get("top_ratios", {}),
                "derived_metrics": dossier.get("derived_metrics", {})
            })
        else:
            logger.info(
                f"QUALIFIED {sym} ({cand['name']}): {cand['screen_count']} screen(s), "
                f"flags={flags or 'None'}"
            )
            qualified_candidates.append(cand)

    # Step D: Sort qualified candidates by screen_count descending (high confluence first)
    qualified_candidates.sort(
        key=lambda x: (
            x.get("screen_count", 0),
            x.get("dossier", {}).get("derived_metrics", {}).get("roce_pct", 0) or 0
        ),
        reverse=True
    )

    # Step E: Write timestamped queue and rejected files
    QUEUES_DIR.mkdir(parents=True, exist_ok=True)
    queue_file = QUEUES_DIR / f"queue_{timestamp_str}.json"
    rejected_file = QUEUES_DIR / f"rejected_{timestamp_str}.json"

    batch_output = {
        "timestamp": timestamp_str,
        "created_at": datetime.now().isoformat(),
        "total_screen_hits": total_unique_screen_hits,
        "surviving_initial_gate": len(surviving_initial_gate),
        "rejected_count": len(rejected_candidates),
        "qualified_count": len(qualified_candidates),
        "candidates": qualified_candidates
    }

    with open(queue_file, "w", encoding="utf-8") as f:
        json.dump(batch_output, f, indent=2, ensure_ascii=False)

    with open(rejected_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": timestamp_str,
            "created_at": datetime.now().isoformat(),
            "rejected_count": len(rejected_candidates),
            "rejected": rejected_candidates
        }, f, indent=2, ensure_ascii=False)

    # Step F: Pre-render Subagent Prompt Batches (Strictly sequential: 1 stock per batch for atomic checkpointing)
    batch_size = int(settings.get("parallel_subagents_batch_size", 1))
    batches = []
    current_batch = []

    for c in qualified_candidates:
        prompt_item = {
            "symbol": c["symbol"],
            "name": c["name"],
            "role": f"Equity Analyst - {c['symbol']}",
            "type_name": "equity-analyst",
            "prompt": generate_analyst_prompt(c)
        }
        current_batch.append(prompt_item)
        if len(current_batch) == batch_size:
            batches.append(current_batch)
            current_batch = []
    if current_batch:
        batches.append(current_batch)

    batch_files = []
    for b_idx, b in enumerate(batches, 1):
        b_file = QUEUES_DIR / f"active_batch_{b_idx}.json"
        with open(b_file, "w", encoding="utf-8") as f:
            json.dump(b, f, indent=2, ensure_ascii=False)
        batch_files.append(str(b_file))

    manifest_file = QUEUES_DIR / "active_manifest.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": timestamp_str,
            "total_candidates": len(qualified_candidates),
            "total_batches": len(batches),
            "batch_files": [f"data/prepared_queues/active_batch_{i}.json" for i in range(1, len(batches) + 1)]
        }, f, indent=2, ensure_ascii=False)

    duration = (datetime.now() - start_time).total_seconds()
    
    # Human-readable Attrition Funnel Report
    print("\n" + "=" * 70)
    print("🎯 ALPHAMINTER SCREENER ATTRITION FUNNEL SUMMARY")
    print("=" * 70)
    print(f"1. Total Unique Tickers Discovered Across Screens: {total_unique_screen_hits}")
    print(f"2. Excluded by Baseline Gate: {excluded_stats['non_equity'] + excluded_stats['d1_silenced'] + excluded_stats['cooldown']}")
    print(f"   - Non-Operating Equities: {excluded_stats['non_equity']}")
    print(f"   - Silenced (Cloudflare D1 Permaban/Drop): {excluded_stats['d1_silenced']}")
    print(f"   - 45-Day Review Cooldown: {excluded_stats['cooldown']}")
    print(f"3. Surviving Initial Gate: {len(surviving_initial_gate)}")
    print(f"4. Screen-Level Table Rejections (0 Net Calls): {len(rejected_candidates) - (len(candidates_to_fetch) - len(qualified_candidates))}")
    print(f"5. Candidates Audited via Deep Dossier: {len(candidates_to_fetch)}")
    print(f"6. Disqualified by Forensic Ratios: {len(rejected_candidates) - (len(rejected_candidates) - (len(candidates_to_fetch) - len(qualified_candidates)))}")
    print(f"7. FINAL QUALIFIED QUEUE FOR AI SUBAGENTS: {len(qualified_candidates)}")
    for q_idx, q in enumerate(qualified_candidates, 1):
        m = q.get('dossier', {}).get('derived_metrics', {})
        print(f"   [{q_idx:02d}] {q['symbol']:<12} | Hits: {q['screen_count']} | ROCE: {m.get('roce_pct', 'N/A')}% | CFO/PAT: {m.get('cfo_to_pat_3y', 'N/A')}x | P/E: {m.get('pe', 'N/A')}x | Flags: {q.get('negative_flags') or 'Clean'}")
    print("=" * 70 + "\n")

    logger.info("=" * 60)
    logger.info(f"STAGE 1 COMPLETE in {duration:.1f}s")
    logger.info(f"Qualified Candidates Queue: {queue_file} ({len(qualified_candidates)} stocks)")
    logger.info(f"Rejected Audit Log: {rejected_file} ({len(rejected_candidates)} stocks)")
    logger.info("=" * 60)

    return {
        "queue_file": str(queue_file),
        "rejected_file": str(rejected_file),
        "qualified_count": len(qualified_candidates),
        "rejected_count": len(rejected_candidates)
    }


if __name__ == "__main__":
    import argparse
    parser_cli = argparse.ArgumentParser(description="Stage 1: Screener Batch Preparation & Quant Gate")
    parser_cli.add_argument("--limit", type=int, default=None, help="Limit number of highest-confluence candidates to process")
    parser_cli.add_argument("--refresh-screens", action="store_true", help="Invalidate screen caches and fetch screens live")
    args = parser_cli.parse_args()
    run_stage_1_prep(limit=args.limit, refresh_screens=args.refresh_screens)

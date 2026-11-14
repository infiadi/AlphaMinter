import os
import sys
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
        (sales_3y is not None and sales_3y >= 12.0)
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
        sales_growth = extract_screen_metric(m, ["sales growth"])
        if sales_growth is not None and sales_growth < global_min_sales:
            if has_multi_year_cushion:
                logger.info(f"Screen {cand_sym}: Transient Qtr Sales dip ({sales_growth}%) cushioned by multi-year CAGR (3Y Sales: {sales_3y}%). Not penalized.")
            else:
                flags.append("NEG_LATEST_QTR_SALES")
                reasons["NEG_LATEST_QTR_SALES"] = f"Screen Sales growth {sales_growth}% < {global_min_sales}% (no multi-year cushion)"

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

    # 2. NEG_CASH_CONVERSION (CFO to PAT 3Y) - Skipped for banks & NBFCs
    if "NEG_CASH_CONVERSION" not in exempt_flags:
        if not is_fin:
            global_min_cfo = float(global_filters.get("min_cfo_to_pat_3y", 0.60))
            screen_cfos = [ov.get("min_cfo_to_pat_3y") for ov in screen_overrides_list if ov.get("min_cfo_to_pat_3y") is not None]
            effective_min_cfo = max(screen_cfos) if screen_cfos else global_min_cfo

            cfo_pat = metrics.get("cfo_to_pat_3y")
            if cfo_pat is not None and cfo_pat < effective_min_cfo:
                flags.append("NEG_CASH_CONVERSION")
                reasons["NEG_CASH_CONVERSION"] = f"3Y CFO/PAT {cfo_pat:.2f} < threshold {effective_min_cfo:.2f}"

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
        elif promoter_qoq is not None and promoter_qoq < -2.0:
            flags.append("NEG_PROMOTER_PLEDGE")
            reasons["NEG_PROMOTER_PLEDGE"] = f"Promoter holding declined by {abs(promoter_qoq)}% QoQ (>-2.0% threshold)"

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

    has_multi_year_cushion = (
        (profit_3y_cagr is not None and profit_3y_cagr >= 15.0) or
        (profit_5y_cagr is not None and profit_5y_cagr >= 15.0) or
        (sales_3y_cagr is not None and sales_3y_cagr >= 12.0)
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


def run_stage_1_prep(limit: Optional[int] = None) -> Dict[str, Any]:
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

        stocks = parser.parse_saved_screen(screen, max_pages=max_pages)
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

    # Step D: Balanced Screen Allocation for Deep Dossier Fetching
    # Ensures representation from:
    # 1. Multi-Screen Confluence (all screen_count >= 2)
    # 2. Bruised Bluechips (sorted by Market Cap & Margin of Safety / Discount from ATH) -> Guarantees TCS, ITC, HUL
    # 3. Super High Growth (sorted by 3Y/5Y growth)
    # 4. Midcap Opportunity (sorted by ROCE)

    multi_screen = [c for c in screen_passed_candidates if c.get("screen_count", 1) >= 2]
    
    # Bruised Bluechips: Sort by Market Cap descending (giants like TCS, ITC, HUL first)
    bruised_bluechips = [
        c for c in screen_passed_candidates
        if "Bruised Bluechips" in c.get("matched_screens", []) and c.get("screen_count", 1) == 1
    ]
    bruised_bluechips.sort(
        key=lambda c: (
            extract_screen_metric(c.get("screen_metrics", {}), ["mar cap", "market cap"]) or 0.0,
            extract_screen_metric(c.get("screen_metrics", {}), ["down ath", "down %"]) or 0.0
        ),
        reverse=True
    )

    # Super High Growth: Sort by 3Y profit growth & sales growth
    growth_stocks = [
        c for c in screen_passed_candidates
        if "Super High Growth Companies in Long Term" in c.get("matched_screens", []) and c.get("screen_count", 1) == 1
    ]
    growth_stocks.sort(
        key=lambda c: (
            extract_screen_metric(c.get("screen_metrics", {}), ["profit var 3yrs", "profit var 3"]) or 0.0,
            extract_screen_metric(c.get("screen_metrics", {}), ["sales growth"]) or 0.0
        ),
        reverse=True
    )

    # Midcap Opportunity: Sort by ROCE
    midcap_stocks = [
        c for c in screen_passed_candidates
        if "Midcap Opportunity" in c.get("matched_screens", []) and c.get("screen_count", 1) == 1
    ]
    midcap_stocks.sort(
        key=lambda c: extract_screen_metric(c.get("screen_metrics", {}), ["roce"]) or 0.0,
        reverse=True
    )

    # Balanced assembly of candidates to audit
    selected_dict: Dict[str, Dict[str, Any]] = {}
    for c in multi_screen:
        selected_dict[c["symbol"]] = c
    for c in bruised_bluechips[:15]:
        if c["symbol"] not in selected_dict:
            selected_dict[c["symbol"]] = c
    for c in growth_stocks[:10]:
        if c["symbol"] not in selected_dict:
            selected_dict[c["symbol"]] = c
    for c in midcap_stocks[:10]:
        if c["symbol"] not in selected_dict:
            selected_dict[c["symbol"]] = c

    candidates_to_fetch = list(selected_dict.values())
    if limit and limit > 0:
        candidates_to_fetch = candidates_to_fetch[:limit]

    logger.info(
        f"Balanced Audit Queue: {len(candidates_to_fetch)} candidates selected for deep dossier analysis: "
        f"Multi-screen={len(multi_screen)}, Bruised Bluechips={len(bruised_bluechips[:15])}, "
        f"High Growth={len(growth_stocks[:10])}, Midcap={len(midcap_stocks[:10])}"
    )

    # Step E: PASS 2 - Deep Financial Dossier Fetch & Multi-Year Indicator Audit
    for idx, cand in enumerate(candidates_to_fetch, 1):
        sym = cand["symbol"]
        logger.info(f"[{idx}/{len(candidates_to_fetch)}] Fetching full dossier for {sym} (Screen Hits: {cand['screen_count']})...")
        dossier = parser.fetch_company_dossier(sym)
        if not dossier:
            logger.warning(f"Skipping {sym}: Unable to retrieve company dossier.")
            continue

        cand["dossier"] = dossier

        # Evaluate deep negative flags (CFO/PAT, Debt/Equity, QoQ/YoY margins, promoter changes)
        flags, flag_reasons = evaluate_negative_flags(
            stock_candidate=cand,
            global_filters=global_filters,
            screen_overrides_list=cand.get("conditional_overrides", [])
        )
        cand["negative_flags"] = flags
        cand["negative_flag_reasons"] = flag_reasons

        if len(flags) > max_neg_flags:
            # Check for Soft-Gate Exemption (Large-Cap / Bruised Bluechip with cyclical headwinds)
            mcap_val = dossier.get("derived_metrics", {}).get("market_cap_cr") or dossier.get("top_ratios", {}).get("Market Cap") or 0.0
            is_bluechip = mcap_val >= 25000 or ("Bruised Bluechips" in cand.get("matched_screens", []))
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
    args = parser_cli.parse_args()
    run_stage_1_prep(limit=args.limit)

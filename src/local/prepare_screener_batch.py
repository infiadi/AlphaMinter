import os
import sys
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

    # 1. NEG_ROCE
    if "NEG_ROCE" not in exempt_flags:
        global_min_roce = float(global_filters.get("min_roce_pct", 10.0))
        # Use strictest threshold among matched screens
        screen_roces = [ov.get("min_roce_pct") for ov in screen_overrides_list if ov.get("min_roce_pct") is not None]
        effective_min_roce = max(screen_roces) if screen_roces else global_min_roce

        roce = metrics.get("roce_pct") or top_ratios.get("ROCE")
        if roce is not None and roce < effective_min_roce:
            flags.append("NEG_ROCE")
            reasons["NEG_ROCE"] = f"ROCE {roce}% < minimum required {effective_min_roce}%"

    # 2. NEG_CASH_CONVERSION (CFO to PAT 3Y)
    if "NEG_CASH_CONVERSION" not in exempt_flags:
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

    # 5. NEG_LATEST_QTR_PAT
    if "NEG_LATEST_QTR_PAT" not in exempt_flags:
        global_min_pat = float(global_filters.get("min_latest_qtr_yoy_pat_pct", -15.0))
        pat_growth = metrics.get("latest_qtr_yoy_pat_pct")
        if pat_growth is not None and pat_growth < global_min_pat:
            flags.append("NEG_LATEST_QTR_PAT")
            reasons["NEG_LATEST_QTR_PAT"] = f"Latest Qtr YoY PAT growth {pat_growth}% < min allowed {global_min_pat}%"

    # 6. NEG_LATEST_QTR_SALES
    if "NEG_LATEST_QTR_SALES" not in exempt_flags:
        global_min_sales = float(global_filters.get("min_latest_qtr_yoy_sales_pct", -5.0))
        sales_growth = metrics.get("latest_qtr_yoy_sales_pct")
        if sales_growth is not None and sales_growth < global_min_sales:
            flags.append("NEG_LATEST_QTR_SALES")
            reasons["NEG_LATEST_QTR_SALES"] = f"Latest Qtr YoY Sales growth {sales_growth}% < min allowed {global_min_sales}%"

    # 7. NEG_OPM_CONTRACTION
    if "NEG_OPM_CONTRACTION" not in exempt_flags:
        yoy_opm_bps = metrics.get("latest_qtr_yoy_opm_bps")
        if yoy_opm_bps is not None and yoy_opm_bps < -300:
            flags.append("NEG_OPM_CONTRACTION")
            reasons["NEG_OPM_CONTRACTION"] = f"Operating Margin contracted by {abs(yoy_opm_bps):.0f} bps YoY (>300 bps contraction)"

    # Also check Market Cap floor if configured
    global_min_mcap = float(global_filters.get("min_market_cap_cr", 1000))
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

    # Prioritize highest confluence candidates (screen_count descending)
    prioritized_candidates = sorted(
        surviving_initial_gate.items(),
        key=lambda item: item[1].get("screen_count", 1),
        reverse=True
    )
    if limit and limit > 0:
        logger.info(f"Applying batch limit: processing top {limit} prioritized candidates out of {len(prioritized_candidates)}")
        prioritized_candidates = prioritized_candidates[:limit]

    # Step C: Fetch Full Financial Dossiers & Score Negative Flags
    qualified_candidates: List[Dict[str, Any]] = []
    rejected_candidates: List[Dict[str, Any]] = []

    for idx, (sym, cand) in enumerate(prioritized_candidates, 1):
        logger.info(f"[{idx}/{len(prioritized_candidates)}] Fetching full dossier for {sym} (Screen Hits: {cand['screen_count']})...")
        dossier = parser.fetch_company_dossier(sym)
        if not dossier:
            logger.warning(f"Skipping {sym}: Unable to retrieve company dossier.")
            continue

        cand["dossier"] = dossier

        # Evaluate negative flags
        flags, flag_reasons = evaluate_negative_flags(
            stock_candidate=cand,
            global_filters=global_filters,
            screen_overrides_list=cand.get("conditional_overrides", [])
        )
        cand["negative_flags"] = flags
        cand["negative_flag_reasons"] = flag_reasons

        if len(flags) > max_neg_flags:
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

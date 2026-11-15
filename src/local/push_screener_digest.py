import os
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import html
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.notifications.telegram_dispatcher import TelegramDispatcher
from src.local.brokerage_targets import fetch_brokerage_consensus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("push_screener_digest")

LOGS_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
DISPATCH_LEDGER_PATH = DATA_DIR / "dispatched_alerts.json"

MAX_ALERTS_PER_BATCH = 5
DISPATCH_COOLDOWN_DAYS = 7


def safe_html(text: Any) -> str:
    """Escapes HTML entities so Telegram API never fails with 400 Bad Request."""
    if text is None:
        return ""
    return html.escape(str(text))


def load_dispatch_ledger() -> Dict[str, Any]:
    """Loads history of dispatched Telegram alerts to eliminate alert spam."""
    if DISPATCH_LEDGER_PATH.exists():
        try:
            with open(DISPATCH_LEDGER_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
        except Exception as e:
            logger.warning(f"Error reading dispatch ledger: {e}")
    return {}


def save_dispatch_ledger(ledger: Dict[str, Any]):
    """Persists dispatch ledger atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = DISPATCH_LEDGER_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
    tmp_path.replace(DISPATCH_LEDGER_PATH)


def find_target_jsonl(date_str: Optional[str] = None) -> Optional[Path]:
    """Finds the audit JSONL file for the given date or the most recent one."""
    if not LOGS_DIR.exists():
        return None

    if date_str:
        exact_path = LOGS_DIR / f"screener_audit_{date_str}.jsonl"
        if exact_path.exists():
            return exact_path

    files = sorted(LOGS_DIR.glob("screener_audit_*.jsonl"))
    return files[-1] if files else None


def load_audit_records(jsonl_path: Path) -> List[Dict[str, Any]]:
    records = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception as e:
                logger.warning(f"Error parsing line in {jsonl_path}: {e}")
    return records


def format_digest_card(record: Dict[str, Any], rank: int = 1, total_picks: int = 1) -> Tuple[str, Dict[str, Any]]:
    """Formats an approved stock record into an institutional Telegram card with 1-tap triage buttons."""
    verdict = record.get("verdict", "WATCH").upper()
    symbol = record.get("symbol", "").upper()
    name = record.get("name", symbol)
    screens = record.get("matched_screens", [])
    screen_count = record.get("screen_count", len(screens))
    conviction = record.get("conviction_score", 8.0)
    
    metrics = record.get("metrics", {})
    flags = record.get("negative_flags", [])

    # Rank & Badge Header
    if verdict == "STRONG_BUY_WATCH":
        icon = "🔥"
        rank_badge = "🏆 <b>#1 TOP PICK</b>" if rank == 1 else f"⭐ <b>PICK #{rank} OF {total_picks}</b>"
        title = f"{icon} <b>[STRONG BUY WATCH]</b> {rank_badge} (Conviction: <b>{conviction}/10</b>)"
    else:
        icon = "🎯"
        rank_badge = f"<b>PICK #{rank} OF {total_picks}</b>"
        title = f"{icon} <b>[ACCUMULATE ON DIP]</b> {rank_badge} (Conviction: <b>{conviction}/10</b>)"

    # Sector Lifecycle & Future Growth Phase
    sector_class = str(record.get("sector_classification", "SUNRISE_SECTOR")).upper()
    growth_phase = str(record.get("future_growth_phase", "ACCELERATING_EXPANSION")).replace("_", " ").title()
    sector_contra = record.get("sector_and_contra_analysis") or record.get("thesis") or "Sector dynamics evaluated."
    future_catalysts = record.get("forward_growth_catalysts") or record.get("guidance_summary") or "Multi-year capacity and demand drivers."

    if sector_class == "SUNRISE_SECTOR":
        sector_badge = f"🌅 <b>SUNRISE SECTOR</b> (Phase: <b>{safe_html(growth_phase)}</b>)"
    elif sector_class == "CONTRA_BET":
        sector_badge = f"🔄 <b>CONTRA BET WATCH</b> (Phase: <b>{safe_html(growth_phase)}</b>)"
    elif sector_class == "CYCLICAL_INFLECTION":
        sector_badge = f"🏗️ <b>CAPEX INFLECTION</b> (Phase: <b>{safe_html(growth_phase)}</b>)"
    else:
        sector_badge = f"📊 <b>{safe_html(sector_class)}</b> (Phase: <b>{safe_html(growth_phase)}</b>)"

    screens_str = safe_html(", ".join(screens) if screens else "Screener Quantitative Gate")
    flags_str = safe_html(", ".join(flags) if flags else "Clean (0 Flags)")

    cmp_val = metrics.get("current_price") or metrics.get("cmp")
    pe_val = metrics.get("pe")
    mcap_val = metrics.get("market_cap_cr")
    roce_val = metrics.get("roce_pct")
    cfo_pat_val = metrics.get("cfo_to_pat_3y")
    sales_3y = metrics.get("sales_growth_3y") or metrics.get("latest_qtr_yoy_sales_pct")
    pat_3y = metrics.get("profit_growth_3y") or metrics.get("latest_qtr_yoy_pat_pct")
    debt_eq = metrics.get("debt_to_equity")

    cmp_str = f"₹{cmp_val:,.1f}" if isinstance(cmp_val, (int, float)) else "N/A"
    pe_str = f"{pe_val:.1f}x" if isinstance(pe_val, (int, float)) else "N/A"
    mcap_str = f"₹{mcap_val:,.0f} Cr" if isinstance(mcap_val, (int, float)) else "N/A"
    roce_str = f"{roce_val:.1f}%" if isinstance(roce_val, (int, float)) else "N/A"
    cfo_pat_str = f"{cfo_pat_val:.2f}x" if isinstance(cfo_pat_val, (int, float)) else "N/A"
    sales_growth_str = f"{sales_3y:.1f}%" if isinstance(sales_3y, (int, float)) else "N/A"
    pat_growth_str = f"{pat_3y:.1f}%" if isinstance(pat_3y, (int, float)) else "N/A"
    de_str = f"{debt_eq:.2f}" if isinstance(debt_eq, (int, float)) else "N/A"

    pathway = safe_html(record.get("alpha_pathway") or "20%+ Target IRR")
    expected_cagr = safe_html(record.get("expected_cagr_3y") or "20%+")
    return_math = safe_html(record.get("return_math_breakdown") or record.get("cagr_breakdown") or "EPS Growth + Valuation Delta")
    margin_of_safety = safe_html(record.get("margin_of_safety") or "High (Quant Verified)")
    
    why_picked = safe_html(record.get("why_picked") or record.get("thesis") or "High-confluence compounder with structural moat.")
    forward_guidance = safe_html(record.get("forward_guidance_vs_pe") or record.get("guidance_summary") or "Management commentary supports forward earnings trajectory.")
    derating_risk = safe_html(record.get("derating_risk_floor_pe") or record.get("risks_summary") or "Derating risk mitigated by current valuation support.")
    kill_switches = record.get("bear_kill_switches") or record.get("kill_switches") or []

    if kill_switches:
        ks_lines = [f"• <code>{safe_html(ks)}</code>" for ks in kill_switches[:3]]
        ks_text = "\n".join(ks_lines)
    else:
        ks_text = "• <i>Standard thesis invalidation triggers apply</i>"

    # Fetch live yFinance consensus & forward metrics
    broker_data = fetch_brokerage_consensus(symbol)
    if broker_data.get("has_coverage"):
        upside_sign = "+" if (broker_data.get("upside_pct") or 0) > 0 else ""
        broker_section = (
            f"<b>📈 Street Consensus & Broker Targets (yFinance):</b>\n"
            f"• 🎯 <b>Consensus Target:</b> <b>₹{broker_data['target_median']:,.1f}</b> "
            f"(<code>{upside_sign}{broker_data['upside_pct']}%</code> upside | {broker_data['analyst_count']} brokers)\n"
            f"• 🏷️ <b>Broker Target Range:</b> Low ₹{broker_data['target_low']:,.0f} — High ₹{broker_data['target_high']:,.0f}\n"
        )
    else:
        broker_section = (
            f"<b>📈 Street Consensus & Broker Targets (yFinance):</b>\n"
            f"• 🎯 <b>Consensus Target:</b> <i>Uncovered by sell-side brokers (pure alpha compounder)</i>\n"
        )

    fwd_pe_str = f"<b>{broker_data['forward_pe']}x</b>" if broker_data.get("forward_pe") else "<i>N/A</i>"
    fwd_eps_str = f"₹{broker_data['forward_eps']}" if broker_data.get("forward_eps") else "<i>N/A</i>"
    ev_ebitda_str = f"{broker_data['ev_to_ebitda']}x" if broker_data.get("ev_to_ebitda") else "<i>N/A</i>"
    beta_str = f"{broker_data['beta']}" if broker_data.get("beta") else "<i>N/A</i>"
    inst_str = f"{broker_data['institutional_holding_pct']}%" if broker_data.get("institutional_holding_pct") else "<i>N/A</i>"

    broker_section += (
        f"• 🔭 <b>Forward P/E:</b> {fwd_pe_str} | <b>Forward EPS:</b> {fwd_eps_str} | <b>EV/EBITDA:</b> {ev_ebitda_str}\n"
        f"• 🏛️ <b>Institutional Float:</b> {inst_str} | <b>Beta (5Y):</b> {beta_str}\n"
    )

    rev_str = broker_data.get("eps_revisions_30d")
    trend_str = broker_data.get("eps_trend_30d")
    if rev_str or trend_str:
        rev_part = f"30D Revisions: <b>{rev_str}</b>" if rev_str else ""
        trend_part = f"30D Trend: <b>{trend_str}</b>" if trend_str else ""
        rev_line = " | ".join(p for p in [rev_part, trend_part] if p)
        broker_section += f"• 📊 <b>Institutional Momentum:</b> {rev_line}\n"

    top_news = broker_data.get("recent_news")
    if top_news:
        clean_headline = safe_html(top_news[0])
        broker_section += f"• 📰 <b>Recent Headline:</b> <i>{clean_headline}</i>\n"

    broker_section += "\n"

    msg = (
        f"{title}\n\n"
        f"<b>Company:</b> <code>{safe_html(symbol)}</code> ({safe_html(name)})\n"
        f"<b>Sector Profile:</b> {sector_badge}\n"
        f"<b>Confluence:</b> {screen_count} Screen(s) [<i>{screens_str}</i>]\n"
        f"<b>🎯 20%+ Pathway:</b> <code>{pathway}</code> (Target: <b>{expected_cagr}</b>)\n"
        f"<b>📐 3Y Return Math:</b> <code>{return_math}</code>\n"
        f"<b>🛡️ Margin of Safety:</b> <i>{margin_of_safety}</i>\n"
        f"<b>Warning Flags:</b> <code>{flags_str}</code>\n\n"
        f"<b>📊 Valuation & Core Ratios:</b>\n"
        f"• CMP: <b>{cmp_str}</b> | P/E: <b>{pe_str}</b> | M.Cap: <b>{mcap_str}</b>\n"
        f"• ROCE: <b>{roce_str}</b> | 3Y CFO/PAT: <b>{cfo_pat_str}</b> | D/E: <b>{de_str}</b>\n"
        f"• 3Y Sales CAGR: <b>{sales_growth_str}</b> | 3Y PAT CAGR: <b>{pat_growth_str}</b>\n\n"
        f"{broker_section}"
        f"<b>🌅 Sector Dynamics & Tailwinds:</b>\n"
        f"<i>{safe_html(sector_contra)}</i>\n\n"
        f"<b>🚀 Future Growth Drivers (FY27–FY29):</b>\n"
        f"<i>{safe_html(future_catalysts)}</i>\n\n"
        f"<b>💎 Why It Was Picked (Moat & Inflection):</b>\n"
        f"<i>{why_picked}</i>\n\n"
        f"<b>🔭 Forward Guidance vs. P/E (Anti-Derating):</b>\n"
        f"<i>{forward_guidance}</i>\n\n"
        f"<b>⚠️ Bear Derating Risk & Valuation Floor:</b>\n"
        f"<i>{derating_risk}</i>\n\n"
        f"<b>🚨 Thesis Invalidation Kill-Switches:</b>\n"
        f"{ks_text}"
    )

    dispatcher = TelegramDispatcher()
    markup = dispatcher.build_triage_buttons(symbol)

    return msg, markup


def push_screener_digest(
    date_str: Optional[str] = None,
    target_symbols: Optional[List[str]] = None,
    force: bool = False,
    max_alerts: int = MAX_ALERTS_PER_BATCH
) -> int:
    """
    Reads the screener audit JSONL file, filters approved high-conviction decisions,
    sorts them in ascending priority order (so the best/strongest candidate is sent
    LAST and sits at the very BOTTOM of Telegram chat), and dispatches to Telegram.
    """
    jsonl_path = find_target_jsonl(date_str)
    if not jsonl_path:
        logger.warning(f"No audit JSONL log found in {LOGS_DIR}. Nothing to dispatch.")
        return 0

    logger.info(f"Loading screener audit log from: {jsonl_path}")
    records = load_audit_records(jsonl_path)
    if not records:
        logger.info("No audit records found in log file.")
        return 0

    dispatcher = TelegramDispatcher()
    ledger = load_dispatch_ledger()
    now_dt = datetime.now()

    # Deduplicate by symbol keeping the latest evaluation
    latest_by_symbol: Dict[str, Dict[str, Any]] = {}
    for r in records:
        sym = r.get("symbol", "").upper()
        if sym:
            latest_by_symbol[sym] = r

    unique_records = list(latest_by_symbol.values())

    # If specific target symbols were requested, restrict to them
    if target_symbols:
        target_set = {s.strip().upper() for s in target_symbols if s.strip()}
        candidates = [r for r in unique_records if r.get("symbol", "").upper() in target_set]
    else:
        candidates = unique_records

    # High-conviction threshold: STRONG_BUY_WATCH (>=8.5) or ACCUMULATE_ON_DIP (>=7.5)
    approved_verdicts = {"STRONG_BUY_WATCH", "ACCUMULATE_ON_DIP"}
    approved_records = []
    for r in candidates:
        verdict = r.get("verdict", "").upper()
        if verdict not in approved_verdicts:
            continue
        conviction = float(r.get("conviction_score") or 0.0)
        if verdict == "STRONG_BUY_WATCH" and conviction < 8.5:
            continue
        if verdict == "ACCUMULATE_ON_DIP" and conviction < 7.5:
            continue
        approved_records.append(r)

    # Filter out already dispatched symbols within cooldown window unless forced or upgraded
    fresh_candidates = []
    for r in approved_records:
        sym = r.get("symbol", "").upper()
        verdict = r.get("verdict", "").upper()
        prev = ledger.get(sym)
        if not force and prev:
            prev_time_str = prev.get("dispatched_at")
            prev_verdict = prev.get("verdict", "")
            if prev_time_str:
                try:
                    prev_dt = datetime.fromisoformat(prev_time_str)
                    if (now_dt - prev_dt).days < DISPATCH_COOLDOWN_DAYS and verdict == prev_verdict:
                        logger.info(f"Skipping {sym}: already dispatched on {prev_time_str} (cooldown {DISPATCH_COOLDOWN_DAYS}d)")
                        continue
                except Exception:
                    pass
        fresh_candidates.append(r)

    logger.info(f"Evaluated candidates: {len(candidates)} | Approved: {len(approved_records)} | Fresh to dispatch: {len(fresh_candidates)}")

    if not fresh_candidates:
        logger.info("No fresh high-conviction alerts to dispatch.")
        return 0

    # Rank score formula: Verdict (STRONG_BUY > ACCUMULATE) + Conviction Score + Screen Confluence
    def rank_score(r: Dict[str, Any]) -> float:
        verdict = str(r.get("verdict", "")).upper()
        v_base = 20.0 if verdict == "STRONG_BUY_WATCH" else 10.0
        conviction = float(r.get("conviction_score") or 0.0)
        screen_count = float(r.get("screen_count") or 0.0) * 0.1
        # Extra boost if classified as Sunrise Sector
        s_class = str(r.get("sector_classification", "")).upper()
        if s_class == "SUNRISE_SECTOR":
            v_base += 2.0
        return v_base + conviction + screen_count

    # Sort descending to assign ranks #1 (Best), #2, #3...
    candidates_ranked = sorted(fresh_candidates, key=rank_score, reverse=True)
    picks_to_dispatch = candidates_ranked[:max_alerts]
    total_picks = len(picks_to_dispatch)

    # Assign rank: index 0 is Rank 1 (Top Pick)
    picks_with_rank = [(r, idx + 1) for idx, r in enumerate(picks_to_dispatch)]

    # 1. Send Batch Summary Header FIRST (so it sits at the top of the batch in Telegram)
    summary_text = (
        f"📋 <b>AlphaMinter Screener Audit Digest</b>\n\n"
        f"• Date: <code>{now_dt.strftime('%Y-%m-%d')}</code>\n"
        f"• Evaluated Candidates: <b>{len(candidates)}</b>\n"
        f"• High-Conviction Approved: <b>{total_picks}</b>\n"
        f"• Strong Buy Watch: <b>{sum(1 for r, _ in picks_with_rank if r.get('verdict') == 'STRONG_BUY_WATCH')}</b>\n"
        f"• Accumulate on Dip: <b>{sum(1 for r, _ in picks_with_rank if r.get('verdict') == 'ACCUMULATE_ON_DIP')}</b>\n\n"
        f"⚡ <i>Delivering {total_picks} pick(s) below. The #1 Top Pick is sent last to appear right at the bottom of your chat!</i>"
    )
    dispatcher.send_message(summary_text)

    # 2. Telegram chat ordering: User requested the best/strongest candidates sit at the BOTTOM of chat.
    # In Telegram, newest messages append chronologically at the bottom.
    # Therefore, we reverse the delivery order: lower rank picks sent first, and Rank #1 Top Pick sent LAST!
    delivery_queue = list(reversed(picks_with_rank))

    dispatched_count = 0
    for r, rank_num in delivery_queue:
        sym = r.get("symbol", "").upper()
        msg_text, markup = format_digest_card(r, rank=rank_num, total_picks=total_picks)
        success = dispatcher.send_message(msg_text, reply_markup=markup)
        if success:
            dispatched_count += 1
            ledger[sym] = {
                "dispatched_at": now_dt.isoformat(),
                "verdict": r.get("verdict"),
                "conviction_score": r.get("conviction_score"),
                "rank": rank_num
            }
            logger.info(f"Dispatched Telegram card #{rank_num}/{total_picks} for {sym}")

    save_dispatch_ledger(ledger)
    return dispatched_count


def parse_args():
    parser = argparse.ArgumentParser(description="Push AlphaMinter screener audit digest to Telegram")
    parser.add_argument("date_str", nargs="?", default=None, help="Target date YYYY-MM-DD")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated list of symbols to dispatch")
    parser.add_argument("--force", action="store_true", help="Bypass cooldown and re-dispatch approved stocks")
    parser.add_argument("--max-alerts", type=int, default=MAX_ALERTS_PER_BATCH, help="Maximum alert cards to send")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sym_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    push_screener_digest(
        date_str=args.date_str,
        target_symbols=sym_list,
        force=args.force,
        max_alerts=args.max_alerts
    )

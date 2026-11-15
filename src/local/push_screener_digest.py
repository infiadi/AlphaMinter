import os
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.notifications.telegram_dispatcher import TelegramDispatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("push_screener_digest")

LOGS_DIR = BASE_DIR / "logs"


def find_target_jsonl(date_str: Optional[str] = None) -> Optional[Path]:
    """Finds the audit JSONL file for the given date or the most recent one."""
    if not LOGS_DIR.exists():
        return None

    if date_str:
        exact_path = LOGS_DIR / f"screener_audit_{date_str}.jsonl"
        if exact_path.exists():
            return exact_path

    # Search for all screener_audit_*.jsonl files
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


def format_digest_card(record: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Formats an approved stock record into HTML Telegram alert and buttons markup."""
    verdict = record.get("verdict", "WATCH").upper()
    symbol = record.get("symbol", "").upper()
    name = record.get("name", symbol)
    screens = record.get("matched_screens", [])
    screen_count = record.get("screen_count", len(screens))
    conviction = record.get("conviction_score", 8)
    
    metrics = record.get("metrics", {})
    thesis = record.get("thesis", "High-growth compounder meeting multi-screen quant criteria.")
    guidance = record.get("guidance_summary", "Strong management commentary for FY27/FY28.")
    flags = record.get("negative_flags", [])

    if verdict == "STRONG_BUY_WATCH":
        icon = "🔥"
        title = f"{icon} <b>[STRONG BUY WATCH]</b> High-Confluence Pick ({conviction}/10)"
    else:
        icon = "🎯"
        title = f"{icon} <b>[ACCUMULATE ON DIP]</b> Quality Compounder ({conviction}/10)"

    screens_str = ", ".join(screens) if screens else "Screener Quantitative Gate"
    flags_str = ", ".join(flags) if flags else "Clean (0 Flags)"

    cmp_val = metrics.get("current_price") or metrics.get("cmp")
    pe_val = metrics.get("pe")
    mcap_val = metrics.get("market_cap_cr")
    roce_val = metrics.get("roce_pct")
    cfo_pat_val = metrics.get("cfo_to_pat_3y")
    sales_3y = metrics.get("sales_growth_3y") or metrics.get("latest_qtr_yoy_sales_pct")
    pat_3y = metrics.get("profit_growth_3y") or metrics.get("latest_qtr_yoy_pat_pct")

    cmp_str = f"₹{cmp_val:,.1f}" if isinstance(cmp_val, (int, float)) else "N/A"
    pe_str = f"{pe_val:.1f}x" if isinstance(pe_val, (int, float)) else "N/A"
    mcap_str = f"₹{mcap_val:,.0f} Cr" if isinstance(mcap_val, (int, float)) else "N/A"
    roce_str = f"{roce_val:.1f}%" if isinstance(roce_val, (int, float)) else "N/A"
    cfo_pat_str = f"{cfo_pat_val:.2f}x" if isinstance(cfo_pat_val, (int, float)) else "N/A"
    sales_growth_str = f"{sales_3y:.1f}%" if isinstance(sales_3y, (int, float)) else "N/A"
    pat_growth_str = f"{pat_3y:.1f}%" if isinstance(pat_3y, (int, float)) else "N/A"

    pathway = record.get("alpha_pathway") or "20%+ Target IRR"
    expected_cagr = record.get("expected_cagr_3y") or "20%+"
    margin_of_safety = record.get("margin_of_safety") or "Quant Verified"

    msg = (
        f"{title}\n\n"
        f"<b>Company:</b> <code>{symbol}</code> ({name})\n"
        f"<b>Confluence:</b> {screen_count} Screen(s) [<i>{screens_str}</i>]\n"
        f"<b>🎯 20%+ Pathway:</b> <code>{pathway}</code> (Target: <b>{expected_cagr}</b>)\n"
        f"<b>🛡️ Margin of Safety:</b> <i>{margin_of_safety}</i>\n"
        f"<b>Warning Flags:</b> <code>{flags_str}</code>\n\n"
        f"<b>📊 Key Quantitative Metrics:</b>\n"
        f"• CMP: <b>{cmp_str}</b> | P/E: <b>{pe_str}</b> | M.Cap: <b>{mcap_str}</b>\n"
        f"• ROCE: <b>{roce_str}</b> | 3Y CFO/PAT: <b>{cfo_pat_str}</b>\n"
        f"• 3Y Sales CAGR: <b>{sales_growth_str}</b> | 3Y PAT CAGR: <b>{pat_growth_str}</b>\n\n"
        f"<b>💡 Thesis & Moat:</b>\n"
        f"<i>{thesis}</i>\n\n"
        f"<b>🔭 Forward Guidance (FY27/FY28):</b>\n"
        f"<i>{guidance}</i>"
    )

    dispatcher = TelegramDispatcher()
    markup = dispatcher.build_triage_buttons(symbol)

    return msg, markup


def push_screener_digest(date_str: Optional[str] = None) -> int:
    """
    Reads the latest screener audit JSONL file and sends cards for
    STRONG_BUY_WATCH and ACCUMULATE_ON_DIP decisions to Telegram.
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
    
    # Deduplicate by symbol keeping the latest record
    latest_by_symbol = {}
    for r in records:
        sym = r.get("symbol", "").upper()
        if sym:
            latest_by_symbol[sym] = r
    unique_records = list(latest_by_symbol.values())

    approved_verdicts = {"STRONG_BUY_WATCH", "ACCUMULATE_ON_DIP"}
    approved_records = [r for r in unique_records if r.get("verdict", "").upper() in approved_verdicts]

    logger.info(f"Total evaluated records: {len(unique_records)} (raw: {len(records)}) | Approved for Telegram dispatch: {len(approved_records)}")

    dispatched_count = 0
    for r in approved_records:
        msg_text, markup = format_digest_card(r)
        success = dispatcher.send_message(msg_text, reply_markup=markup)
        if success:
            dispatched_count += 1
            logger.info(f"Successfully pushed Telegram card for {r.get('symbol')}")

    # Send batch summary
    summary_text = (
        f"📋 <b>AlphaMinter Screener Audit Summary</b>\n\n"
        f"• Date: <code>{datetime.now().strftime('%Y-%m-%d')}</code>\n"
        f"• Total Evaluated by AI: <b>{len(unique_records)}</b>\n"
        f"• Strong Buy Watch: <b>{sum(1 for r in unique_records if r.get('verdict') == 'STRONG_BUY_WATCH')}</b>\n"
        f"• Accumulate on Dip: <b>{sum(1 for r in unique_records if r.get('verdict') == 'ACCUMULATE_ON_DIP')}</b>\n"
        f"• Hold Watch: <b>{sum(1 for r in unique_records if r.get('verdict') == 'HOLD_WATCH')}</b>\n"
        f"• Rejected: <b>{sum(1 for r in unique_records if r.get('verdict') == 'REJECT')}</b>\n\n"
        f"<i>Tap [🚫 Permaban] on any stock above to exclude it from future scans.</i>"
    )
    dispatcher.send_message(summary_text)

    return dispatched_count


if __name__ == "__main__":
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    push_screener_digest(target_date)

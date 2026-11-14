import logging
from datetime import datetime
from src.scanners.guidance_scanner import GuidanceScanner
from src.notifications.telegram_dispatcher import TelegramDispatcher
from src.storage.state_store import StateStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AlphaMinterGuidance")

def run_guidance():
    logger.info("Starting AlphaMinter Dual-Stream Forward Guidance Pipeline (Gemini 3.5 Flash Lite)...")
    state_store = StateStore()
    dispatcher = TelegramDispatcher()
    scanner = GuidanceScanner(state_store=state_store)

    if not scanner.is_gemini_configured():
        logger.warning("GEMINI_API_KEY is not set in environment or secret. Skipping scan.")
        return

    # Run Dual-Stream Guidance Detection (BSE Filings + Google News RSS)
    hits = scanner.scan_all_guidance()
    logger.info(f"Identified {len(hits)} management forward guidance targets.")

    # 1. Dispatch Individual Alert Cards
    for h in hits:
        metrics = [
            f"<b>Source:</b> {h.get('source', 'Corporate Filing')}",
            f"<b>Guided Metric:</b> {h['metric']} Growth",
            f"<b>Target Growth:</b> <code>+{h['guidance_pct']}%</code>",
            f"<b>Target Period:</b> {h['period']} ({h.get('quarter_tag', '')})"
        ]
        
        thesis = f"Forward Guidance: {h['summary']}"

        dispatcher.send_stock_alert(
            title=f"🚀 20%+ FORWARD GUIDANCE: {h['symbol']}",
            symbol=h['symbol'],
            company=h['company'],
            metrics=metrics,
            thesis=thesis,
            buttons=True
        )

    # 2. Dispatch Summary Wrap
    today_str = datetime.now().strftime("%d-%b-%Y")
    summary_lines = [
        f"<b>🎯 ALPHAMINTER FORWARD GUIDANCE WRAP ({today_str})</b>",
        f"<i>Dual-Stream AI Detection: BSE Official Filings + Financial Media</i>\n"
    ]
    if hits:
        summary_lines.append("<b>Companies with >=20% Management Guidance:</b>")
        for h in hits:
            summary_lines.append(f"• <b>{h['symbol']}</b>: {h['metric']} +{h['guidance_pct']}% for {h['period']} ({h.get('source', '')})")
    else:
        summary_lines.append("<i>No new >=20% forward guidance disclosures detected today.</i>")

    summary_lines.append("\n<i>All disclosures parsed by Gemini 3.5 Flash Lite and deduplicated by quarter.</i>")
    dispatcher.send_message("\n".join(summary_lines))
    logger.info("AlphaMinter Forward Guidance Pipeline complete.")

if __name__ == "__main__":
    run_guidance()

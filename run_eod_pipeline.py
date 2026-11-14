import logging
from datetime import datetime
from src.scanners.bhavcopy_scanner import BhavcopyDeliveryScanner
from src.notifications.telegram_dispatcher import TelegramDispatcher
from src.storage.state_store import StateStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AlphaMinterEOD")

def run_eod():
    logger.info("Starting AlphaMinter EOD Automated Pipeline...")
    state_store = StateStore()
    dispatcher = TelegramDispatcher()
    
    # 1. Drain any 1-Tap Triage Buttons clicked on your phone over the last 24h
    logger.info("Checking for pending Telegram button taps...")
    state_store.drain_telegram_updates()

    # 2. Run Bhavcopy Delivery Accumulation Scanner
    logger.info("Executing Bhavcopy Delivery % Scanner...")
    delivery_scanner = BhavcopyDeliveryScanner(state_store=state_store)
    delivery_hits = delivery_scanner.scan_accumulation()
    
    # Filter Top 5 Clean Setups
    top_delivery = delivery_hits[:5]
    logger.info(f"Found {len(delivery_hits)} total delivery accumulation candidates. Surfacing top {len(top_delivery)}.")

    today_str = datetime.now().strftime("%d-%b-%Y")
    
    # 3. Dispatch Delivery Alerts with 1-Tap Triage Buttons
    if top_delivery:
        for stock in top_delivery:
            symbol = stock['symbol']
            
            # Prevent duplicate alerts on same date
            if state_store.is_already_alerted_today(symbol, "DELIVERY_ACCUMULATION"):
                continue

            metrics = [
                f"<b>Delivery %:</b> <code>{stock['delivery_pct']}%</code>",
                f"<b>CMP:</b> ₹{stock['cmp']} (Day Change: {stock['price_change_pct']}%)",
                f"<b>Delivery Turnover:</b> ₹{stock['delivery_turnover_cr']} Cr ({stock['traded_qty']:,} shares)"
            ]
            
            thesis = "High institutional delivery turnover in tight price range indicates smart money accumulation before breakout."
            
            dispatcher.send_stock_alert(
                title=f"🚨 SWING ACCUMULATION: {symbol}",
                symbol=symbol,
                company=symbol,
                metrics=metrics,
                thesis=thesis,
                buttons=True
            )
            
            state_store.log_alert(symbol, "DELIVERY_ACCUMULATION", f"Delivery: {stock['delivery_pct']}%")

    # 4. Dispatch Executive Daily EOD Summary
    summary_lines = [
        f"<b>📊 ALPHAMINTER EOD WRAP ({today_str})</b>",
        f"<i>Automated Market Scan (Delivery Spikes & Smart Money)</i>\n"
    ]

    if top_delivery:
        summary_lines.append("<b>Top Institutional Accumulation Today:</b>")
        for s in top_delivery:
            summary_lines.append(f"• <b>{s['symbol']}</b>: Deliv <code>{s['delivery_pct']}%</code> | ₹{s['delivery_turnover_cr']} Cr Turnover | CMP ₹{s['cmp']}")
    else:
        summary_lines.append("<i>No high-conviction delivery spikes above threshold today.</i>")

    summary_lines.append("\n<i>Tap buttons on alert cards above to Watchlist, Drop for 30 Days, or Permaban.</i>")
    
    full_summary = "\n".join(summary_lines)
    dispatcher.send_message(full_summary)
    logger.info("AlphaMinter EOD Pipeline completed successfully.")

if __name__ == "__main__":
    run_eod()

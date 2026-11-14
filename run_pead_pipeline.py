import logging
from datetime import datetime
from src.scanners.pead_scanner import PEADScanner
from src.scanners.bhavcopy_scanner import BhavcopyDeliveryScanner
from src.notifications.telegram_dispatcher import TelegramDispatcher
from src.storage.state_store import StateStore
from src.scanners.equity_filter import EquityFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AlphaMinterPEAD")

def run_pead():
    logger.info("Starting AlphaMinter PEAD (Post-Earnings Announcement Drift) Automated Pipeline...")
    state_store = StateStore()
    dispatcher = TelegramDispatcher()
    equity_filter = EquityFilter()
    pead_scanner = PEADScanner(state_store=state_store)
    bhav_scanner = BhavcopyDeliveryScanner(state_store=state_store)

    # 1. Fetch today's / latest Bhavcopy rows for market reaction confirmation
    bhav_rows, is_today = bhav_scanner.fetch_bhavcopy_rows()
    bhav_lookup = {}
    for r in bhav_rows:
        if str(r.get('SERIES', '')).strip() == 'EQ':
            sym = str(r.get('SYMBOL', '')).strip().upper()
            if sym:
                bhav_lookup[sym] = r

    logger.info(f"Loaded Bhavcopy lookup with {len(bhav_lookup)} EQ tickers.")

    # 2. Fetch recent BSE financial results filings
    results_filings = pead_scanner.fetch_bse_financial_results()
    logger.info(f"Polled {len(results_filings)} corporate financial results filings from BSE.")

    qualified_pead = []

    for filing in results_filings:
        scrip = filing.get('scrip_code', '')
        company = filing.get('company_name', '')
        headline = filing.get('headline', '')
        
        # Resolve to valid NSE operating equity symbol
        symbol = equity_filter.resolve_symbol(company, scrip)
        if not symbol or state_store.is_ticker_silenced(symbol):
            continue

        bhav_rec = bhav_lookup.get(symbol)
        if not bhav_rec:
            continue

        try:
            close_p = float(str(bhav_rec.get('CLOSE_PRICE', '0')).replace(',', '').strip() or 0)
            prev_close = float(str(bhav_rec.get('PREV_CLOSE', '0')).replace(',', '').strip() or 0)
            high_p = float(str(bhav_rec.get('HIGH_PRICE', '0')).replace(',', '').strip() or 0)
            low_p = float(str(bhav_rec.get('LOW_PRICE', '0')).replace(',', '').strip() or 0)
            deliv_pct = float(str(bhav_rec.get('DELIV_PER', '0')).replace(',', '').strip() or 0)
            traded_qty = int(float(str(bhav_rec.get('TTL_TRD_QNTY', '0')).replace(',', '').strip() or 0))
            deliv_qty = float(str(bhav_rec.get('DELIV_QTY', '0')).replace(',', '').strip() or 0)
        except (ValueError, TypeError):
            continue

        if prev_close <= 0 or close_p <= 0:
            continue

        price_change_pct = ((close_p - prev_close) / prev_close) * 100.0

        # Filter 2: Must show institutional positive reaction on results day
        # Minimum +4.0% price reaction and >= 50% delivery
        if price_change_pct < 4.0 or deliv_pct < 50.0:
            continue

        # Delivery turnover check (min ₹3 Cr for PEAD institutional presence)
        deliv_cr = (deliv_qty * close_p) / 10000000.0
        if deliv_cr < 3.0:
            continue

        # Check circuit feasibility: high == low indicates locked at upper circuit
        is_upper_circuit = (high_p == low_p and price_change_pct >= 4.0)

        metrics = [
            f"<b>Price Reaction:</b> +{round(price_change_pct, 2)}%",
            f"<b>Delivery %:</b> <code>{round(deliv_pct, 2)}%</code>",
            f"<b>Delivery Turnover:</b> ₹{round(deliv_cr, 2)} Cr ({traded_qty:,} shares)",
            f"<b>CMP:</b> ₹{round(close_p, 2)}",
            f"<b>Filing Details:</b> {headline[:60]}..."
        ]

        title = f"🚀 PEAD EARNINGS DRIFT: {symbol}" if not is_upper_circuit else f"🔒 PEAD UPPER CIRCUIT LOCK: {symbol}"
        thesis = (
            f"Post-Earnings Institutional Reaction: Stock reacted +{round(price_change_pct, 1)}% "
            f"with {round(deliv_pct, 1)}% delivery (₹{round(deliv_cr, 1)} Cr permanently absorbed). "
            f"{'Locked in upper circuit — watch for opening pullback.' if is_upper_circuit else 'Prime candidate for 15-60 day PEAD drift.'}"
        )

        qualified_pead.append({
            "symbol": symbol,
            "company": company or symbol,
            "cmp": round(close_p, 2),
            "price_change_pct": round(price_change_pct, 2),
            "delivery_pct": round(deliv_pct, 2),
            "deliv_cr": round(deliv_cr, 2),
            "metrics": metrics,
            "thesis": thesis,
            "title": title
        })

    # Sort by delivery turnover descending
    qualified_pead.sort(key=lambda x: x['deliv_cr'], reverse=True)
    top_pead = qualified_pead[:5]

    logger.info(f"Identified {len(qualified_pead)} qualified PEAD setups. Surfacing top {len(top_pead)}.")

    # 3. Dispatch Alerts
    for setup in top_pead:
        dispatcher.send_stock_alert(
            title=setup['title'],
            symbol=setup['symbol'],
            company=setup['company'],
            metrics=setup['metrics'],
            thesis=setup['thesis'],
            buttons=True
        )

    # 4. Dispatch EOD PEAD Summary
    today_str = datetime.now().strftime("%d-%b-%Y")
    summary_lines = [
        f"<b>📈 ALPHAMINTER PEAD DRIFT WRAP ({today_str})</b>",
        f"<i>Post-Earnings Announcement Institutional Reactions</i>\n"
    ]
    if top_pead:
        summary_lines.append("<b>Top Post-Earnings Drift Setups Today:</b>")
        for q in top_pead:
            summary_lines.append(f"• <b>{q['symbol']}</b>: Reacted +{q['price_change_pct']}% | Deliv <code>{q['delivery_pct']}%</code> | ₹{q['deliv_cr']} Cr Turnover")
    else:
        summary_lines.append("<i>No companies met the strict +4% reaction and 50% delivery PEAD threshold today.</i>")

    summary_lines.append("\n<i>Tap buttons on alert cards above to Watchlist, Drop, or Permaban.</i>")
    dispatcher.send_message("\n".join(summary_lines))
    logger.info("AlphaMinter PEAD Pipeline complete.")

if __name__ == "__main__":
    run_pead()

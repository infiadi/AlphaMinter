import logging
import re
from datetime import datetime, timedelta
import requests
from config.settings import BSE_HEADERS
from src.arbitrage.buyback_arb import BuybackArbitrageCalculator
from src.scanners.bhavcopy_scanner import BhavcopyDeliveryScanner
from src.notifications.telegram_dispatcher import TelegramDispatcher
from src.storage.state_store import StateStore
from src.scanners.equity_filter import EquityFilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AlphaMinterBuyback")

def fetch_bse_buyback_announcements(session: requests.Session) -> list:
    """
    Polls BSE announcements searching for active share buybacks.
    Cloud-safe endpoint that does not block GitHub Actions runners.
    """
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    params = {
        "pageno": 1,
        "strCat": "Company Update",
        "strPrevDate": "",
        "strScrip": "",
        "strSearch": "P",
        "strToDate": "",
        "strType": "C"
    }
    
    buyback_items = []
    try:
        resp = session.get(url, params=params, timeout=20)
        if resp.status_code == 200:
            table = resp.json().get("Table", [])
            for item in table:
                headline = str(item.get("NEWSSUB", "")).strip()
                desc = str(item.get("MORE", "") or item.get("HEADLINE", "")).strip()
                full_text = f"{headline} {desc}"

                if re.search(r"\b(buyback|buy-back|buy back)\b", full_text, re.IGNORECASE):
                    buyback_items.append({
                        "scrip_code": str(item.get("SCRIP_CD", "")).strip(),
                        "company_name": str(item.get("SLONGNAME", "")).strip(),
                        "headline": headline,
                        "desc": desc,
                        "date": str(item.get("NEWS_DT", "")).strip()
                    })
    except Exception as e:
        logger.error(f"Error fetching BSE buyback filings: {e}")

    return buyback_items

def parse_buyback_details(text: str) -> dict:
    """
    Extracts buyback price, offer type, and record date using regex heuristics.
    """
    # Detect Buyback Type: Tender Offer vs Open Market
    offer_type = "TENDER_OFFER"
    if re.search(r"\b(open market|stock exchange mechanism)\b", text, re.IGNORECASE):
        offer_type = "OPEN_MARKET"

    # Extract Buyback Price: e.g. "Rs. 600 per equity share" or "₹600"
    price_match = re.search(r"(?:rs\.?|₹|inr|price of)\s*([0-9,]+(?:\.[0-9]+)?)\s*(?:per\s*(?:equity\s*)?share|-)", text, re.IGNORECASE)
    buyback_price = 0.0
    if price_match:
        try:
            buyback_price = float(price_match.group(1).replace(",", ""))
        except ValueError:
            buyback_price = 0.0

    # Extract Record Date: e.g. "Record date is November 28, 2026" or "28/11/2026"
    record_date_str = None
    date_match = re.search(r"record\s*date\s*(?:is|fixed\s*as|for\s*the\s*purpose\s*of)?\s*[:\-]?\s*([0-9]{1,2}[th|st|nd|rd]*\s+[A-Za-z]+,?\s+[0-9]{4}|[0-9]{1,2}[\/\-][0-9]{1,2}[\/\-][0-9]{2,4})", text, re.IGNORECASE)
    if date_match:
        raw_date = date_match.group(1).strip()
        # Clean ordinal indicators (14th -> 14)
        clean_date = re.sub(r"([0-9]+)(?:st|nd|rd|th)", r"\1", raw_date, flags=re.IGNORECASE)
        for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
            try:
                record_date_str = datetime.strptime(clean_date.replace(",", ""), fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    return {
        "offer_type": offer_type,
        "buyback_price": buyback_price,
        "record_date": record_date_str
    }

def run_buyback():
    logger.info("Starting AlphaMinter Share Buyback Arbitrage Automated Pipeline...")
    state_store = StateStore()
    dispatcher = TelegramDispatcher()
    equity_filter = EquityFilter()
    calculator = BuybackArbitrageCalculator(state_store=state_store)
    bhav_scanner = BhavcopyDeliveryScanner(state_store=state_store)

    session = requests.Session()
    session.headers.update(BSE_HEADERS)

    # 1. Fetch latest Bhavcopy for current market prices (CMP)
    bhav_rows, _ = bhav_scanner.fetch_bhavcopy_rows()
    cmp_lookup = {}
    for r in bhav_rows:
        sym = str(r.get('SYMBOL', '')).strip().upper()
        if sym and str(r.get('SERIES', '')).strip() == 'EQ':
            try:
                cmp_lookup[sym] = float(str(r.get('CLOSE_PRICE', '0')).replace(',', '').strip() or 0)
            except ValueError:
                pass

    # 2. Fetch Buyback Announcements from BSE
    buyback_filings = fetch_bse_buyback_announcements(session)
    logger.info(f"Polled {len(buyback_filings)} potential buyback notices from BSE.")

    viable_buybacks = []

    for filing in buyback_filings:
        scrip = filing['scrip_code']
        company = filing['company_name']
        full_text = f"{filing['headline']} {filing['desc']}"

        symbol = equity_filter.resolve_symbol(company, scrip)
        if not symbol or state_store.is_ticker_silenced(symbol):
            continue

        details = parse_buyback_details(full_text)
        cmp = cmp_lookup.get(symbol, 0.0)

        # Skip if price or CMP is unavailable
        if cmp <= 0 or details['buyback_price'] <= cmp:
            continue

        # Calculate Arbitrage Matrix
        arb_res = calculator.calculate_retail_arbitrage(
            symbol=symbol,
            cmp=cmp,
            buyback_price=details['buyback_price'],
            buyback_type=details['offer_type'],
            record_date_str=details['record_date']
        )

        if arb_res.get("is_viable"):
            base = arb_res['base_case']
            scenarios = arb_res['scenarios']

            metrics = [
                f"<b>Buyback Price:</b> ₹{arb_res['buyback_price']} (CMP: ₹{cmp})",
                f"<b>Gross Spread:</b> <code>+{arb_res['spread_pct']}%</code>",
                f"<b>Safe Retail Sizing:</b> {arb_res['safe_retail_shares']} shares (Cap: ₹{arb_res['total_investment']:,.0f})",
                f"<b>30% Acceptance Net Gain:</b> ₹{scenarios['30%']['net_gain_rs']:,.0f} (+{scenarios['30%']['net_return_pct']}%)",
                f"<b>50% Acceptance Net Gain:</b> ₹{scenarios['50%']['net_gain_rs']:,.0f} (+{scenarios['50%']['net_return_pct']}%)",
                f"<b>75% Acceptance Net Gain:</b> ₹{scenarios['75%']['net_gain_rs']:,.0f} (+{scenarios['75%']['net_return_pct']}%)",
                f"<b>Breakeven Cushion:</b> Unaccepted shares can drop <code>{base['drop_cushion_pct']}%</code> (to ₹{base['breakeven_price']})",
                f"<b>T-2 Buy Cutoff:</b> {arb_res['final_buy_cutoff_t2'] or 'To be announced'}"
            ]

            thesis = (
                f"SEBI 15% Small Shareholder Tender Arbitrage. Sizing under ₹1.85L ceiling protects category qualification. "
                f"Net post-tax expected return is +{base['net_return_pct']}% at 50% acceptance with a {base['drop_cushion_pct']}% downside buffer."
            )

            viable_buybacks.append({
                "symbol": symbol,
                "company": company or symbol,
                "metrics": metrics,
                "thesis": thesis,
                "spread_pct": arb_res['spread_pct'],
                "record_date": arb_res['record_date']
            })

    logger.info(f"Identified {len(viable_buybacks)} viable tender buyback opportunities.")

    # 3. Dispatch Alert Cards
    for bb in viable_buybacks[:3]:
        dispatcher.send_stock_alert(
            title=f"🎯 RETAIL BUYBACK ARBITRAGE: {bb['symbol']}",
            symbol=bb['symbol'],
            company=bb['company'],
            metrics=bb['metrics'],
            thesis=bb['thesis'],
            buttons=True
        )

    # 4. Dispatch EOD Buyback Wrap
    today_str = datetime.now().strftime("%d-%b-%Y")
    summary_lines = [
        f"<b>🎯 ALPHAMINTER BUYBACK ARBITRAGE WRAP ({today_str})</b>",
        f"<i>SEBI 15% Small Shareholder Tender Arbitrage</i>\n"
    ]
    if viable_buybacks:
        summary_lines.append("<b>Active Viable Tender Offers:</b>")
        for b in viable_buybacks:
            summary_lines.append(f"• <b>{b['symbol']}</b>: Spread +{b['spread_pct']}% | Record Date: {b['record_date'] or 'TBD'}")
    else:
        summary_lines.append("<i>No tender buybacks currently within the viable spread (>=12%) and cutoff window.</i>")

    summary_lines.append("\n<i>All calculations model safe ₹1.85L sizing and 20% STCG tax drag.</i>")
    dispatcher.send_message("\n".join(summary_lines))
    logger.info("AlphaMinter Buyback Pipeline complete.")

if __name__ == "__main__":
    run_buyback()

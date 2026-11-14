import os
import re
import json
import time
import random
import logging
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Ensure environment is loaded
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

SESSION_CACHE_PATH = BASE_DIR / "data" / ".screener_session.json"
ENV_PATH = BASE_DIR / ".env"


def clean_numeric(val: Any) -> Optional[float]:
    """
    Cleans raw table/metric strings into float:
    Strips %, Cr., ₹, commas, and handles '-' or empty strings as None.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)

    s = str(val).strip()
    if not s or s in ("-", "--", "None", "null", "N/A", "nan"):
        return None

    # Remove currency, units, commas, extra whitespace
    cleaned = re.sub(r"[₹,%\s]", "", s)
    cleaned = re.sub(r"(Cr\.?|Lakhs?|k)$", "", cleaned, flags=re.IGNORECASE)

    try:
        return float(cleaned)
    except ValueError:
        return None


class ScreenerAuth:
    """
    Cookie-first session manager for Screener.in:
    1. Loads cached session ID from data/.screener_session.json or .env.
    2. Auto-logins via dummy account (SCREENER_EMAIL + SCREENER_PASSWORD)
       handling csrfmiddlewaretoken if redirected to /login/.
    3. Persists renewed session ID back to data/.screener_session.json and .env.
    """
    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id or self._load_session_id()
        self.email = os.getenv("SCREENER_EMAIL", "").strip()
        self.password = os.getenv("SCREENER_PASSWORD", "").strip()

    def _load_session_id(self) -> str:
        # Check cached session file first
        if SESSION_CACHE_PATH.exists():
            try:
                with open(SESSION_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    sid = data.get("sessionid", "").strip()
                    if sid:
                        return sid
            except Exception as e:
                logger.warning(f"Could not read {SESSION_CACHE_PATH}: {e}")

        # Check environment
        return os.getenv("SCREENER_SESSION_ID", "").strip()

    def save_session_id(self, sid: str):
        self.session_id = sid
        # Save to data/.screener_session.json
        try:
            SESSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SESSION_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"sessionid": sid, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}, f, indent=2)
            logger.info("Saved refreshed session ID to data/.screener_session.json")
        except Exception as e:
            logger.error(f"Failed to write {SESSION_CACHE_PATH}: {e}")

        # Update .env if writable
        try:
            if ENV_PATH.exists():
                with open(ENV_PATH, "r", encoding="utf-8") as f:
                    content = f.read()
                if "SCREENER_SESSION_ID=" in content:
                    content = re.sub(r"SCREENER_SESSION_ID=.*", f"SCREENER_SESSION_ID={sid}", content)
                else:
                    content += f"\nSCREENER_SESSION_ID={sid}\n"
                with open(ENV_PATH, "w", encoding="utf-8") as f:
                    f.write(content)
                logger.info("Updated SCREENER_SESSION_ID in .env")
        except Exception as e:
            logger.warning(f"Could not update .env: {e}")

    def login(self, session: requests.Session) -> bool:
        """Attempts dummy account login using csrfmiddlewaretoken."""
        if not self.email or not self.password:
            logger.warning("SCREENER_EMAIL or SCREENER_PASSWORD not set. Cannot perform auto-login.")
            return False

        login_url = "https://www.screener.in/login/"
        try:
            logger.info(f"Attempting Screener auto-login as {self.email}...")
            get_resp = session.get(login_url, headers=DEFAULT_HEADERS, timeout=12)
            if get_resp.status_code != 200:
                logger.error(f"Failed to fetch login page: HTTP {get_resp.status_code}")
                return False

            soup = BeautifulSoup(get_resp.text, "html.parser")
            csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
            if not csrf_input or not csrf_input.get("value"):
                logger.error("Could not locate csrfmiddlewaretoken on Screener login page.")
                return False

            csrf_token = csrf_input["value"]
            payload = {
                "csrfmiddlewaretoken": csrf_token,
                "username": self.email,
                "password": self.password,
                "next": "/"
            }
            post_headers = dict(DEFAULT_HEADERS)
            post_headers["Referer"] = login_url

            post_resp = session.post(login_url, data=payload, headers=post_headers, timeout=15, allow_redirects=True)
            new_sid = session.cookies.get("sessionid") or post_resp.cookies.get("sessionid")

            if new_sid:
                logger.info("Auto-login successful! Captured new sessionid.")
                self.save_session_id(new_sid)
                return True
            elif "Invalid username or password" in post_resp.text:
                logger.error("Auto-login failed: Invalid username or password.")
                return False
            else:
                logger.warning(f"Auto-login response status {post_resp.status_code}, but sessionid cookie was not found.")
                return False
        except Exception as e:
            logger.error(f"Exception during Screener auto-login: {e}")
            return False


class CustomScreenerParser:
    """
    Lean, robust BeautifulSoup parser for Screener.in:
    - Saved Screen Crawler (crawls multi-page /screens/<id>/<slug>/)
    - Company Financial Dossier Fetcher (/company/<SYMBOL>/consolidated/ with fallback)
    - Derived Financial Indicators (CFO/PAT, Debt/Equity, QoQ/YoY margins, promoter changes)
    """
    def __init__(self, request_delay_sec: float = 2.0):
        self.request_delay_sec = max(request_delay_sec, 1.5)
        self.auth = ScreenerAuth()
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        if self.auth.session_id:
            self.session.cookies.set("sessionid", self.auth.session_id, domain=".screener.in")

    def _paced_sleep(self):
        """Enforces jittered pacing between requests to respect rate limits."""
        jitter = random.uniform(0.9, 1.25)
        time.sleep(self.request_delay_sec * jitter)

    def _request_with_retry(self, url: str, max_retries: int = 3) -> Optional[requests.Response]:
        """Performs HTTP GET with rate limit (429/503) backoff and login redirect detection."""
        for attempt in range(1, max_retries + 1):
            try:
                self._paced_sleep()
                resp = self.session.get(url, timeout=15, allow_redirects=True)

                # Check if redirected to login
                if resp.status_code == 302 or "/login/" in resp.url or "Login - Screener" in resp.text:
                    logger.warning(f"Redirected to login on {url}. Attempting auto-login refresh...")
                    if self.auth.login(self.session):
                        # Re-try with renewed session
                        return self.session.get(url, timeout=15, allow_redirects=True)
                    else:
                        logger.error("Authentication refresh failed.")
                        return resp

                if resp.status_code in (429, 503):
                    wait_time = (2 ** attempt) + random.uniform(1.0, 3.0)
                    logger.warning(f"HTTP {resp.status_code} received from {url}. Backing off {wait_time:.1f}s (Attempt {attempt}/{max_retries})...")
                    time.sleep(wait_time)
                    continue

                return resp
            except Exception as e:
                logger.warning(f"Network error on {url} (Attempt {attempt}/{max_retries}): {e}")
                time.sleep(2.0 * attempt)

        logger.error(f"Failed to fetch {url} after {max_retries} attempts.")
        return None

    def parse_saved_screen(self, screen_cfg: Dict[str, Any], max_pages: int = 10) -> List[Dict[str, Any]]:
        """
        Paginates through a saved screen /screens/<id>/<slug>/?page=1..N.
        Parses the <table class="data-table">, extracts symbols and visible metrics.
        """
        screen_url = screen_cfg.get("url", "").strip()
        screen_name = screen_cfg.get("name", "Unknown Screen")
        if not screen_url:
            logger.warning(f"No URL specified for screen: {screen_name}")
            return []

        # Strip existing query params
        base_screen_url = screen_url.split("?")[0].rstrip("/") + "/"
        page = 1
        all_screen_stocks: List[Dict[str, Any]] = []
        seen_symbols = set()

        logger.info(f"Crawling screen '{screen_name}': {base_screen_url}")

        while page <= max_pages:
            url = f"{base_screen_url}?page={page}"
            resp = self._request_with_retry(url)
            if not resp or resp.status_code != 200:
                logger.warning(f"Could not fetch page {page} for '{screen_name}'. Ending crawl.")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", class_="data-table")
            if not table:
                logger.info(f"No data-table found on page {page} for '{screen_name}'. Crawl complete.")
                break

            # Parse headers from the first header row
            headers = []
            header_row = table.find("tr")
            if header_row:
                for th in header_row.find_all("th"):
                    h_text = th.get_text(strip=True)
                    # Clean header name
                    h_clean = re.sub(r"\s+", " ", h_text)
                    headers.append(h_clean)

            # Find all data rows (tr containing td)
            rows = table.find_all("tr")
            page_found = 0

            for tr in rows:
                tds = tr.find_all("td")
                if not tds:
                    continue  # Header or separator row

                # Extract company link and symbol
                link_tag = tr.find("a", href=re.compile(r"/company/[A-Z0-9_\-]+/?"))
                if not link_tag:
                    continue

                href = link_tag.get("href", "")
                m = re.search(r"/company/([A-Z0-9_\-]+)/?", href)
                if not m:
                    continue

                symbol = m.group(1).upper()
                company_name = link_tag.get_text(strip=True)

                if symbol in seen_symbols:
                    continue
                seen_symbols.add(symbol)
                page_found += 1

                # Map column values
                raw_columns: Dict[str, Any] = {}
                for idx, td in enumerate(tds):
                    col_name = headers[idx] if idx < len(headers) else f"col_{idx}"
                    if not col_name or col_name in ("S.No.", ""):
                        continue
                    txt = td.get_text(strip=True)
                    num = clean_numeric(txt)
                    raw_columns[col_name] = num if num is not None else txt

                all_screen_stocks.append({
                    "symbol": symbol,
                    "name": company_name,
                    "company_url": href,
                    "screen_id": screen_cfg.get("id", ""),
                    "screen_name": screen_name,
                    "columns": raw_columns
                })

            logger.info(f"Screen '{screen_name}' [Page {page}]: Found {page_found} stocks (Total: {len(all_screen_stocks)})")

            if page_found == 0:
                break

            # Check for next page link in pagination
            has_next = False
            for a in soup.find_all("a"):
                href_a = a.get("href", "")
                text_a = a.get_text(strip=True).lower()
                if f"page={page+1}" in href_a or text_a == "next":
                    has_next = True
                    break

            if not has_next:
                logger.info(f"No Next page found for '{screen_name}'. Crawl finished at page {page}.")
                break

            page += 1

        return all_screen_stocks

    def fetch_company_dossier(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetches full financial dossier for a symbol:
        1. Requests /company/{symbol}/consolidated/ first.
        2. If top ratios or quarters table is unpopulated, falls back to /company/{symbol}/.
        3. Parses ratios, 3Y/5Y growth, quarters, P&L, Balance Sheet, Cash Flow, Shareholding.
        4. Calculates derived ratios (cfo_to_pat_3y, debt_to_equity, QoQ/YoY margins, etc.).
        """
        symbol = symbol.strip().upper()
        consolidated_url = f"https://www.screener.in/company/{symbol}/consolidated/"
        standalone_url = f"https://www.screener.in/company/{symbol}/"

        resp = self._request_with_retry(consolidated_url)
        is_consolidated = True

        # Fallback check: If consolidated fails or has empty top ratios/quarters
        needs_fallback = False
        if not resp or resp.status_code != 200:
            needs_fallback = True
        else:
            soup_test = BeautifulSoup(resp.text, "html.parser")
            top_test = soup_test.find(id="top-ratios")
            first_num = top_test.find(class_="number") if top_test else None
            # If top-ratios numbers are empty strings (like Page Industries consolidated)
            if not first_num or not first_num.get_text(strip=True):
                needs_fallback = True

        if needs_fallback:
            logger.info(f"{symbol}: Consolidated data incomplete or unavailable. Falling back to standalone.")
            resp = self._request_with_retry(standalone_url)
            is_consolidated = False

        if not resp or resp.status_code != 200:
            logger.error(f"Could not retrieve company page for {symbol} (HTTP {resp.status_code if resp else 'No Resp'}).")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        return self._extract_dossier_from_soup(symbol, soup, is_consolidated)

    def _extract_dossier_from_soup(self, symbol: str, soup: BeautifulSoup, is_consolidated: bool) -> Dict[str, Any]:
        """Extracts and computes all structured tables and ratios from BeautifulSoup page."""
        # 1. Company Name & About
        h1 = soup.find("h1")
        company_name = h1.get_text(strip=True) if h1 else symbol
        about_div = soup.find("div", class_="about")
        about_text = about_div.get_text(strip=True) if about_div else ""

        # 2. Sector and Industry from #peers
        peers_sec = soup.find("section", id="peers")
        market_links = []
        if peers_sec:
            for l in peers_sec.find_all("a"):
                if "/market/" in l.get("href", ""):
                    market_links.append(l.get_text(strip=True))

        sector = market_links[0] if len(market_links) > 0 else "Unknown"
        industry = market_links[-1] if len(market_links) > 1 else sector

        # 3. Top Ratios
        top_ratios: Dict[str, Any] = {}
        top_ul = soup.find(id="top-ratios")
        if top_ul:
            for li in top_ul.find_all("li"):
                name_el = li.find(class_="name")
                val_el = li.find(class_="number") or li.find(class_="value")
                if name_el and val_el:
                    k = re.sub(r"\s+", " ", name_el.get_text()).strip()
                    val_text = val_el.get_text(strip=True)
                    num = clean_numeric(val_text)
                    top_ratios[k] = num if num is not None else val_text

        # 4. Pros & Cons
        pros = [li.get_text(strip=True) for li in soup.select(".pros li")]
        cons = [li.get_text(strip=True) for li in soup.select(".cons li")]

        # 5. Compounded Growth Tables (.ranges-table)
        compounded_growth: Dict[str, Dict[str, str]] = {}
        for rt in soup.find_all("table", class_="ranges-table"):
            th = rt.find("th")
            category = th.get_text(strip=True) if th else "Growth"
            cat_dict = {}
            for tr in rt.find_all("tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) >= 2:
                    period_key = tds[0].rstrip(":")
                    cat_dict[period_key] = tds[1]
            if cat_dict:
                compounded_growth[category] = cat_dict

        # 6. Helper for Standard Financial Tables
        def parse_table_section(sec_id: str) -> Tuple[List[str], Dict[str, List[Optional[float]]]]:
            sec = soup.find("section", id=sec_id)
            if not sec:
                return [], {}
            tbl = sec.find("table")
            if not tbl:
                return [], {}

            headers = []
            header_row = tbl.find("tr")
            if header_row:
                headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]
                if headers and headers[0] == "":
                    headers = headers[1:]

            rows_dict: Dict[str, List[Optional[float]]] = {}
            for tr in tbl.find_all("tr")[1:]:
                cells = tr.find_all(["td", "th"])
                if len(cells) > 1:
                    raw_row_name = cells[0].get_text(strip=True)
                    clean_row_name = re.sub(r"[\+\:\s]+$", "", raw_row_name).strip()
                    row_vals = [clean_numeric(c.get_text(strip=True)) for c in cells[1:]]
                    rows_dict[clean_row_name] = row_vals

            return headers, rows_dict

        q_headers, quarters = parse_table_section("quarters")
        pl_headers, profit_loss = parse_table_section("profit-loss")
        bs_headers, balance_sheet = parse_table_section("balance-sheet")
        cf_headers, cash_flows = parse_table_section("cash-flow")
        sh_headers, shareholding = parse_table_section("shareholding")

        # 7. Calculate Derived Ratios
        derived = self._calculate_derived_ratios(
            top_ratios=top_ratios,
            quarters=quarters,
            profit_loss=profit_loss,
            balance_sheet=balance_sheet,
            cash_flows=cash_flows,
            shareholding=shareholding,
            sector=sector,
            industry=industry
        )

        return {
            "symbol": symbol,
            "name": company_name,
            "about": about_text,
            "is_consolidated": is_consolidated,
            "sector": sector,
            "industry": industry,
            "top_ratios": top_ratios,
            "pros": pros,
            "cons": cons,
            "compounded_growth": compounded_growth,
            "quarters": {
                "headers": q_headers,
                "data": quarters
            },
            "profit_loss": {
                "headers": pl_headers,
                "data": profit_loss
            },
            "balance_sheet": {
                "headers": bs_headers,
                "data": balance_sheet
            },
            "cash_flows": {
                "headers": cf_headers,
                "data": cash_flows
            },
            "shareholding": {
                "headers": sh_headers,
                "data": shareholding
            },
            "derived_metrics": derived
        }

    def _calculate_derived_ratios(
        self,
        top_ratios: Dict[str, Any],
        quarters: Dict[str, List[Optional[float]]],
        profit_loss: Dict[str, List[Optional[float]]],
        balance_sheet: Dict[str, List[Optional[float]]],
        cash_flows: Dict[str, List[Optional[float]]],
        shareholding: Dict[str, List[Optional[float]]],
        sector: str,
        industry: str
    ) -> Dict[str, Any]:
        """Calculates 3Y cash conversion, debt-to-equity, QoQ/YoY margins, and promoter trends."""
        # 1. 3Y CFO to PAT
        cfo_row = cash_flows.get("Cash from Operating Activity") or []
        pat_row = profit_loss.get("Net Profit") or []

        cfo_3y = sum([v for v in cfo_row[-3:] if v is not None]) if len(cfo_row) >= 3 else None
        pat_3y = sum([v for v in pat_row[-3:] if v is not None]) if len(pat_row) >= 3 else None

        cfo_to_pat_3y = None
        if cfo_3y is not None and pat_3y is not None:
            if pat_3y > 0:
                cfo_to_pat_3y = round(cfo_3y / pat_3y, 2)
            elif pat_3y < 0:
                cfo_to_pat_3y = 0.0  # Loss making
            else:
                cfo_to_pat_3y = 1.0 if cfo_3y >= 0 else 0.0

        # 2. 3Y CFO to EBITDA / Operating Profit
        op_profit_row = profit_loss.get("Operating Profit") or []
        ebitda_3y = sum([v for v in op_profit_row[-3:] if v is not None]) if len(op_profit_row) >= 3 else None
        cfo_to_ebitda_3y = None
        if cfo_3y is not None and ebitda_3y is not None and ebitda_3y > 0:
            cfo_to_ebitda_3y = round(cfo_3y / ebitda_3y, 2)

        # 3. Debt to Equity
        debt_to_equity = top_ratios.get("Debt to equity")
        if debt_to_equity is None:
            borrowings_row = balance_sheet.get("Borrowings") or []
            equity_cap_row = balance_sheet.get("Equity Capital") or []
            reserves_row = balance_sheet.get("Reserves") or []
            if borrowings_row and equity_cap_row and reserves_row:
                latest_borrowings = borrowings_row[-1] or 0.0
                latest_equity = (equity_cap_row[-1] or 0.0) + (reserves_row[-1] or 0.0)
                if latest_equity > 0:
                    debt_to_equity = round(latest_borrowings / latest_equity, 2)
                elif latest_borrowings == 0.0:
                    debt_to_equity = 0.0

        # 4. QoQ & YoY OPM (Operating Profit Margin)
        opm_row = quarters.get("OPM %") or []
        qoq_opm_delta_bps = None
        latest_qtr_yoy_opm_bps = None
        if len(opm_row) >= 2 and opm_row[-1] is not None and opm_row[-2] is not None:
            qoq_opm_delta_bps = round((opm_row[-1] - opm_row[-2]) * 100, 1)

        if len(opm_row) >= 5 and opm_row[-1] is not None and opm_row[-5] is not None:
            latest_qtr_yoy_opm_bps = round((opm_row[-1] - opm_row[-5]) * 100, 1)

        # 5. Latest Quarter YoY Sales Growth
        sales_row = quarters.get("Sales") or []
        latest_qtr_yoy_sales_pct = None
        if len(sales_row) >= 5 and sales_row[-1] is not None and sales_row[-5] is not None:
            prior = sales_row[-5]
            if prior != 0:
                latest_qtr_yoy_sales_pct = round(((sales_row[-1] - prior) / abs(prior)) * 100, 2)

        # 6. Latest Quarter YoY PAT Growth
        q_pat_row = quarters.get("Net Profit") or []
        latest_qtr_yoy_pat_pct = None
        if len(q_pat_row) >= 5 and q_pat_row[-1] is not None and q_pat_row[-5] is not None:
            prior_pat = q_pat_row[-5]
            if prior_pat != 0:
                latest_qtr_yoy_pat_pct = round(((q_pat_row[-1] - prior_pat) / abs(prior_pat)) * 100, 2)

        # 7. Promoter Pledging & QoQ Change
        promoter_pledge_pct = top_ratios.get("Pledged percentage")
        if promoter_pledge_pct is None:
            promoter_pledge_pct = 0.0

        promoters_row = shareholding.get("Promoters") or []
        promoter_qoq_change_pct = None
        if len(promoters_row) >= 2 and promoters_row[-1] is not None and promoters_row[-2] is not None:
            promoter_qoq_change_pct = round(promoters_row[-1] - promoters_row[-2], 2)

        roce_pct = top_ratios.get("ROCE")
        roe_pct = top_ratios.get("ROE")
        market_cap_cr = top_ratios.get("Market Cap")
        pe = top_ratios.get("Stock P/E")
        cmp = top_ratios.get("Current Price")

        return {
            "market_cap_cr": market_cap_cr,
            "current_price": cmp,
            "pe": pe,
            "roce_pct": roce_pct,
            "roe_pct": roe_pct,
            "cfo_to_pat_3y": cfo_to_pat_3y,
            "cfo_to_ebitda_3y": cfo_to_ebitda_3y,
            "debt_to_equity": debt_to_equity,
            "qoq_opm_delta_bps": qoq_opm_delta_bps,
            "latest_qtr_yoy_opm_bps": latest_qtr_yoy_opm_bps,
            "latest_qtr_yoy_sales_pct": latest_qtr_yoy_sales_pct,
            "latest_qtr_yoy_pat_pct": latest_qtr_yoy_pat_pct,
            "promoter_pledge_pct": promoter_pledge_pct,
            "promoter_qoq_change_pct": promoter_qoq_change_pct,
            "is_financial_sector": any(k in f"{sector} {industry}".lower() for k in ["bank", "finance", "nbfc", "housing finance"])
        }

import json
import logging
import os
import re
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET
import requests
from config.settings import BSE_HEADERS, GEMINI_API_KEY, GEMINI_MODEL, BASE_DIR
from src.storage.state_store import StateStore
from src.scanners.equity_filter import EquityFilter

logger = logging.getLogger(__name__)

class GuidanceScanner:
    """
    Dual-Source Forward Management Guidance Scanner (v2.5.0):
    1. Primary Stream: Official BSE Corporate LODR Disclosures (Investor Presentations, Concall Transcripts, Press Releases).
    2. Secondary Stream: Real-Time Google News RSS (Moneycontrol, Economic Times, Livemint, CNBC-TV18).
       - Zero Cloudflare / Akamai blocking risks on GitHub Actions.
       - Captures CEO/MD media interviews and market briefings.
    3. Quantitative LLM Extraction: Uses Gemini 3.5 Flash Lite (500 req/day free tier) with strict JSON output.
    4. Guardrails:
       - Dynamic FY/Quarter detection (zero hardcoded strings).
       - (Symbol, Quarter) deduplication prevents multiple filings in the same quarter from spamming alerts.
       - Paced 3.0s delay between LLM calls to respect free-tier RPM/TPM limits.
    """
    def __init__(self, state_store: StateStore = None, gemini_api_key: str = GEMINI_API_KEY, model: str = GEMINI_MODEL):
        self.state_store = state_store or StateStore()
        self.equity_filter = EquityFilter()
        self.api_key = gemini_api_key.strip()
        self.model = model
        self.session = requests.Session()
        self.session.headers.update(BSE_HEADERS)
        self.history = self._load_history()

    def is_gemini_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "your_gemini_api_key_here")

    def _get_current_and_next_fy(self) -> Tuple[str, str, str]:
        """
        Dynamically calculates current and next Indian Fiscal Year and quarter based on current date.
        Zero hardcoded year values.
        """
        today = datetime.now()
        # In India, Fiscal Year starts April 1 (month 4)
        if today.month >= 4:
            current_fy_end = today.year + 1
        else:
            current_fy_end = today.year
            
        current_fy = f"FY{str(current_fy_end)[-2:]}"
        next_fy = f"FY{str(current_fy_end + 1)[-2:]}"

        if today.month in [4, 5, 6]:
            qtr = "Q1"
        elif today.month in [7, 8, 9]:
            qtr = "Q2"
        elif today.month in [10, 11, 12]:
            qtr = "Q3"
        else:
            qtr = "Q4"

        quarter_tag = f"{qtr}{current_fy}"
        return current_fy, next_fy, quarter_tag

    def _load_history(self) -> Dict[str, str]:
        path = BASE_DIR / "data" / "guidance_history.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_history(self):
        path = BASE_DIR / "data" / "guidance_history.json"
        try:
            path.parent.mkdir(exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist guidance history: {e}")

    def _extract_relevant_guidance_text(self, text: str, max_chars: int = 8000) -> str:
        """
        Slices text to keep only paragraphs mentioning forward guidance or operational targets.
        Protects against TPM limits from large disclosures.
        """
        keywords = re.compile(r"(guidance|outlook|target|growth|margin|revenue|profit|PAT|EBITDA|capex|order book|CAGR)", re.IGNORECASE)
        paragraphs = text.split("\n")
        relevant_blocks = [p.strip() for p in paragraphs if keywords.search(p) and len(p.strip()) > 30]

        if relevant_blocks:
            combined = "\n\n".join(relevant_blocks)
            return combined[:max_chars]
        return text[:max_chars]

    def analyze_guidance_with_gemini(self, context_title: str, raw_text: str) -> Optional[Dict]:
        """
        Calls Gemini 3.5 Flash Lite with structured JSON schema and dynamic period detection.
        """
        if not self.is_gemini_configured():
            logger.warning("GEMINI_API_KEY not configured. Skipping LLM guidance extraction.")
            return None

        current_fy, next_fy, quarter_tag = self._get_current_and_next_fy()
        filtered_text = self._extract_relevant_guidance_text(raw_text)

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        
        prompt = f"""
        You are a quantitative Indian equity research analyst.
        Analyze this corporate announcement or financial news article: "{context_title}".
        Target Fiscal Periods: Current ({current_fy}) or Upcoming ({next_fy}).
        
        Content Text:
        \"\"\"{filtered_text}\"\"\"

        CRITICAL FILTERING RULES:
        1. ACCEPT ONLY forward-looking management guidance for {current_fy} or {next_fy}.
        2. REJECT historical past performance ("we grew 25% last year").
        3. REJECT vague aspirational multi-decade visions ("we hope to be ₹10,000 Cr by 2035").
        4. Determine if management explicitly guided for:
           - 20% or higher PAT (Profit After Tax) growth, OR
           - 20% or higher Revenue/Volume growth, OR
           - Major Capex expansion > 15% of gross block.

        Return strict JSON only (no markdown fences, no backticks):
        {{
            "has_20pct_guidance": true or false,
            "symbol": "NSE/BSE stock ticker or company name",
            "metric": "PAT" or "Revenue" or "Volume" or "Capex" or "None",
            "guidance_pct": float or null,
            "target_period": "{current_fy}" or "{next_fy}" or "Multi-Year",
            "summary": "1 concise sentence explaining the specific forward guidance given"
        }}
        """

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
        }

        # Handle rate limits with exponential backoff
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_resp = data['candidates'][0]['content']['parts'][0]['text']
                    clean_json = raw_resp.strip()
                    if clean_json.startswith("```"):
                        clean_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean_json, flags=re.MULTILINE).strip()
                    return json.loads(clean_json)
                elif resp.status_code == 429:
                    wait_time = (attempt + 1) * 4
                    logger.warning(f"Rate limited (429). Backing off for {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Gemini API error {resp.status_code}: {resp.text}")
                    break
            except Exception as e:
                logger.error(f"Error calling Gemini API: {e}")
                time.sleep(2)

        return None

    def scan_bse_guidance(self) -> List[Dict]:
        """
        Stream 1: Polls BSE corporate announcements (LODR Investor Presentations, Concalls, Transcripts).
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

        logger.info("Scanning Stream 1: BSE corporate disclosures for management guidance...")
        high_guidance_hits = []
        current_fy, next_fy, quarter_tag = self._get_current_and_next_fy()

        try:
            resp = self.session.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                table = resp.json().get("Table", [])
                for item in table:
                    headline = str(item.get("NEWSSUB", "")).strip()
                    desc = str(item.get("MORE", "") or item.get("HEADLINE", "")).strip()
                    company_raw = str(item.get("SLONGNAME", "")).strip()
                    combined_text = f"{headline} {desc}"

                    # Pre-filter by relevant disclosure subject line first
                    if any(k in combined_text.lower() for k in ["investor presentation", "concall", "guidance", "transcript", "analyst meet", "outlook", "press release"]):
                        scrip = str(item.get("SCRIP_CD", "")).strip().upper()
                        symbol = self.equity_filter.resolve_symbol(company_raw, scrip)
                        if not symbol:
                            continue

                        # Exclude non-equities (ETFs, REITs)
                        if not self.equity_filter.is_valid_operating_stock(symbol):
                            continue

                        # Check state persistence (permaban / 30-day drop)
                        if self.state_store.is_ticker_silenced(symbol):
                            continue

                        # Deduplication check: Avoid alerting twice for same (symbol, quarter)
                        dedup_key = f"{symbol}:{quarter_tag}"
                        if dedup_key in self.history:
                            continue

                        # Paced API call (sleep 3s to respect free-tier RPM)
                        time.sleep(3)
                        gemini_res = self.analyze_guidance_with_gemini(f"{symbol} ({company_raw})", combined_text)
                        
                        if gemini_res and gemini_res.get("has_20pct_guidance"):
                            pct = gemini_res.get("guidance_pct", 20.0)
                            if pct and pct >= 20.0:
                                period = gemini_res.get("target_period", current_fy)
                                high_guidance_hits.append({
                                    "symbol": symbol,
                                    "company": company_raw or symbol,
                                    "source": "BSE Official Filing",
                                    "metric": gemini_res.get("metric", "PAT"),
                                    "guidance_pct": pct,
                                    "period": period,
                                    "summary": gemini_res.get("summary", ""),
                                    "quarter_tag": quarter_tag
                                })
                                # Mark as seen in history
                                self.history[dedup_key] = datetime.now().isoformat()
                                self._save_history()

        except Exception as e:
            logger.error(f"Error scanning BSE guidance filings: {e}")

        return high_guidance_hits

    def scan_google_news_guidance(self) -> List[Dict]:
        """
        Stream 2: Polls Google News RSS for Indian Equities forward guidance (Moneycontrol, Economic Times, Livemint, CNBC-TV18).
        Cloud-safe RSS XML: Zero Cloudflare challenges or IP blocks on GitHub Actions runners.
        """
        rss_url = (
            "https://news.google.com/rss/search?"
            "q=guidance+OR+outlook+OR+target+when:24h+inurl:moneycontrol+OR+inurl:economictimes+OR+inurl:livemint"
            "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        logger.info("Scanning Stream 2: Google News RSS for Indian equities forward guidance coverage...")
        news_hits = []
        current_fy, next_fy, quarter_tag = self._get_current_and_next_fy()

        try:
            resp = self.session.get(rss_url, timeout=15)
            if resp.status_code == 200 and "<rss" in resp.text:
                root = ET.fromstring(resp.content)
                items = root.findall(".//item")
                logger.info(f"Found {len(items)} news headlines in Google News feed.")
                
                # Check top 20 items to respect rate limits
                for item in items[:20]:
                    title = item.findtext("title", "").strip()
                    desc = item.findtext("description", "").strip()
                    clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()
                    combined_text = f"{title}. {clean_desc}"

                    # Quick pre-filter: must contain forward guidance keywords
                    if not any(k in combined_text.lower() for k in ["guidance", "target", "outlook", "growth", "pat", "revenue", "cagr", "capex", "ebitda"]):
                        continue

                    if not self.is_gemini_configured():
                        continue

                    # Paced API call (sleep 3s to respect free-tier RPM)
                    time.sleep(3)
                    gemini_res = self.analyze_guidance_with_gemini(title, combined_text)

                    if gemini_res and gemini_res.get("has_20pct_guidance"):
                        pct = gemini_res.get("guidance_pct", 20.0)
                        if pct and pct >= 20.0:
                            extracted_sym = gemini_res.get("symbol", "").upper().strip()
                            resolved_sym = self.equity_filter.resolve_symbol(extracted_sym, extracted_sym)

                            if not resolved_sym or not self.equity_filter.is_valid_operating_stock(resolved_sym):
                                continue

                            if self.state_store.is_ticker_silenced(resolved_sym):
                                continue

                            dedup_key = f"{resolved_sym}:{quarter_tag}"
                            if dedup_key in self.history:
                                continue

                            period = gemini_res.get("target_period", current_fy)
                            news_source = title.split(" - ")[-1] if " - " in title else "Financial Media"
                            news_hits.append({
                                "symbol": resolved_sym,
                                "company": title.split(" - ")[0] if " - " in title else resolved_sym,
                                "source": f"News ({news_source})",
                                "metric": gemini_res.get("metric", "PAT"),
                                "guidance_pct": pct,
                                "period": period,
                                "summary": gemini_res.get("summary", ""),
                                "quarter_tag": quarter_tag
                            })
                            self.history[dedup_key] = datetime.now().isoformat()
                            self._save_history()

        except Exception as e:
            logger.error(f"Error scanning Google News RSS for guidance: {e}")

        return news_hits

    def scan_all_guidance(self) -> List[Dict]:
        """
        Executes Dual-Stream Guidance Detection:
        1. BSE Corporate LODR Disclosures
        2. Google News RSS Feeds
        """
        all_hits = []
        bse_hits = self.scan_bse_guidance()
        all_hits.extend(bse_hits)

        news_hits = self.scan_google_news_guidance()
        for hit in news_hits:
            if not any(h['symbol'] == hit['symbol'] for h in all_hits):
                all_hits.append(hit)

        return all_hits

import json
import logging
import os
import re
import time
from typing import List, Dict, Optional
from datetime import datetime
import requests
from config.settings import BSE_HEADERS, GEMINI_API_KEY, GEMINI_MODEL
from src.storage.state_store import StateStore
from src.scanners.equity_filter import EquityFilter

logger = logging.getLogger(__name__)

class GuidanceScanner:
    """
    Scans corporate disclosures for operating equities (Market Cap >= ₹5,000 Cr).
    Uses the free Gemini 3.5 Flash Lite API (500 requests/day) with:
    1. Dynamic fiscal year detection (no hardcoded fixed FY strings).
    2. Smart text extraction to keep token payload small and prevent TPM limits.
    3. Exponential backoff and pacing on API calls to strictly avoid HTTP 429.
    """
    def __init__(self, state_store: StateStore = None, gemini_api_key: str = GEMINI_API_KEY, model: str = GEMINI_MODEL):
        self.state_store = state_store or StateStore()
        self.equity_filter = EquityFilter()
        self.api_key = gemini_api_key.strip()
        self.model = model
        self.session = requests.Session()
        self.session.headers.update(BSE_HEADERS)

    def is_gemini_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "your_gemini_api_key_here")

    def _get_current_and_next_fy(self) -> (str, str):
        """Calculates current and next Indian Fiscal Year dynamically based on current date."""
        today = datetime.now()
        # In India, Fiscal Year starts April 1 (month 4)
        if today.month >= 4:
            current_fy_end = today.year + 1
        else:
            current_fy_end = today.year
            
        current_fy = f"FY{str(current_fy_end)[-2:]}"
        next_fy = f"FY{str(current_fy_end + 1)[-2:]}"
        return current_fy, next_fy

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

    def analyze_guidance_with_gemini(self, symbol: str, raw_text: str) -> Optional[Dict]:
        """
        Calls Gemini 3.5 Flash Lite with structured schema and dynamic period detection.
        """
        if not self.is_gemini_configured():
            logger.warning("GEMINI_API_KEY not configured. Skipping LLM guidance extraction.")
            return None

        current_fy, next_fy = self._get_current_and_next_fy()
        filtered_text = self._extract_relevant_guidance_text(raw_text)

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        
        prompt = f"""
        You are a quantitative Indian equity research analyst.
        Analyze this corporate disclosure / earnings commentary for company: {symbol}.
        Target Fiscal Periods: {current_fy} or {next_fy}.
        
        Disclosure Text:
        \"\"\"{filtered_text}\"\"\"

        Determine if management explicitly provided forward guidance of:
        - 20% or higher PAT (Profit After Tax) growth, OR
        - 20% or higher Revenue/Volume growth for the current ({current_fy}) or upcoming ({next_fy}) fiscal year.

        Return strict JSON only (no markdown, no backticks):
        {{
            "has_20pct_guidance": true or false,
            "metric": "PAT" or "Revenue" or "Volume" or "None",
            "guidance_pct": float or null,
            "target_period": "{current_fy}" or "{next_fy}" or "Multi-Year",
            "summary": "1 concise sentence explaining the specific guidance given"
        }}
        """

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
        }

        # Handle rate limits with retries
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
        Polls BSE corporate announcements with pre-filtering and paced API calls.
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

        logger.info("Scanning BSE corporate updates for management guidance...")
        high_guidance_hits = []
        try:
            resp = self.session.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                table = resp.json().get("Table", [])
                for item in table:
                    headline = str(item.get("NEWSSUB", "")).strip()
                    desc = str(item.get("MORE", "") or item.get("HEADLINE", "")).strip()
                    combined_text = f"{headline} {desc}"

                    # Pre-filter by relevant disclosure subject line first
                    if any(k in combined_text.lower() for k in ["investor presentation", "concall", "guidance", "transcript", "analyst meet", "outlook", "press release"]):
                        symbol = str(item.get("SCRIP_CD", "")).strip().upper()
                        company = str(item.get("SLONGNAME", symbol)).strip()

                        # Exclude ETFs, REITs, MFs
                        if not self.equity_filter.is_valid_operating_stock(symbol):
                            continue

                        # Check state persistence
                        if self.state_store.is_ticker_silenced(symbol):
                            continue

                        # Paced API call (sleep 3s to respect free-tier RPM)
                        time.sleep(3)
                        gemini_res = self.analyze_guidance_with_gemini(symbol, combined_text)
                        
                        if gemini_res and gemini_res.get("has_20pct_guidance"):
                            pct = gemini_res.get("guidance_pct", 20.0)
                            if pct and pct >= 20.0:
                                current_fy, next_fy = self._get_current_and_next_fy()
                                period = gemini_res.get("target_period", current_fy)
                                high_guidance_hits.append({
                                    "symbol": symbol,
                                    "company": company,
                                    "metric": gemini_res.get("metric", "PAT"),
                                    "guidance_pct": pct,
                                    "period": period,
                                    "summary": gemini_res.get("summary", "")
                                })

        except Exception as e:
            logger.error(f"Error scanning corporate guidance: {e}")

        return high_guidance_hits

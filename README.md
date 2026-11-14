# 🦅 AlphaMinter: Autonomous Stock Market Discovery & Signal Suite

**Version:** 2.4.0 (Hardened Edition)  
AlphaMinter is a zero-maintenance, zero-API-cost intelligence engine designed to automatically identify high-probability swing, PEAD, and arbitrage setups in **Indian Equities (NSE/BSE)**.

It runs **100% on GitHub Actions + Cloudflare Workers + Telegram**.

---

## ⚡ Core Signal Engines

### 1. Daily Bhavcopy Delivery Accumulation Scanner (`18:45 IST`)
* Runs automatically at **6:45 PM IST (13:15 UTC, Mon–Fri)** via GitHub Actions.
* **3 Mandatory Production Guards:**
  * **Strict Operating Equity Guard:** Excludes all ETFs (`*BEES`, `*ETF`, etc.), Mutual Funds, REITs (`EMBASSY`, etc.), and InvITs. Protects genuine companies like `GOLDIAM` and `GOLDTECH`.
  * **Holiday Guard:** Validates `DATE1 == today` (exits cleanly on market holidays when NSE returns previous day's file).
  * **Turnover & Market Cap Floor:** Requires `DELIVERY_TURNOVER >= ₹5 Crore` (`DELIV_QTY × CLOSE_PRICE >= 5 Cr`), filtering out illiquid micro-caps.

### 2. PEAD (Post-Earnings Announcement Drift) Scanner (`19:00 IST`)
* Polls the cloud-safe **BSE Corporate Announcements API** (does not block GitHub Actions / Azure IPs).
* **3-Filter Trigger Rule:**
  1. **Real Earnings Acceleration:** YoY PAT $\ge 30\%$ **AND** YoY Revenue $\ge 15\%$, plus YoY OPM expansion $\ge 150\text{ bps}$.
  2. **Institutional Reaction Confirmation:** Stock closes $\ge +4\%$ on reaction session with **Volume $\ge 3\times$ 20D average** and **Delivery $\% \ge 50\%$**.
  3. **Entry Feasibility:** Not locked in upper circuit, trading above its 50-day EMA.

### 3. Retail Share Buyback Arbitrage Calculator (`19:15 IST`)
* **Strict Guard:** Evaluates strictly `TENDER_OFFER` buybacks (skips `OPEN_MARKET` buybacks which have no retail quota).
* **Safe Retail Sizing:** Calculates `floor(195,000 / CMP)` (providing a ₹5,000 safety cushion below the ₹2 Lakh SEBI Small Shareholder threshold).
* **Cutoff Alert:** Computes the final execution cutoff at **T-2 trading days** before the Record Date under T+1 settlement.

### 4. 20%+ Management Guidance Scanner (`19:30 IST`)
* Uses **Gemini 3.5 Flash Lite** (free tier 500 requests/day).
* **Dynamic Period Detection:** Dynamically calculates current and upcoming fiscal years (no hardcoded fixed FY strings).
* **Smart TPM Pre-Filtering:** Extracts only guidance-related paragraphs (max 8,000 chars), with 3-second pacing between calls and exponential backoff on HTTP 429.

### 5. Interactive Mobile Triage (Cloudflare Worker + D1)
* Responds in **$<100\text{ ms}$** on your phone.
* Commands:
  * `/watchlist` — Instant list of active watchlist stocks.
  * `/banned` — Instant list of blacklisted stocks.
  * `/unban <SYMBOL>` — Instantly removes a stock from the ban list.
* Actions: `[⭐ Watchlist]`, `[❌ Drop 30D]` (with native D1 30-day auto-expiry), `[🚫 Permaban]`.

---

## 🚀 Quick Setup

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Paste your `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `GEMINI_API_KEY`.
3. In GitHub repo **Settings $\rightarrow$ Secrets and variables $\rightarrow$ Actions**, add:
   * `TELEGRAM_BOT_TOKEN`
   * `TELEGRAM_CHAT_ID`
   * `GEMINI_API_KEY`

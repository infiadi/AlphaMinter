# 🦅 AlphaMinter: Autonomous Stock Market Discovery & Signal Suite

**Version:** 2.3.0 (Ultra-Lean Edition)  
AlphaMinter is a zero-maintenance, zero-API-cost, zero-external-database intelligence engine designed to automatically identify high-probability swing, PEAD, and arbitrage setups in **Indian Equities (NSE/BSE)**.

It runs **100% on GitHub Actions + Telegram** with no Cloudflare or external database setup required.

---

## ⚡ Core Signal Engines

### 1. Daily Bhavcopy Delivery Accumulation Scanner (`18:45 IST`)
* Runs automatically at **6:45 PM IST (13:15 UTC, Mon–Fri)** via GitHub Actions.
* **3 Mandatory Production Guards:**
  * **Series Guard:** Filters strictly for `SERIES == 'EQ'` (eliminates `BE`/`BZ` 100% mandatory delivery false alarms).
  * **Holiday Guard:** Validates `DATE1 == today` (exits cleanly on market holidays when NSE returns previous day's file).
  * **Turnover Floor:** Requires `DELIVERY_TURNOVER >= ₹5 Crore` (`DELIV_QTY × CLOSE_PRICE >= 5 Cr`), filtering out illiquid micro-caps.

### 2. PEAD (Post-Earnings Announcement Drift) Scanner (`19:00 IST`)
* Polls the cloud-safe **BSE Corporate Announcements API** (does not block GitHub Actions / Azure IPs).
* **3-Filter Trigger Rule:**
  1. **Real Earnings Acceleration:** YoY PAT $\ge 30\%$ **AND** YoY Revenue $\ge 15\%$, plus YoY OPM expansion $\ge 150\text{ bps}$.
  2. **Institutional Reaction Confirmation:** Stock closes $\ge +4\%$ on reaction session with **Volume $\ge 3\times$ 20D average** and **Delivery $\% \ge 50\%$**.
  3. **Entry Feasibility:** Not locked in upper circuit, trading above its 50-day EMA.

### 3. Retail Share Buyback Arbitrage Calculator (`19:15 IST`)
* **Safe Retail Sizing:** Calculates `floor(195,000 / CMP)` (providing a ₹5,000 safety cushion below the ₹2 Lakh SEBI Small Shareholder threshold).
* **Cutoff Alert:** Computes the final execution cutoff at **T-2 trading days** before the Record Date under T+1 settlement.
* Computes expected acceptance ratio, net profit, breakeven floor price, and annualized return.

### 4. Anti-Fatigue State Store (Ultra-Lean JSON + `getUpdates`)
* **No Database Setup:** Stores state in human-readable, Git-diffable `data/stocks_state.json`.
* **The `getUpdates` Queue Pattern:** Before each scan runs, it reads any `[⭐ Watchlist]`, `[❌ Drop 30D]`, or `[🚫 Permaban]` buttons clicked on your phone over the last 24 hours and updates `stocks_state.json`.
* **Zero Git Conflicts:** Workflows use `concurrency: group: alphaminter-state-lock` to guarantee sequential updates.

---

## 🚀 Quick Setup (3 Minutes)

### Step 1: Create Your Free Telegram Bot
1. Open Telegram on your phone and search for **`@BotFather`**.
2. Send `/newbot`, choose a name (e.g. `AlphaMinter Sentinel`), and pick a username ending in `bot`.
3. Copy the **HTTP API Token** provided by BotFather.
4. Search for **`@userinfobot`** on Telegram, send `/start`, and copy your numeric **Id** (Chat ID).

### Step 2: Local Configuration
1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Paste your credentials:
   ```ini
   TELEGRAM_BOT_TOKEN=7123456789:AAFxxx...
   TELEGRAM_CHAT_ID=987654321
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Test the pipeline locally:
   ```bash
   python run_eod_pipeline.py
   ```

### Step 3: Deploy to GitHub Actions (Runs on Autopilot)
1. Push this folder to your GitHub repository (e.g. `AlphaMinter`).
2. Go to **Settings $\rightarrow$ Secrets and variables $\rightarrow$ Actions**.
3. Add only two repository secrets:
   * `TELEGRAM_BOT_TOKEN`
   * `TELEGRAM_CHAT_ID`
4. Go to the **Actions** tab $\rightarrow$ Click **"1. Bhavcopy Delivery Accumulation Scanner"** $\rightarrow$ Click **"Run workflow"** to trigger a test run immediately.

---

## 📱 Interactive Telegram Triage Console

Every evening, you receive clean, actionable alert cards on Android:

```text
🚨 SWING ACCUMULATION: HBLPOWER
Ticker: HBLPOWER (HBL Power Systems)
Key Signals:
• Delivery %: 78.4%
• CMP: ₹542.50 (Day Change: +1.2%)
• Delivery Turnover: ₹18.5 Cr (340,000 shares)

Setup Note:
High delivery volume in tight price range indicates institutional accumulation before swing breakout.

[📈 TradingView]   [📊 Screener]
[⭐ Watchlist]   [❌ Drop 30D]   [🚫 Permaban]
```

* Tap **`[❌ Drop 30D]`** directly on your phone $\rightarrow$ Telegram queues the tap $\rightarrow$ Next GitHub Action drains it and silences the ticker for 30 days!

---

## 📂 Project Structure

```
AlphaMinter/
├── .github/
│   └── workflows/
│       ├── bhavcopy_delivery_scanner.yml  # 18:45 IST Mon-Fri (EQ only, >=₹5 Cr turnover)
│       ├── pead_earnings_drift.yml        # 19:00 IST Mon-Sat (BSE Cloud-Safe API + 3-Filter rule)
│       ├── buyback_arbitrage.yml          # 19:15 IST Mon-Fri (floor ₹1.95L sizing & T-2 cutoff)
│       └── state_heartbeat.yml            # Weekly Sunday keepalive ping
├── config/
│   └── settings.py                        # Hardened thresholds & headers
├── data/
│   └── stocks_state.json                  # Git-tracked state (Watchlist, Dropped, Permabanned)
├── src/
│   ├── arbitrage/
│   │   └── buyback_arb.py                 # Retail buyback quota & ROI calculator (floor ₹1.95L)
│   ├── notifications/
│   │   └── telegram_dispatcher.py         # Formatted Telegram alert cards + 1-tap buttons
│   ├── scanners/
│   │   ├── bhavcopy_scanner.py            # NSE Bhavcopy Delivery % accumulator (3 Guards)
│   │   └── pead_scanner.py                # Cloud-safe BSE PEAD 3-filter engine
│   └── storage/
│       └── state_store.py                 # Lean JSON state store + getUpdates queue drainer
├── run_eod_pipeline.py                    # Main EOD pipeline runner
├── requirements.txt                       # Lightweight dependencies
└── .env.example                           # Environment variables template
```

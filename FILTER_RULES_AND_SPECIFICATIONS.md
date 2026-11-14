# AlphaMinter: Quantitative Filter Rules, Formulas & System Specifications (v2.5.0)

This document serves as the complete, authoritative quantitative manual and operational rulebook for the AlphaMinter autonomous equity engine. It outlines every mathematical formula, boundary check, market-microstructure justification, and tuning knob across all four scanner engines and the Cloudflare edge persistence layer.

---

## 1. Scanner 1: Daily Bhavcopy Delivery Accumulation Scanner
* **Execution Cadence:** Every Monday through Friday at **18:45 IST** (after NSE finishes uploading `sec_bhavdata_full_DDMMYYYY.csv`).
* **Underlying Edge:** Wyckoff Phase B/C silent institutional accumulation. When institutions build positions of ₹10–₹100 Cr in a liquid stock, they avoid moving the price drastically. They absorb all floating supply at market bids, resulting in tight daily price ranges, suppressed volatility, massive delivery turnover, and closes in the upper half of the candle.

### Quantitative Formula Matrix

| Rule ID | Metric | Quantitative Hurdle | Market Microstructure Rationale |
| :--- | :--- | :--- | :--- |
| **BAC-01** | **Asset Classification** | `SERIES == 'EQ'` AND In `EQUITY_L.csv` | **Zero-Noise Equities:** NSE tags ETFs (`GOLDIETF`, `NIFTYBEES`), REITs (`EMBASSY`, `BIRET`), and InvITs under series `EQ`. By cross-referencing against NSE's official `EQUITY_L.csv` and regex blocking `(BEES\|IETF\|ETF\|REIT\|INVIT)$`, we eliminate non-operating asset noise. |
| **BAC-02** | **Delivery Turnover Floor** | $\text{Deliv Turnover} = \frac{\text{DELIV\_QTY} \times \text{CLOSE}}{10,000,000} \ge ₹5.0\text{ Cr}$ | **Institutional Scale:** DIIs, FIIs, and mutual funds deploy tens of crores. A stock trading ₹20 Lakhs of delivery is prone to operator pump-and-dump manipulation. A ₹5 Cr hard floor ensures true institutional footprint. |
| **BAC-03** | **Absolute Delivery %** | $\text{Deliv \%} = \frac{\text{DELIV\_QTY}}{\text{TTL\_TRD\_QNTY}} \times 100 \ge 65.0\%$ | **Float Lockup:** Day-trading scalpers and algorithmic intraday churn are marked to market and squared off before 3:30 PM. A delivery ratio $\ge 65\%$ proves permanent overnight transfer of certificates into Demat accounts. |
| **BAC-04** | **Asymmetric Price Drift** | $-1.0\% \le \frac{\text{CLOSE} - \text{PREV\_CLOSE}}{\text{PREV\_CLOSE}} \times 100 \le +4.0\%$ | **Accumulation vs. Distribution:** A symmetric $\pm 4\%$ band is flawed because a stock dropping $-3.8\%$ on heavy volume is **distribution** (institutions dumping into bids). The asymmetric $[-1.0\%, +4.0\%]$ band isolates accumulation and stealth upward drift. |
| **BAC-05** | **Close Location Value (CLV)** | $\text{CLV} = \frac{\text{CLOSE} - \text{LOW}}{\text{HIGH} - \text{LOW}} \ge 0.50$ | **Late-Day Absorption:** $\text{CLV} \ge 0.50$ verifies that the close occurred in the upper half of the day's total range. If a stock opens high, drops, and closes near the low ($\text{CLV} < 0.50$), supply overwhelmed buyers. A high CLV proves buyers absorbed selling into the 3:30 PM closing auction. |
| **BAC-06** | **Session Staleness Guard** | `DATE1 == Today` | **Holiday & Stale Data Guard:** On exchange holidays, NSE archives often retain the previous trading day's CSV. Comparing the row's `DATE1` against current date prevents firing stale duplicate signals. |

### Tuning Knobs (Adjusting Quality & Volume)
* **Conservative / Large-Cap Bias (Nifty 100 Focus):**
  * Increase `MIN_DELIVERY_TURNOVER_CR` from `5.0` to `15.0` (₹15 Cr delivery floor).
  * Increase `MIN_DELIVERY_PCT` from `65.0` to `70.0%`.
  * Tighten `MAX_DAILY_PRICE_CHANGE_PCT` to `+3.0%`.
* **High-Beta Mid & Small-Cap Discovery:**
  * Keep `MIN_DELIVERY_TURNOVER_CR` at `5.0`.
  * Allow price drift up to `+5.0%`.
  * Require `CLV >= 0.60` (strong close in top 40% of range).

---

## 2. Scanner 2: Post-Earnings Announcement Drift (PEAD) Engine
* **Execution Cadence:** Every Monday through Saturday at **19:00 IST** (capturing full results filings through 18:30 IST).
* **Underlying Edge:** Post-Earnings Announcement Drift (Ball & Brown, 1968; Bernard & Thomas, 1989). Large institutions cannot execute their entire target allocation in a single session without spiking the price into an upper circuit. As a result, stocks with blowout surprises experience continuous net buying pressure for 15 to 60 trading days.

### The 3-Filter Institutional Hurdle

```
[ Quarterly Filing ] ──► [ Filter 1: Fundamental Acceleration ] ──► [ Filter 2: Market Confirmation ] ──► [ Filter 3: Feasibility ] ──► ALERT
```

#### Filter 1: Fundamental Acceleration (The Fundamental Blowout)
1. **Net Profit (PAT) YoY Growth:**
   $$\text{PAT YoY \%} = \frac{\text{PAT}_{\text{Current Qtr}} - \text{PAT}_{\text{Same Qtr Last Year}}}{|\text{PAT}_{\text{Same Qtr Last Year}}|} \times 100 \ge +30.0\%$$
   * **Turnaround Mathematical Guard:** If $\text{PAT}_{\text{Same Qtr Last Year}} \le 0$ and $\text{PAT}_{\text{Current Qtr}} > 0$ (e.g. turnaround from loss of ₹-50 Cr to profit of +₹20 Cr), the standard formula produces a negative or division-by-zero error. The system detects this as an **Inflection Turnaround** and qualifies it with top priority.
2. **Topline Revenue YoY Growth:**
   $$\text{Revenue YoY \%} \ge +15.0\%$$
   *(Prevents cost-cutting illusions or one-time deferred tax asset gains from masquerading as operational growth).*
3. **Operating Profit Margin (OPM / EBITDA Margin) Expansion:**
   $$\Delta \text{OPM} = \text{OPM}_{\text{Current Qtr}} - \text{OPM}_{\text{Same Qtr Last Year}} \ge +150\text{ bps} \ (+1.5\%)$$
   *(Confirms pricing power, raw material deflation benefits, or operational leverage).*

#### Filter 2: Institutional Price & Volume Confirmation
1. **Reaction Day Price Change:** $\ge +4.0\%$ on the post-announcement trading session.
2. **The Negative Divergence Exclusion Rule:** If a company reports $+80\%$ PAT but the stock closes flat or negative ($-1.0\%$ to $-5.0\%$), the alert is **instantly rejected**. Sophisticated institutions trade on hidden cash-flow deteriorations, pledge increases, or unbilled revenue that headline numbers obscure.
3. **Volume Surge:** $\text{Reaction Session Volume} \ge 3.0 \times \text{Volume}_{20D\text{ Average}}$.
4. **Delivery Quality:** $\text{Reaction Delivery \%} \ge 50.0\%$.

#### Filter 3: Entry Feasibility & Circuit Mechanics
1. **Upper Circuit Differentiation:**
   * If a stock is locked at a single price ($\text{HIGH} == \text{LOW}$) in an Upper Circuit with zero sellers, it cannot be bought today. The system flags it as `UPPER_CIRCUIT_LOCKED` for monitoring on the first liquidity pullback, rather than discarding it.
2. **Structural Trend Alignment:**
   * Price must be trading above its rising 50-day and 200-day Simple/Exponential Moving Averages (avoiding dead-cat bounces in structurally broken downtrends).

---

## 3. Scanner 3: Retail Share Buyback Arbitrage Monitor
* **Execution Cadence:** Every Monday through Friday at **19:15 IST**.
* **Underlying Edge:** Regulatory structural arbitrage created by Section 68 of the Companies Act, 2013, and SEBI (Buy-Back of Securities) Regulations. SEBI mandates a **strict 15% reservation** of the total buyback size exclusively for "Small Shareholders" (defined as holding equity shares whose market value is $\le ₹2,00,000$ on the Record Date).

### Mathematical Formulas & Boundary Safeguards

#### 1. Category Filter (Non-Negotiable Guard)
* **Strictly `TENDER_OFFER` Only.**
* **Open-Market Buybacks are Auto-Rejected:** In August 2026, SEBI reinstated open-market buybacks through stock exchanges. However, open-market buybacks have **no small shareholder reservation** and **no fixed tender price**. Arbitrage is mathematically impossible under open market buybacks.

#### 2. Safe Retail Sizing Formula (The Pre-Record Date Rally Buffer)
$$\text{Max Safe Retail Shares} = \left\lfloor \frac{185,000}{\text{CMP}} \right\rfloor$$
* **Why ₹1,85,000 instead of ₹1,95,000 or ₹2,00,000?**
  * SEBI measures the ₹2,00,000 ceiling on the **closing price of the Record Date**, NOT on the purchase date.
  * If you invest ₹1,95,000 and the stock rallies $+3.0\%$ between announcement and Record Date, your position value closes at $₹2,00,850$.
  * **Catastrophic Failure Mode:** Breaching ₹2,00,000 reclassifies your account into the "General Category" (alongside LIC, FIIs, and promoters). In the General category, the acceptance ratio collapses from $50\%–80\%$ down to $2\%–5\%$, completely destroying the trade.
  * Sizing at ₹1,85,000 provides a **~8.1% pre-record-date rally buffer**, guaranteeing you remain in the protected Small Shareholder category.

#### 3. 3-Tier Acceptance Matrix with 20% STCG Tax Drag
Under Indian income tax provisions (post-Finance Act capital gains regime), proceeds from tender buybacks are taxed as Short-Term Capital Gains (**STCG @ 20%** under Section 111A).

For any scenario acceptance ratio $A \in \{30\%, 50\%, 75\%\}$:
* $\text{Accepted Shares} = \lfloor \text{Max Safe Shares} \times A \rfloor$
* $\text{Unaccepted Shares} = \text{Max Safe Shares} - \text{Accepted Shares}$
* $\text{Gross Profit} = \text{Accepted Shares} \times (\text{Buyback Price} - \text{CMP})$
* $\text{Net Profit (Post-Tax)} = \text{Gross Profit} \times (1 - 0.20)$
* $\text{Net Return on Capital \%} = \frac{\text{Net Profit}}{\text{Total Capital Deployed}} \times 100$
* $\text{Annualized Net ROI \%} = \text{Net Return \%} \times \left( \frac{365}{\text{Days to Payout}} \right)$

#### 4. Breakeven Downside Floor (Capital Preservation)
Calculates the minimum price at which unaccepted shares can be dumped in the open market without experiencing a net portfolio loss:
$$P_{\text{breakeven}} = \frac{\text{Total Investment} - (\text{Accepted Shares} \times \text{Buyback Price})}{\text{Unaccepted Shares}}$$
$$\text{Allowed Drop Cushion \%} = \frac{\text{CMP} - P_{\text{breakeven}}}{\text{CMP}} \times 100$$
*(If $100\%$ of shares are accepted, $\text{Unaccepted Shares} = 0 \implies P_{\text{breakeven}} = ₹0.00$, Cushion $= 100\%$).*

#### 5. Hurdle Checklist for Viability
* Spread: $\frac{\text{Buyback Price} - \text{CMP}}{\text{CMP}} \times 100 \ge 12.0\%$.
* Base-case (50% acceptance) Net Return: $\ge 5.0\%$ net of tax in $<60$ days.
* Cushion: Unaccepted shares can drop $\ge 8.0\%$ before breakeven is breached.
* Purchase Window: Executed before **T-2 trading days prior to Record Date** (under T+1 settlement, shares must settle in Demat before the Record Date cutoff).

---

## 4. Scanner 4: 20%+ Forward Management Guidance Scanner
* **Execution Cadence:** Every Monday through Friday at **19:30 IST**.
* **Underlying Edge:** Forward guidance revisions drive institutional earnings upgrades. When an executive team explicitly guides for $\ge 20\%$ profit or revenue growth, consensus earnings estimates increase, forcing institutional sell-side analysts to re-rate the stock's valuation multiple.

### Operational Specification

#### 1. Scope & Universe:
* Operating equities only ($\text{Market Cap} \ge ₹5,000\text{ Crore}$).
* Whitelisted Disclosures: Scans BSE corporate announcements filtering by subject tags: `Investor Presentation`, `Concall Transcript`, `Earnings Conference Call`, `Guidance`, `Outlook`, `Press Release`.

#### 2. Dynamic Calendar Logic (Zero Hardcoded FY Strings):
* Indian Fiscal Years run from April 1 to March 31.
* The system computes:
  * Current Fiscal Year: $\text{FY} = \text{today.year} + 1$ (if month $\ge 4$) else $\text{today.year}$.
  * Upcoming Fiscal Year: $\text{FY} + 1$.
  * Current Quarter: Month 4–6 = `Q1`, 7–9 = `Q2`, 10–12 = `Q3`, 1–3 = `Q4`.
  * Quarter Tag: e.g. `Q3FY27`.

#### 3. Quarter-Level Deduplication:
* During earnings season, a company may release an Earnings Press Release on Tuesday, an Investor Presentation on Wednesday, and a Concall Transcript on Thursday.
* The system generates a persistent deduplication key:
  $$\text{Key} = \text{SYMBOL} : \text{QuarterTag} \quad (\text{e.g., } \texttt{TRENT:Q3FY27})$$
* Once an alert fires for that symbol in that quarter, subsequent duplicate disclosures for the same quarter are suppressed.

#### 4. Strict LLM Prompt Guardrails (Gemini 3.5 Flash Lite):
* **Model ID:** `gemini-3.5-flash-lite` (Free-tier, 500 requests/day, sub-second latency, zero billing account requirement).
* **Token Slicing:** Extracts paragraphs containing guidance keywords (`guidance`, `outlook`, `target`, `PAT`, `EBITDA`, `capex`, `CAGR`) capped at 8,000 characters to keep TPM usage $<5\%$.
* **Rate-Limit Pacing:** 3.0-second delay between calls + exponential backoff on HTTP 429.
* **Strict Response Format:** `responseMimeType: "application/json"`.
* **The Vision vs. Guidance Negation Rule:**
  * **REJECT:** Past-tense claims (*"We grew revenue by 30% in the previous quarter"*).
  * **REJECT:** Unanchored long-term visions (*"Our vision is to become a ₹20,000 Cr company by 2035"*).
  * **ACCEPT ONLY:** Definite near-term forward guidance (*"Management expects FY27 revenue to grow between 22%–25% with 200 bps EBITDA margin improvement"*).

---

## 5. Cloudflare Worker & D1 Edge Persistence Specifications
* **Infrastructure:** Cloudflare Worker (`alphaminter-bot`) + Cloudflare D1 Serverless SQL (`alphaminter-db`).
* **Latency:** $<100\text{ ms}$ response time globally (served from Cloudflare edge locations across India: Mumbai, Delhi, Chennai, Bangalore, Hyderabad).

### Database Schema (`stocks_state`)

```sql
CREATE TABLE IF NOT EXISTS stocks_state (
    symbol TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('WATCHLIST', 'DROPPED_TEMP', 'PERMABAN')),
    cooldown_until DATE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Mobile Telegram Actions & Commands

| User Action | Telegram Payload / Input | Cloudflare Edge Execution | Edge Latency |
| :--- | :--- | :--- | :--- |
| **⭐ Watchlist** | Callback: `WATCH:<SYMBOL>` | Sets `status='WATCHLIST'`, `cooldown_until=NULL`. Stock prioritizes in tracking. | $<80\text{ ms}$ |
| **❌ Drop 30D** | Callback: `DROP:<SYMBOL>` | Sets `status='DROPPED_TEMP'`, `cooldown_until=today + 30 days`. Suppresses alerts. | $<80\text{ ms}$ |
| **🚫 Permaban** | Callback: `BAN:<SYMBOL>` | Sets `status='PERMABAN'`, `cooldown_until=NULL`. Never alerts again. | $<80\text{ ms}$ |
| **/unban** | Command: `/unban <SYMBOL>` | Deletes record from `stocks_state`. Re-activates ticker for scanning immediately. | $<95\text{ ms}$ |
| **/watchlist** | Command: `/watchlist` | Returns formatted list of all active watchlist tickers. | $<95\text{ ms}$ |
| **/banned** | Command: `/banned` | Returns list of all permabanned tickers with copyable `/unban` shortcut. | $<95\text{ ms}$ |

### Security Model
1. **Chat ID Whitelist:** Every incoming webhook evaluates `chat.id == env.TELEGRAM_CHAT_ID`. Strangers or unauthorized bots receive HTTP 403 Forbidden.
2. **Internal API Protection:** `/api/silenced-list` requires `X-Internal-Secret` matching `env.INTERNAL_SECRET`.
3. **Zero Token Leaks:** All API keys and tokens reside in GitHub Secrets and Cloudflare Worker Environment Variables.

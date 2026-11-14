---
name: run-screener-pipeline
description: End-to-end AlphaMinter local screener and sequential 1-stock-at-a-time AI evaluation pipeline. Runs zero-quota Python preparation, loops through surviving candidates with the equity-analyst subagent, and pushes strong picks to Telegram with 1-tap triage buttons.
---

# AlphaMinter Screener & 1-Stock-at-a-Time AI Evaluation Pipeline

> [!IMPORTANT]
> **STRICT OPERATIONAL DIRECTIVES (DO NOT DEVIATE):**
> 1. **DO NOT read codebase files, browse directories, or explore repository structure.**
> 2. **DO NOT run unit tests, test suites, or pytest.** This is an operational execution task, NOT a software development or debugging task.
> 3. **IMMEDIATELY execute Step 1** as your first tool call without any preliminary steps.

---

## Step 1: Execute Pure Python Data Prep & Quant Gate (0 Quota Burn)
Immediately run the Stage 1 batch preparation script via `run_command`:
```bash
python src/local/prepare_screener_batch.py
```
This script:
- Connects to Screener.in using `SCREENER_SESSION_ID` (auto-renewing cookie via dummy account `SCREENER_EMAIL` + `SCREENER_PASSWORD` if expired).
- Crawls all configured screens in `config/screens.json` with 2.0s pacing.
- Merges multi-screen overlap and confluence.
- Excludes non-equities (`EquityFilter`), silenced tickers (`StateStore` via Cloudflare D1), and stocks on 45-day cooldown (`data/reviewed_history.json`).
- Fetches full Screener tables for surviving candidates.
- Runs the 7 negative parameter checks (`NEG_ROCE`, `NEG_CASH_CONVERSION`, `NEG_HIGH_DEBT`, `NEG_PROMOTER_PLEDGE`, `NEG_LATEST_QTR_PAT`, `NEG_LATEST_QTR_SALES`, `NEG_OPM_CONTRACTION`).
- Drops candidates with more than 1 negative flag (logged to `data/prepared_queues/rejected_*.json`).
- Writes sorted surviving candidates to `data/prepared_queues/queue_YYYY-MM-DD_HHMMSS.json`.

---

## Step 2: Read Latest Prepared Queue
Locate and parse the most recent timestamped queue:
Read the file path printed by Step 1 (`data/prepared_queues/queue_YYYY-MM-DD_HHMMSS.json`) using `read_file`.

Inspect `candidates`:
- If `len(candidates) == 0`:
  State that all stocks were either filtered by quantitative gates or are currently on 45-day cooldown. Conclude gracefully without burning subagent turns.
- If `len(candidates) > 0`: Proceed directly to Step 3.

---

## Step 3: Parallel Subagent Batches (3 to 5 at a time)
Process surviving candidate stocks in parallel batches of **3 to 5 concurrent subagents**:

1. Divide `candidates` into slices of 3 to 5 stocks (e.g. `batch = candidates[i:i+4]`).
2. For each stock in the current batch, call `invoke_subagent` concurrently:
   - `TypeName`: `"equity-analyst"`
   - `Role`: `"Equity Analyst - <SYMBOL>"`
   - `Prompt`: Provide that stock's complete JSON dossier and specific due diligence directives:
     - **Sector & Industry Analysis**: Analyze sectoral tailwinds/headwinds, competitive intensity, capex cycles, and structural drivers.
     - **Sector-Specific Metrics**: For Financials (Banks/NBFCs/AMCs), evaluate ROE, NIM, asset quality (GNPA/NNPA), and credit growth rather than manufacturing ROCE or CFO/PAT.
     - **Multi-Year Perspective**: Recognize that a single transient or cyclical negative quarter does not break a multi-year compounder (check 3Y/5Y Sales and Profit CAGR).
     - **Targeted Web Searches**: Perform 1–2 web searches on recent concall FY27/FY28 management guidance, expansion plans, and any forensic red flags / SEBI / tax scrutiny.
3. Wait for all subagents in the batch to report back.
4. Record their verdicts into `logs/screener_audit_YYYY-MM-DD.md`, `logs/screener_audit_YYYY-MM-DD.jsonl`, and checkpoint `data/reviewed_history.json`.
5. Proceed to the next batch until all candidates are processed.

---

## Step 4: Dispatch Digest & Telegram Feedback Loop
Once all candidates have been audited:
Run the Stage 3 notification dispatcher via `run_command`:
```bash
python src/local/push_screener_digest.py
```
This script:
1. Reads today's `STRONG_BUY_WATCH` and `ACCUMULATE_ON_DIP` decisions from `logs/screener_audit_YYYY-MM-DD.jsonl`.
2. Pushes formatted Telegram cards with key metrics, moat thesis, FY27/FY28 guidance summary, and 1-tap triage buttons:
   - `[📈 TradingView]`
   - `[📊 Screener]`
   - `[⭐ Watchlist]`
   - `[❌ Drop 30D]`
   - `[🚫 Permaban]`
3. Sends a final audit summary batch card.

---

## Step 5: Conclude
Print a concise summary table of evaluated stocks, verdicts, and dispatched Telegram alerts to the user.

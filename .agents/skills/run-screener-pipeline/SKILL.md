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

## Step 3: Sequential 1-Stock-at-a-Time Subagent Loop
For each candidate stock in `candidates` (strictly **one at a time**, sequentially, never in parallel):

1. Call `invoke_subagent` with:
   - `TypeName`: `"equity-analyst"`
   - `Role`: `"Equity Analyst - <SYMBOL>"`
   - `Prompt`: Provide ONLY that single stock's JSON object:
     ```json
     {
       "symbol": candidate["symbol"],
       "name": candidate["name"],
       "screen_count": candidate["screen_count"],
       "matched_screens": candidate["matched_screens"],
       "rubrics": candidate["rubrics"],
       "negative_flags": candidate["negative_flags"],
       "negative_flag_reasons": candidate.get("negative_flag_reasons", {}),
       "screen_metrics": candidate.get("screen_metrics", {}),
       "dossier": candidate["dossier"]
     }
     ```
2. The `@equity-analyst` subagent will:
   - Evaluate the pre-parsed financials against matched screen rubrics.
   - Perform 1–2 targeted web searches for FY27/FY28 concall guidance and red flags.
   - Assign a standardized verdict (`STRONG_BUY_WATCH`, `ACCUMULATE_ON_DIP`, `HOLD_WATCH`, `REJECT`).
   - Return structured findings.
3. Wait for the subagent to report back.
4. Record its verdict into `logs/screener_audit_YYYY-MM-DD.md`, `logs/screener_audit_YYYY-MM-DD.jsonl`, and checkpoint `data/reviewed_history.json`.
5. Repeat for the next candidate.

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

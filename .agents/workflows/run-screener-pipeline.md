---
name: run-screener-pipeline
description: End-to-end AlphaMinter local screener and sequential 1-stock-at-a-time AI evaluation pipeline. Runs zero-quota Python preparation, loops through surviving candidates with the equity-analyst subagent, and pushes strong picks to Telegram with 1-tap triage buttons.
---

# AlphaMinter Screener & 1-Stock-at-a-Time AI Evaluation Pipeline

Execute the autonomous, end-to-end stock screening and qualitative evaluation workflow:

## Step 1: Execute Pure Python Data Prep & Quant Gate (0 Quota Burn)
Run the Stage 1 batch preparation script via terminal:
```bash
python src/local/prepare_screener_batch.py
```
This script:
1. Connects to Screener.in using `SCREENER_SESSION_ID` (auto-renewing cookie via dummy account `SCREENER_EMAIL` + `SCREENER_PASSWORD` if expired).
2. Crawls all configured screens in `config/screens.json` with 2.0s pacing.
3. Merges multi-screen overlap and confluence.
4. Excludes non-equities (`EquityFilter`), silenced tickers (`StateStore` via Cloudflare D1), and stocks reviewed within 45 days (`data/reviewed_history.json`).
5. Fetches full Screener tables for surviving candidates.
6. Runs the 7 negative parameter checks (`NEG_ROCE`, `NEG_CASH_CONVERSION`, `NEG_HIGH_DEBT`, `NEG_PROMOTER_PLEDGE`, `NEG_LATEST_QTR_PAT`, `NEG_LATEST_QTR_SALES`, `NEG_OPM_CONTRACTION`).
7. Drops candidates with more than 1 negative flag (logged to `data/prepared_queues/rejected_*.json`).
8. Writes sorted surviving candidates to `data/prepared_queues/queue_YYYY-MM-DD_HHMMSS.json`.

---

## Step 2: Read Latest Prepared Queue
Locate and parse the most recent timestamped queue:
```python
import json
from glob import glob
from pathlib import Path

queue_files = sorted(glob("data/prepared_queues/queue_*.json"))
if not queue_files:
    print("No prepared queues found.")
    exit(0)

latest_file = queue_files[-1]
with open(latest_file, "r", encoding="utf-8") as f:
    batch = json.load(f)

candidates = batch.get("candidates", [])
print(f"Loaded {len(candidates)} candidates from {latest_file}")
```

If `len(candidates) == 0`:
- Report that all screen hits were either filtered by quantitative gates or are currently on cooldown.
- Exit gracefully without burning subagent turns.

---

## Step 3: Sequential 1-Stock-at-a-Time Subagent Loop
For each candidate stock in `candidates` (strictly one at a time, never in parallel):

1. Invoke the `@equity-analyst` subagent (`model: flash`, isolated context) with ONLY this single stock's JSON payload:
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
2. The `@equity-analyst` subagent:
   - Evaluates the pre-parsed financials against matched screen rubrics.
   - Performs 1–2 targeted web searches for FY27/FY28 concall guidance and red flags.
   - Assigns a verdict (`STRONG_BUY_WATCH`, `ACCUMULATE_ON_DIP`, `HOLD_WATCH`, `REJECT`).
   - Logs decision to `logs/screener_audit_YYYY-MM-DD.md` and `logs/screener_audit_YYYY-MM-DD.jsonl`.
   - Checkpoints `data/reviewed_history.json`.
3. Wait for the subagent to complete and exit before moving to the next candidate.

---

## Step 4: Dispatch Digest & Telegram Feedback Loop
Once all candidates have been audited:
Run the Stage 3 notification dispatcher:
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

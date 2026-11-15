"""
AlphaMinter Autonomous Screener Pipeline Workflow
Orchestrates end-to-end data preparation, parallel batched subagent evaluation,
deterministic audit persistence, and Telegram dispatch.
"""

import os
import sys
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# Set up project paths
BASE_DIR = Path.cwd()
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
QUEUES_DIR = DATA_DIR / "prepared_queues"
HISTORY_PATH = DATA_DIR / "reviewed_history.json"

LOGS_DIR.mkdir(parents=True, exist_ok=True)
QUEUES_DIR.mkdir(parents=True, exist_ok=True)

# Compatibility fallbacks for workflow primitives if tested outside runtime
if "log" not in globals():
    def log(*args):
        print("[WORKFLOW LOG]", *args)
if "phase" not in globals():
    def phase(name):
        print(f"\n{'='*20} {name} {'='*20}")

CHUNK_SIZE = 3


def extract_growth(growth_dict: Dict[str, Any], period: str) -> Optional[float]:
    """Extracts numeric growth percentage from growth dict (e.g. {'3 Years': '22%'} -> 22.0)."""
    val = growth_dict.get(period)
    if not val:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(val))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def build_analyst_prompt(candidate: Dict[str, Any]) -> str:
    """Assembles the full context prompt for @equity-analyst."""
    sym = candidate.get("symbol", "")
    name = candidate.get("name", "")
    dossier = candidate.get("dossier", {})
    metrics = dossier.get("derived_metrics", {})
    top_ratios = dossier.get("top_ratios", {})
    growth = dossier.get("compounded_growth", {})
    screens = candidate.get("matched_screens", [])
    rubrics = candidate.get("rubrics", [])
    flags = candidate.get("negative_flags", [])
    forensic_flags = candidate.get("forensic_flags", [])
    sm = candidate.get("screen_metrics", {})
    tech = candidate.get("technicals", {})
    review_reason = candidate.get("review_reason", "")
    needs_review = candidate.get("needs_agent_review", False)

    down_52w = sm.get("Down%") or sm.get("Down %") or "N/A"
    down_ath = sm.get("Down ATH%") or sm.get("Down ATH %") or "N/A"

    prompt = f"""You are analyzing candidate stock {sym} ({name}).
Evaluate this single stock using its pre-prepared quantitative dossier below.
Follow the STRICT BEAR-FIRST EVALUATION PROTOCOL.

### CANDIDATE DOSSIER:
- Symbol: {sym} | Company: {name}
- Matched Screens: {', '.join(screens)} (Hits: {candidate.get('screen_count', 1)})
- Rubrics: {' '.join(rubrics)}
- Market Cap: ₹{metrics.get('market_cap_cr', top_ratios.get('Market Cap', 'N/A'))} Cr | CMP: ₹{metrics.get('current_price', top_ratios.get('Current Price', 'N/A'))}
- Drawdown: Down from 52W High: {down_52w}% | Down from ATH: {down_ath}%
- Valuation & Quality: Stock P/E: {metrics.get('pe', top_ratios.get('Stock P/E', 'N/A'))}x | ROCE: {metrics.get('roce_pct', top_ratios.get('ROCE', 'N/A'))}% | ROE: {metrics.get('roe_pct', top_ratios.get('ROE', 'N/A'))}%
- Cash Conversion: 3Y CFO / PAT: {metrics.get('cfo_to_pat_3y', 'N/A')}x | 3Y CFO / EBITDA: {metrics.get('cfo_to_ebitda_3y', 'N/A')}x
- Working Capital: Debtor Days: {metrics.get('debtor_days_latest', 'N/A')} (Delta vs 3Y Avg: {metrics.get('debtor_days_delta', 'N/A')}d)
- Capex & Leverage: CWIP / Net Block: {metrics.get('cwip_ratio_latest', 'N/A')}x | Debt to Equity: {metrics.get('debt_to_equity', 'N/A')}
- Non-Core Income: Other Income / PBT: {metrics.get('other_income_to_pbt_ttm', 'N/A')}
- Quarterly Sales Expansion: {metrics.get('latest_qtr_yoy_sales_pct', 'N/A')}% YoY (Quarterly Ratio: {metrics.get('sales_qtr_ratio', 'N/A')})
- Promoter: Pledge: {metrics.get('promoter_pledge_pct', 0.0)}% | QoQ Change: {metrics.get('promoter_qoq_change_pct', 'N/A')}%
- Compounded Sales Growth: {json.dumps(growth.get('Compounded Sales Growth', {}))}
- Compounded Profit Growth: {json.dumps(growth.get('Compounded Profit Growth', {}))}
- Technofundamental Profile: {tech.get('technical_summary', 'N/A')}
- Flags: Standard Flags={flags or 'None'} | Forensic Flags={forensic_flags or 'None'}
- Review Context: {review_reason if needs_review else 'Standard quant qualification'}

### STRICT BEAR-FIRST EVALUATION PROTOCOL:
1. Conduct Bear-First Critique (multiple derating, balance sheet/working capital, cyclical vs moat decay).
2. Perform targeted web search for recent concall highlights, guidance, and analyst tension.
3. Test the >= 20% 3Y CAGR hurdle across alpha pathways: PE_RERATING, EARNINGS_COMPOUNDER, CAPEX_INFLECTION, CYCLICAL_MARKET_PROXY.
4. Conclude with verdict: STRONG_BUY_WATCH, ACCUMULATE_ON_DIP, HOLD_WATCH, or REJECT.

CRITICAL: Return ONLY a valid, compact JSON object matching this schema. No introductory text, no conversational filler, no markdown report text.
```json
{{
  "ticker": "{sym}",
  "verdict": "STRONG_BUY_WATCH|ACCUMULATE_ON_DIP|HOLD_WATCH|REJECT",
  "conviction_score": 8,
  "pathway_id": "PE_RERATING|EARNINGS_COMPOUNDER|CAPEX_INFLECTION|CYCLICAL_MARKET_PROXY",
  "expected_cagr_3y": "20-25%",
  "cagr_breakdown": "Explicit return math: EPS growth % + P/E delta %",
  "margin_of_safety": "High|Moderate|Low with brief justification",
  "thesis": "Concise 1-2 sentence core investment thesis",
  "guidance_audit": "Summary of management guidance, concall analyst tension, and capex roadmap",
  "bear_risks": "Summary of forensic risks, multiple derating risks, and cyclical headwinds",
  "kill_switches": ["Trigger 1", "Trigger 2", "Trigger 3"]
}}
```"""
    return prompt


def parse_agent_response(resp: Any, symbol: str) -> Optional[Dict[str, Any]]:
    """Strips accidental code fences, parses JSON, and validates required keys."""
    if isinstance(resp, dict):
        data = resp
    elif isinstance(resp, str):
        text = resp.strip()
        # Strip markdown code fences if present
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start:end + 1]
        try:
            data = json.loads(text)
        except Exception as e:
            log(f"[{symbol}] Failed to parse JSON from response: {e}")
            return None
    else:
        log(f"[{symbol}] Unexpected response type: {type(resp)}")
        return None

    # Handle alias ticker/symbol
    if "ticker" not in data and "symbol" in data:
        data["ticker"] = data["symbol"]
    if "ticker" not in data:
        data["ticker"] = symbol

    req_keys = [
        "ticker", "verdict", "conviction_score", "pathway_id",
        "cagr_breakdown", "bear_risks", "guidance_audit", "kill_switches"
    ]
    for k in req_keys:
        if k not in data:
            log(f"[{symbol}] Missing required key in agent JSON: {k}")
            return None

    # Normalize verdict
    verdict = str(data["verdict"]).upper().strip()
    if verdict not in ["STRONG_BUY_WATCH", "ACCUMULATE_ON_DIP", "HOLD_WATCH", "REJECT"]:
        log(f"[{symbol}] Invalid verdict value: {verdict}")
        return None
    data["verdict"] = verdict

    return data


def load_reviewed_history() -> Dict[str, Any]:
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return json.loads(content)
        except Exception as e:
            log(f"Warning loading reviewed_history.json: {e}")
    return {}


def save_reviewed_history(history: Dict[str, Any]):
    tmp_path = HISTORY_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    tmp_path.replace(HISTORY_PATH)


def append_jsonl(record: Dict[str, Any], date_str: str):
    target_path = LOGS_DIR / f"screener_audit_{date_str}.jsonl"
    with open(target_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_md(md_text: str, date_str: str):
    target_path = LOGS_DIR / f"screener_audit_{date_str}.md"
    with open(target_path, "a", encoding="utf-8") as f:
        f.write(md_text + "\n")


import shutil
PYTHON_EXE = shutil.which("python") or "python"

async def run_pipeline():
    phase("Stage 1: Screener Batch Preparation & Quant Gate")
    log("Executing Stage 1 Python batch preparation script...")

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    prep_proc = subprocess.run(
        [PYTHON_EXE, "src/local/prepare_screener_batch.py"],
        capture_output=True,
        text=True,
        check=False,
        env=env
    )
    log(prep_proc.stdout)
    if prep_proc.returncode != 0:
        log(f"Preparation script error: {prep_proc.stderr}")
        raise RuntimeError(f"prepare_screener_batch.py failed with code {prep_proc.returncode}")

    # Locate latest queue file
    queue_files = sorted(QUEUES_DIR.glob("queue_*.json"))
    if not queue_files:
        raise FileNotFoundError("No queue_*.json file found in data/prepared_queues/")

    latest_queue_file = queue_files[-1]
    log(f"Reading qualified candidates from {latest_queue_file.name}...")

    with open(latest_queue_file, "r", encoding="utf-8") as f:
        queue_data = json.load(f)

    candidates = queue_data.get("candidates", [])
    total_qualified = len(candidates)
    log(f"Total qualified candidates for AI review: {total_qualified}")

    if total_qualified == 0:
        log("All candidates were filtered by quant criteria or are on active review cooldown. Exiting.")
        return

    phase(f"Stage 2: Parallel Subagent Audits (Batches of {CHUNK_SIZE})")

    today_str = datetime.now().strftime("%Y-%m-%d")
    history = load_reviewed_history()

    num_chunks = (total_qualified + CHUNK_SIZE - 1) // CHUNK_SIZE

    for chunk_idx in range(num_chunks):
        start_i = chunk_idx * CHUNK_SIZE
        chunk = candidates[start_i:start_i + CHUNK_SIZE]
        chunk_symbols = [c["symbol"] for c in chunk]
        log(f"\n--- Starting Chunk {chunk_idx + 1}/{num_chunks}: {chunk_symbols} ---")

        async def audit_stock(cand_item: Dict[str, Any]) -> Dict[str, Any]:
            sym = cand_item["symbol"]
            prompt = build_analyst_prompt(cand_item)
            try:
                a = agent(
                    prompt=prompt,
                    schema={"type": "string"},
                    role=f"Equity Analyst - {sym}",
                    type_name="equity-analyst",
                    model="flash"
                )
                res = await a
                return {"symbol": sym, "candidate": cand_item, "raw_result": res, "success": True}
            except Exception as exc:
                log(f"[{sym}] Subagent execution error: {exc}")
                return {"symbol": sym, "candidate": cand_item, "error": str(exc), "success": False}

        # Run chunk in parallel with try/except isolation per ticker
        results = await parallel(chunk, audit_stock)

        # Process results and checkpoint
        history_updates = 0
        for res in results:
            sym = res["symbol"]
            cand_item = res["candidate"]

            if not res.get("success"):
                log(f"⚠️ [{sym}] Skipping persistence due to execution failure. Will retry on next run.")
                continue

            parsed = parse_agent_response(res.get("raw_result"), sym)
            if not parsed:
                log(f"⚠️ [{sym}] Skipping persistence due to invalid or malformed JSON. Will retry on next run.")
                continue

            # Merge quantitative dossier data with qualitative analyst verdict
            dossier = cand_item.get("dossier", {})
            metrics = dossier.get("derived_metrics", {})
            top_ratios = dossier.get("top_ratios", {})
            growth = dossier.get("compounded_growth", {})
            screens = cand_item.get("matched_screens", [])
            screen_count = cand_item.get("screen_count", len(screens))
            flags = cand_item.get("negative_flags", [])

            verdict = parsed["verdict"]
            conviction = parsed.get("conviction_score", 8)
            pathway = parsed.get("pathway_id", "EARNINGS_COMPOUNDER")
            expected_cagr = parsed.get("expected_cagr_3y", "20%+")
            cagr_breakdown = parsed.get("cagr_breakdown", "")
            margin_of_safety = parsed.get("margin_of_safety", "Moderate")
            thesis = parsed.get("thesis", f"Quality compounder in {', '.join(screens)}.")
            guidance = parsed.get("guidance_audit", "")
            bear_risks = parsed.get("bear_risks", "")
            kill_switches = parsed.get("kill_switches", [])

            cmp_val = metrics.get("current_price") or top_ratios.get("Current Price")
            pe_val = metrics.get("pe") or top_ratios.get("Stock P/E")
            mcap_val = metrics.get("market_cap_cr") or top_ratios.get("Market Cap")
            roce_val = metrics.get("roce_pct") or top_ratios.get("ROCE")
            roe_val = metrics.get("roe_pct") or top_ratios.get("ROE")
            cfo_pat_val = metrics.get("cfo_to_pat_3y")
            debt_eq_val = metrics.get("debt_to_equity")

            sales_3y = extract_growth(growth.get("Compounded Sales Growth", {}), "3 Years")
            pat_3y = extract_growth(growth.get("Compounded Profit Growth", {}), "3 Years")

            # 1. Structured JSONL Record
            record = {
                "timestamp": datetime.now().isoformat(),
                "symbol": sym,
                "name": cand_item.get("name", sym),
                "verdict": verdict,
                "conviction_score": conviction,
                "alpha_pathway": pathway,
                "expected_cagr_3y": expected_cagr,
                "return_math_breakdown": cagr_breakdown,
                "margin_of_safety": margin_of_safety,
                "screen_count": screen_count,
                "matched_screens": screens,
                "negative_flags": flags,
                "flag_assessment": cand_item.get("review_reason") or ("Clean quant profile" if not flags else ", ".join(flags)),
                "metrics": {
                    "market_cap_cr": mcap_val,
                    "current_price": cmp_val,
                    "pe": pe_val,
                    "roce_pct": roce_val,
                    "roe_pct": roe_val,
                    "cfo_to_pat_3y": cfo_pat_val,
                    "cfo_to_ebitda_3y": metrics.get("cfo_to_ebitda_3y"),
                    "debt_to_equity": debt_eq_val,
                    "sales_growth_3y": sales_3y,
                    "profit_growth_3y": pat_3y
                },
                "thesis": thesis,
                "guidance_summary": guidance,
                "risks_summary": bear_risks,
                "bear_kill_switches": kill_switches
            }
            append_jsonl(record, today_str)

            # 2. Markdown Journal Entry
            kill_switches_md = "\n".join([f"  - {ks}" for ks in kill_switches]) if kill_switches else "  - None defined"
            cmp_fmt = f"₹{cmp_val:,.1f}" if isinstance(cmp_val, (int, float)) else str(cmp_val)
            mcap_fmt = f"₹{mcap_val:,.0f} Cr" if isinstance(mcap_val, (int, float)) else str(mcap_val)
            flags_fmt = ", ".join(flags) if flags else "None - Clean quant profile"

            md_block = f"""### [{verdict}] {sym} - {cand_item.get('name', sym)}
- **Date**: {today_str}
- **Conviction Score**: {conviction}/10 | **Alpha Pathway**: {pathway}
- **Expected 3Y CAGR**: {expected_cagr} (Return Math: {cagr_breakdown})
- **Margin of Safety**: {margin_of_safety}
- **Matched Screens**: {', '.join(screens)} (Hits: {screen_count})
- **Warning Flags**: {flags_fmt}
- **Key Metrics**: CMP: {cmp_fmt} | P/E: {pe_val}x | M.Cap: {mcap_fmt} | ROCE: {roce_val}% | 3Y CFO/PAT: {cfo_pat_val}x
- **Moat & Growth Drivers**: {thesis}
- **FY27/FY28 Management Guidance**: {guidance}
- **Risks & Red Flags**: {bear_risks}
- **Thesis Invalidation Triggers**:
{kill_switches_md}
---"""
            append_md(md_block, today_str)

            # 3. Checkpoint to Reviewed History
            history[sym.upper()] = {
                "last_reviewed_at": datetime.now().isoformat(),
                "screen_count": screen_count,
                "verdict": verdict,
                "matched_screens": screens,
                "metrics": {
                    "market_cap_cr": mcap_val,
                    "current_price": cmp_val,
                    "pe": pe_val,
                    "roce_pct": roce_val,
                    "roe_pct": roe_val,
                    "cfo_to_pat_3y": cfo_pat_val,
                    "debt_to_equity": debt_eq_val,
                    "sales_growth_3y": sales_3y,
                    "profit_growth_3y": pat_3y
                }
            }
            history_updates += 1
            log(f"✅ [{sym}] Verdict: {verdict} ({conviction}/10) | Expected CAGR: {expected_cagr}")

        # Atomic per-chunk checkpoint flush
        if history_updates > 0:
            save_reviewed_history(history)
            log(f"💾 Checkpointed {history_updates} reviewed stocks to data/reviewed_history.json")

    phase("Stage 3: Push Screener Digest to Telegram")
    log("Executing push_screener_digest.py...")

    push_proc = subprocess.run(
        [PYTHON_EXE, "src/local/push_screener_digest.py"],
        capture_output=True,
        text=True,
        check=False,
        env=env
    )
    log(push_proc.stdout)
    if push_proc.returncode != 0:
        log(f"⚠️ Push screener digest output: {push_proc.stderr}")
    else:
        log("Telegram notification digest sent successfully.")

    log("\n🎯 Screener Pipeline Autonomous Run Completed Successfully!")


# Antigravity workflow runner entry point
await run_pipeline()

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
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from src.local.brokerage_targets import fetch_brokerage_consensus

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
PYTHON_EXE = shutil.which("python") or "python"


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

    # Harmonize with live yFinance market & forward metrics
    b_data = fetch_brokerage_consensus(sym)
    b_target = f"₹{b_data['target_median']} ({'+' if (b_data.get('upside_pct') or 0) > 0 else ''}{b_data.get('upside_pct')}% upside | {b_data.get('analyst_count')} brokers)" if b_data.get("has_coverage") else "Unrated by sell-side brokers"
    b_fwd_pe = f"{b_data['forward_pe']}x" if b_data.get("forward_pe") else "N/A"
    b_fwd_eps = f"₹{b_data['forward_eps']}" if b_data.get("forward_eps") else "N/A"
    b_ev_ebitda = f"{b_data['ev_to_ebitda']}x" if b_data.get("ev_to_ebitda") else "N/A"
    b_revisions = b_data.get("eps_revisions_30d") or "None in last 30D"
    b_trend = b_data.get("eps_trend_30d") or "Stable / unguided"
    b_news = "\n".join([f"  * {hl}" for hl in b_data.get("recent_news", [])]) if b_data.get("recent_news") else "  * None available"

    # Direct Industry Peers
    peers = dossier.get("peers", [])
    if peers:
        peer_lines = [f"  * {p['name']}: CMP ₹{p['cmp']} | P/E: {p['pe']}x | M.Cap: ₹{p['mcap_cr']} Cr | ROCE: {p['roce']}%" for p in peers[:3]]
        peers_str = "\n".join(peer_lines)
    else:
        peers_str = "  * None available"

    # Technical Levels & Support Proximity
    t_levels = tech.get("technical_levels", {})
    cmp_now = t_levels.get("cmp", metrics.get("current_price", top_ratios.get("Current Price", "N/A")))
    support_prox = t_levels.get("support_proximity", "N/A")
    swing_low = t_levels.get("swing_low_20d", "N/A")
    swing_high = t_levels.get("swing_high_20d", "N/A")
    ema20_val = t_levels.get("ema20", "N/A")
    ema50_val = t_levels.get("ema50", "N/A")
    ema200_val = t_levels.get("ema200", "N/A")
    dist_20 = t_levels.get("dist_to_ema20_pct", "N/A")
    dist_50 = t_levels.get("dist_to_ema50_pct", "N/A")
    dist_200 = t_levels.get("dist_to_ema200_pct", "N/A")
    b_low = b_data.get("target_low", "N/A")

    prompt = f"""You are analyzing candidate stock {sym} ({name}).
Evaluate this single stock using its pre-prepared quantitative dossier below.
Follow the STRICT FORWARD GROWTH PHASE & SECTOR LIFECYCLE EVALUATION PROTOCOL.

### CANDIDATE QUANTITATIVE DOSSIER:
- Symbol: {sym} | Company: {name}
- Matched Screens: {', '.join(screens)} (Hits: {candidate.get('screen_count', 1)})
- Rubrics: {' '.join(rubrics)}
- Market Cap: ₹{metrics.get('market_cap_cr', top_ratios.get('Market Cap', 'N/A'))} Cr | CMP: ₹{cmp_now}
- Drawdown: Down from 52W High: {down_52w}% | Down from ATH: {down_ath}%
- Valuation & Quality (Screener): Stock P/E: {metrics.get('pe', top_ratios.get('Stock P/E', 'N/A'))}x | ROCE: {metrics.get('roce_pct', top_ratios.get('ROCE', 'N/A'))}% | ROE: {metrics.get('roe_pct', top_ratios.get('ROE', 'N/A'))}%
- Forward Valuation & Consensus (yFinance): Street Target: {b_target} | Forward P/E: {b_fwd_pe} | Forward EPS: {b_fwd_eps} | EV/EBITDA: {b_ev_ebitda}
- Institutional Revisions & Trajectory: 30D EPS Revisions: {b_revisions} | 30D EPS Drift: {b_trend}
- Direct Industry Peers (Screener):
{peers_str}
- Structural Technical Support Levels:
  * Current CMP: ₹{cmp_now}
  * Support Proximity: {support_prox}
  * Moving Averages: EMA20: ₹{ema20_val} ({dist_20}%) | EMA50: ₹{ema50_val} ({dist_50}%) | EMA200: ₹{ema200_val} ({dist_200}%)
  * 20-Day Range: Swing Low ₹{swing_low} | Swing High ₹{swing_high}
  * Broker Floor (Lowest Analyst Target): ₹{b_low}
- Recent News & Concall Summaries:
{b_news}
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

### STRICT FORWARD GROWTH PHASE & SECTOR LIFECYCLE PROTOCOL:
1. SECTOR LIFECYCLE (Sunrise vs. Sunset vs. Contra Bet vs. Deep Value):
   - Quant screens already filter past growth (3Y sales/profit > 20%, ROCE > 20%, PEG < 2).
   - Your primary job is to evaluate FUTURE GROWTH VISIBILITY for FY27-FY29:
     * SUNRISE_SECTOR: Expanding TAM (>18-25% CAGR), multi-year tailwinds (Power T&D, Defense, EMS, Renewable/BESS, Data Centers, CDMO, Quick Commerce). High conviction candidates.
     * CONTRA_BET: Structural value traps (IT facing AI disruption, commoditized agrochem destocking) must wait on HOLD_WATCH.
     * DEEP-VALUE FRANCHISE MISPRICING: If a stock is trading at a deep drawdown (>40% from ATH) but has stellar unit economics (ROA > 5%, ROE > 15%, pristine balance sheet e.g. Aptus Value Housing) and sell-side consensus confirms BUY with Forward P/E < 15x: DO NOT PENALIZE as a broken trap! Classify as PE_RERATING or EARNINGS_COMPOUNDER and award ACCUMULATE_ON_DIP.
     * CYCLICAL_INFLECTION: Upcoming capex coming online in FY26-FY28 backed by firm order book.
     * SUNSET_STAGNANT: Low volume growth (<8%), commoditized price-taker, moat decay. Mandatory REJECT or HOLD_WATCH.
2. FORWARD GROWTH PHASE AUDIT (FY27-FY29 Visibility):
   - Check concall and broker reports for: executable order book (2x-3x revenue), capacity commissioning dates, margin durability.
   - Cross-check Street consensus Forward P/E and EPS revisions: Are analysts revising estimates UP or DOWN?
   - Is future growth accelerating or plateauing?
3. ACTIONABLE ENTRY ZONE & STRUCTURAL RISK/REWARD SETUP:
   - CRITICAL: Never use fixed, arbitrary percentage targets or static discount formulas!
   - Determine whether the stock is ALREADY at structural support (testing 20 EMA, 50 EMA, 20-day swing low, or at historical valuation floor like Aptus at single-digit P/E):
     * If ALREADY at support: Recommend immediate entry at CMP / current levels (e.g. "ACCUMULATE_AT_CMP: ₹{cmp_now}–₹{round(float(cmp_now)*1.02, 1) if isinstance(cmp_now, (int, float)) else cmp_now}"). DO NOT make the user wait for a lower pullback that risks missing the move!
     * If extended (>5% above 20/50 EMA): Specify a realistic pullback accumulation range anchored to nearest EMA support.
     * Invalidation Stop-Loss: Set strictly below structural support (e.g. below 20-day swing low, 200 EMA, or broker floor), calculating exact % downside risk.
     * Risk-to-Reward Ratio: Calculate (Target Upside % / Risk %). Must be favorable (>= 3:1).
4. ANTI-DERATING & FORWARD PEG GATE:
   - Calculate Forward PEG = Current P/E / Forward Guided EPS Growth %.
   - If P/E > 30x and forward guidance is < 16% (or unguided), DOWNGRADE to HOLD_WATCH or REJECT.
   - Net 3Y CAGR = Forward EPS Growth % - Derating Drag %. Must be >= 20% (>=25% for STRONG_BUY_WATCH).
5. RUTHLESS INSTITUTIONAL SELECTIVITY:
   - Target approval rate: Only 10-15% of candidates (2-4 stocks out of 25-30) should ever be approved!
   - STRONG_BUY_WATCH: Conviction 8.5-10.0, Sunrise Sector / Capex Inflection, verified forward growth >= 20%, Forward PEG <= 1.8x, Net CAGR >= 25%.
   - ACCUMULATE_ON_DIP: Conviction 7.5-8.4, growing sector or deep-value franchise mispricing, solid moat, forward growth >= 16%, pullbacks elevate return to >=25%.
   - HOLD_WATCH: Conviction 5.0-7.4. All structural traps, mature plateaus, or unguided stocks. (Excluded from Telegram alerts).
   - REJECT: Conviction 1.0-4.9. Disrupted sectors, growth traps, governance flags. (Excluded from Telegram alerts).

CRITICAL: Return ONLY a valid, compact JSON object matching this schema. No introductory text, no conversational filler, no markdown report text.
```json
{{
  "ticker": "{sym}",
  "verdict": "STRONG_BUY_WATCH|ACCUMULATE_ON_DIP|HOLD_WATCH|REJECT",
  "conviction_score": 8.5,
  "sector_classification": "SUNRISE_SECTOR|CYCLICAL_INFLECTION|CONTRA_BET|SUNSET_STAGNANT",
  "future_growth_phase": "ACCELERATING_EXPANSION|SECULAR_COMPOUNDER|UNCERTAIN_CONTRA|MATURE_PLATEAU|STRUCTURAL_DECLINE",
  "pathway_id": "PE_RERATING|EARNINGS_COMPOUNDER|CAPEX_INFLECTION|CYCLICAL_MARKET_PROXY",
  "expected_cagr_3y": "22-26%",
  "cagr_breakdown": "Forward EPS growth 24% - Multiple derating drag 3% = Net 21% CAGR",
  "margin_of_safety": "High|Moderate|Low with brief justification",
  "why_picked": "Specific structural catalyst, moat, and market share driver in a sunrise sector",
  "actionable_trade_setup": {{
    "entry_strategy": "ACCUMULATE_AT_CMP|ACCUMULATE_ON_DIP|BREAKOUT_ENTRY",
    "entry_zone": "e.g. ₹1,960–₹1,990 (CMP is currently testing 50 EMA support; accumulate now)",
    "invalidation_stop_loss": "e.g. ₹1,880 (-5.3% below 50 EMA and 20D swing low)",
    "risk_reward_ratio": "e.g. 4.2:1 (Target +42% vs Risk -5.3%)",
    "setup_rationale": "Why this entry was selected (e.g. stock already resting at critical 50 EMA support after 12% consolidation; waiting for lower prices risks missing the move)"
  }},
  "concall_key_takeaways": "2-3 bullet points: Management commitments on FY26-FY28 revenue targets, capacity commissioning dates, and margin guidance",
  "peer_comparison_verdict": "How this stock compares to top industry peers on P/E, ROCE, and valuation discount",
  "forward_growth_catalysts": "Concrete forward visibility: Upcoming capacity commissioning dates, order book executable timeline, or market-share expansion",
  "sector_and_contra_analysis": "Explicit evaluation of whether sector is Sunrise vs Sunset, and if a deep-value mispricing, why unit economics justify rerating",
  "forward_guidance_vs_pe": "FY27/FY28 guided growth % vs current P/E (Forward PEG: X.Xx). Concall analyst scrutiny summary.",
  "derating_risk_floor_pe": "Multiple compression downside if growth decelerates: 5Y median P/E and valuation floor",
  "bear_risks": "Summary of forensic risks, balance sheet/working capital, and cyclical headwinds",
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

    # Handle synonyms and fallback keys
    if "why_picked" not in data and "thesis" in data:
        data["why_picked"] = data["thesis"]
    if "thesis" not in data and "why_picked" in data:
        data["thesis"] = data["why_picked"]

    if "forward_guidance_vs_pe" not in data and "guidance_audit" in data:
        data["forward_guidance_vs_pe"] = data["guidance_audit"]
    if "guidance_audit" not in data and "forward_guidance_vs_pe" in data:
        data["guidance_audit"] = data["forward_guidance_vs_pe"]

    if "derating_risk_floor_pe" not in data and "bear_risks" in data:
        data["derating_risk_floor_pe"] = data["bear_risks"]

    # Sector & future growth phase fallbacks
    if "sector_classification" not in data:
        data["sector_classification"] = "SUNRISE_SECTOR" if str(data.get("verdict")) == "STRONG_BUY_WATCH" else "CYCLICAL_INFLECTION"
    if "future_growth_phase" not in data:
        data["future_growth_phase"] = "ACCELERATING_EXPANSION" if str(data.get("verdict")) == "STRONG_BUY_WATCH" else "SECULAR_COMPOUNDER"
    if "forward_growth_catalysts" not in data:
        data["forward_growth_catalysts"] = data.get("why_picked", "")
    if "sector_and_contra_analysis" not in data:
        data["sector_and_contra_analysis"] = data.get("thesis", "")

    req_keys = [
        "ticker", "verdict", "conviction_score", "pathway_id",
        "cagr_breakdown", "kill_switches"
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
    evaluated_symbols = []

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

            evaluated_symbols.append(sym)

            # Merge quantitative dossier data with qualitative analyst verdict
            dossier = cand_item.get("dossier", {})
            metrics = dossier.get("derived_metrics", {})
            top_ratios = dossier.get("top_ratios", {})
            growth = dossier.get("compounded_growth", {})
            screens = cand_item.get("matched_screens", [])
            screen_count = cand_item.get("screen_count", len(screens))
            flags = cand_item.get("negative_flags", [])

            verdict = parsed["verdict"]
            conviction = parsed.get("conviction_score", 8.0)
            sector_class = parsed.get("sector_classification", "SUNRISE_SECTOR")
            growth_phase = parsed.get("future_growth_phase", "ACCELERATING_EXPANSION")
            pathway = parsed.get("pathway_id", "EARNINGS_COMPOUNDER")
            expected_cagr = parsed.get("expected_cagr_3y", "20%+")
            cagr_breakdown = parsed.get("cagr_breakdown", "")
            margin_of_safety = parsed.get("margin_of_safety", "Moderate")
            why_picked = parsed.get("why_picked") or parsed.get("thesis", f"Quality compounder in {', '.join(screens)}.")
            thesis = why_picked
            forward_growth_catalysts = parsed.get("forward_growth_catalysts", "")
            sector_contra = parsed.get("sector_and_contra_analysis", "")
            forward_guidance = parsed.get("forward_guidance_vs_pe") or parsed.get("guidance_audit", "")
            guidance = forward_guidance
            derating_risk = parsed.get("derating_risk_floor_pe") or parsed.get("bear_risks", "")
            bear_risks = derating_risk
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
                "sector_classification": sector_class,
                "future_growth_phase": growth_phase,
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
                "why_picked": why_picked,
                "thesis": thesis,
                "concall_key_takeaways": parsed.get("concall_key_takeaways", ""),
                "peer_comparison_verdict": parsed.get("peer_comparison_verdict", ""),
                "peers": dossier.get("peers", []),
                "actionable_trade_setup": parsed.get("actionable_trade_setup", {}),
                "technical_levels": tech.get("technical_levels", {}),
                "forward_growth_catalysts": forward_growth_catalysts,
                "sector_and_contra_analysis": sector_contra,
                "forward_guidance_vs_pe": forward_guidance,
                "guidance_summary": guidance,
                "derating_risk_floor_pe": derating_risk,
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
- **Sector Lifecycle**: {sector_class} | **Future Growth Phase**: {growth_phase}
- **Expected 3Y CAGR**: {expected_cagr} (Return Math: {cagr_breakdown})
- **Margin of Safety**: {margin_of_safety}
- **Matched Screens**: {', '.join(screens)} (Hits: {screen_count})
- **Warning Flags**: {flags_fmt}
- **Key Metrics**: CMP: {cmp_fmt} | P/E: {pe_val}x | M.Cap: {mcap_fmt} | ROCE: {roce_val}% | 3Y CFO/PAT: {cfo_pat_val}x
- **Sector Dynamics & Contra Analysis**: {sector_contra}
- **Future Growth Drivers (FY27-FY29)**: {forward_growth_catalysts}
- **Why It Was Picked (Moat & Catalyst)**: {why_picked}
- **Forward Guidance vs. P/E (Anti-Derating)**: {forward_guidance}
- **Bear Derating Risk & Valuation Floor**: {derating_risk}
- **Thesis Invalidation Triggers**:
{kill_switches_md}
---"""
            append_md(md_block, today_str)

            # 3. Checkpoint to Reviewed History
            history[sym.upper()] = {
                "last_reviewed_at": datetime.now().isoformat(),
                "screen_count": screen_count,
                "verdict": verdict,
                "sector_classification": sector_class,
                "future_growth_phase": growth_phase,
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
            log(f"✅ [{sym}] Verdict: {verdict} ({conviction}/10) | Sector: {sector_class} ({growth_phase}) | Expected CAGR: {expected_cagr}")

        # Atomic per-chunk checkpoint flush
        if history_updates > 0:
            save_reviewed_history(history)
            log(f"💾 Checkpointed {history_updates} reviewed stocks to data/reviewed_history.json")

    phase("Stage 3: Push Screener Digest to Telegram")
    log("Executing push_screener_digest.py with newly evaluated symbols...")

    cmd = [PYTHON_EXE, "src/local/push_screener_digest.py"]
    if evaluated_symbols:
        cmd.extend(["--symbols", ",".join(evaluated_symbols)])

    push_proc = subprocess.run(
        cmd,
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

---
name: run-screener-pipeline
description: End-to-end AlphaMinter local screener and autonomous batched AI evaluation pipeline. Runs zero-quota Python preparation, evaluates candidates in parallel chunks of 3 via equity-analyst, and pushes strong picks to Telegram with 1-tap triage buttons.
---

# AlphaMinter Screener & Autonomous AI Evaluation Pipeline

> [!IMPORTANT]
> **STRICT OPERATIONAL DIRECTIVES (DO NOT DEVIATE):**
> 1. **DO NOT read codebase files, browse directories, or explore repository structure.**
> 2. **DO NOT run unit tests, test suites, or pytest.** This is an operational scheduled data run, NOT a software development or debugging task.
> 3. **IMMEDIATELY execute the workflow** via `run_workflow` on your very first tool turn without preliminary steps.

---

## Autonomous Execution via `run_workflow`

Launch the end-to-end background workflow:
```python
run_workflow(
    ScriptPath="workflows/screener_pipeline.py",
    Description="AlphaMinter End-to-End Screener Pipeline"
)
```

The workflow runs entirely in the background and performs:
1. **Stage 1 (Data Prep & Quant Gate)**: Runs `prepare_screener_batch.py` to crawl screens, filter non-equities, exclude silenced/cooldown stocks, score negative parameters, and output surviving candidates.
2. **Stage 2 (Parallel Batched AI Audits)**: Assembles each stock's full context prompt in Python and runs `@equity-analyst` concurrently in chunks of 3 (`CHUNK_SIZE = 3`) with per-stock try/except isolation.
3. **Stage 3 (Deterministic Persistence & Checkpointing)**: Ingests pure JSON verdicts, strips code fences, validates required keys, deterministically updates `logs/screener_audit_YYYY-MM-DD.md` and `logs/screener_audit_YYYY-MM-DD.jsonl`, and atomically checkpoints `data/reviewed_history.json` after each chunk (skipping failed stocks for retry safety).
4. **Stage 4 (Telegram Dispatch)**: Automatically triggers `push_screener_digest.py` to push formatted Telegram cards with interactive triage buttons.

---

## Conclude & Summarize
Once the workflow notification arrives, output a concise summary of the attrition funnel, evaluated candidate verdicts, and dispatched Telegram alerts.

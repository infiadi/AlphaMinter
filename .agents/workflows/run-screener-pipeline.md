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

Execute the workflow in the background:
```python
run_workflow(
    ScriptPath="workflows/screener_pipeline.py",
    Description="AlphaMinter End-to-End Screener Pipeline"
)
```

The script autonomously coordinates:
- Stage 1: Data preparation, screen merging, and quant gating (`src/local/prepare_screener_batch.py`).
- Stage 2: Parallel `@equity-analyst` evaluation in chunks of 3 with try/except error isolation.
- Stage 3: Deterministic Python markdown & JSONL generation and atomic per-chunk `data/reviewed_history.json` checkpointing.
- Stage 4: Telegram alert dispatch (`src/local/push_screener_digest.py`).

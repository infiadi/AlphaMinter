# AlphaMinter Agent Operational Rules

## Operational Pipeline Execution
When instructed to run the Screener Pipeline (`run-screener-pipeline`, scheduled task, or "Run Now"):
1. **NO CODEBASE RECONNAISSANCE**: Do NOT inspect repository files, read source code, or explore directory trees.
2. **NO TEST SUITE RUNS**: Do NOT run test suites (`pytest`, `unittest`, or test commands). This is an operational scheduled data run, NOT a software development or debugging turn.
3. **IMMEDIATE WORKFLOW EXECUTION**: Start directly by executing the automated background workflow `workflows/screener_pipeline.py` via `run_workflow` on your very first tool turn:
   ```python
   run_workflow(
       ScriptPath="workflows/screener_pipeline.py",
       Description="AlphaMinter End-to-End Screener Pipeline"
   )
   ```
4. **NO MANUAL STEP-BY-STEP ORCHESTRATION**: Do NOT manually invoke subagents 1-by-1, and do NOT manually write or append to `.md`, `.jsonl`, or `data/reviewed_history.json`. The Python workflow script handles all data preparation, parallel subagent batches (chunks of 3), error-isolated evaluation, deterministic journal formatting, atomic checkpointing, and Telegram dispatch autonomously in the background.
5. **CONCLUDE & REPORT**: When the background workflow completes, summarize the attrition funnel, reviewed verdicts, and Telegram dispatch results for the user.

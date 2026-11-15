# AlphaMinter Agent Operational Rules

## Operational Pipeline Execution
When instructed to run the Screener Pipeline (`run-screener-pipeline`, scheduled task, or "Run Now"):
1. **NO CODEBASE RECONNAISSANCE**: Do NOT inspect repository files, read source code, or explore directory trees.
2. **NO TEST SUITE RUNS**: Do NOT run test suites (`pytest`, `unittest`, or test commands). This is an operational scheduled data run, NOT a software development or debugging turn.
3. **IMMEDIATE EXECUTION**: Start directly by executing `python src/local/prepare_screener_batch.py` via `run_command` on your very first tool turn.
4. **NO SCRATCH SCRIPTS & SEQUENTIAL 1-STOCK-AT-A-TIME SUBAGENTS**: Do NOT write temporary Python scripts or scratch files to build prompts, slice JSON, or parse queues. The pipeline automatically pre-renders ready-to-run prompt batches in `data/prepared_queues/active_batch_1.json`, `active_batch_2.json`, etc. Read `data/prepared_queues/active_batch_1.json` directly with `read_file` and invoke `@equity-analyst` sequentially (1 stock at a time). Record verdicts into `logs/screener_audit_YYYY-MM-DD.{md,jsonl}` and atomically checkpoint `data/reviewed_history.json`. Repeat for subsequent batches.
5. **DISPATCH & FINISH**: Run `python src/local/push_screener_digest.py` to push approved picks to Telegram, output the attrition funnel and verdict summary, and finish.

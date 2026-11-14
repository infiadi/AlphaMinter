# AlphaMinter Agent Operational Rules

## Operational Pipeline Execution
When instructed to run the Screener Pipeline (`run-screener-pipeline`, scheduled task, or "Run Now"):
1. **NO CODEBASE RECONNAISSANCE**: Do NOT inspect repository files, read source code, or explore directory trees.
2. **NO TEST SUITE RUNS**: Do NOT run test suites (`pytest`, `unittest`, or test commands). This is an operational scheduled data run, NOT a software development or debugging turn.
3. **IMMEDIATE EXECUTION**: Start directly by executing `python src/local/prepare_screener_batch.py` via `run_command` on your very first tool turn.
4. **PARALLEL SUBAGENT BATCHES (3 to 5 AT A TIME)**: Read the generated queue file and invoke `@equity-analyst` in parallel batches of 3 to 5 concurrent subagents. Ensure each subagent performs thorough due diligence including sector analysis, forward-looking concall guidance, and forensic red-flag checks, with sector-specific awareness (financials evaluated on ROE/NIM/NPAs, multi-year compounders cushioned against 1 transient negative quarter).
5. **DISPATCH & FINISH**: Run `python src/local/push_screener_digest.py` to push approved picks to Telegram, output the attrition funnel and verdict summary, and finish.

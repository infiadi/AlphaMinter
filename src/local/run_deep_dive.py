import os
import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import List

# Ensure project root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.local.deep_dive_engine import DeepDiveEngine, DATA_DIR, REPORTS_DIR
from src.storage.state_store import StateStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("run_deep_dive")


def list_existing_deep_dives():
    """Lists all stocks currently stored in the deep dive repository."""
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        print("\n📭 No deep dive records found in data/deep_dives/.\n")
        return

    print("\n" + "="*80)
    print("🔬 ALPHAMINTER STORED DEEP DIVE DOSSIERS")
    print("="*80)
    print(f"{'SYMBOL':<12} | {'VERDICT':<22} | {'CAGR TARGET':<12} | {'LAST REVIEWED':<14} | {'UPDATES'}")
    print("-"*80)

    import json
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as jf:
                data = json.load(jf)
                sym = data.get("symbol", f.stem)
                verdict = data.get("verdict", "N/A")
                cagr = data.get("target_3y_cagr", "N/A")
                last_dt = data.get("last_updated_date", "N/A")
                updates_count = len(data.get("monthly_updates", []))
                print(f"{sym:<12} | {verdict:<22} | {cagr:<12} | {last_dt:<14} | {updates_count} delta(s)")
        except Exception as e:
            logger.warning(f"Error reading {f.name}: {e}")

    print("="*80 + "\n")


def run_monthly_batch_checks(engine: DeepDiveEngine, force: bool = False):
    """Audits all existing deep dives that are >= 30 days old."""
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        logger.info("No deep dives found to check.")
        return

    logger.info(f"Scanning {len(files)} existing deep dives for 30-day delta updates...")
    updated_count = 0

    for f in files:
        sym = f.stem.upper()
        try:
            res = engine.perform_monthly_delta_check(sym, force=force)
            if res.get("status") != "SKIPPED_COOLDOWN":
                updated_count += 1
                logger.info(f"✅ Completed 30-day delta check for {sym}")
        except Exception as e:
            logger.error(f"Error checking {sym}: {e}")

    logger.info(f"Monthly check run complete. Processed {updated_count} updates out of {len(files)} stocks.")


def process_cloudflare_queue(engine: DeepDiveEngine, state_store: StateStore):
    """Fetches tickers queued via Telegram [🔬 Deep Dive] button and analyzes them."""
    logger.info("Syncing pending deep dive requests from Cloudflare D1 queue...")
    queue = state_store.get_deep_dive_queue()
    if not queue:
        logger.info("No pending deep dive requests in queue.")
        return

    logger.info(f"Found {len(queue)} pending deep dive requests: {', '.join(queue)}")
    for sym in queue:
        try:
            logger.info(f"\n---> Analyzing Queued Stock: {sym}")
            engine.perform_full_deep_dive(sym)
            logger.info(f"Completed deep dive for {sym}.")
        except Exception as e:
            logger.error(f"Failed to analyze queued stock {sym}: {e}")


def main():
    parser = argparse.ArgumentParser(description="AlphaMinter 25%+ Sure-Shot Growth Deep Dive & Delta Engine")
    parser.add_argument("--symbol", "-s", type=str, help="Single stock ticker to analyze (e.g. ANANTRAJ, TRENT)")
    parser.add_argument("--force", action="store_true", help="Force re-analysis or bypass 30-day delta cooldown")
    parser.add_argument("--update", "-u", action="store_true", help="Perform 30-day delta update on --symbol")
    parser.add_argument("--from-queue", "-q", action="store_true", help="Process pending requests from Cloudflare Worker queue")
    parser.add_argument("--monthly-check", "-m", action="store_true", help="Run 30-day delta checks across all stored deep dives")
    parser.add_argument("--list", "-l", action="store_true", help="List all stored deep dive records and dates")

    args = parser.parse_args()

    state_store = StateStore()
    engine = DeepDiveEngine(state_store=state_store)

    if args.list:
        list_existing_deep_dives()
        return

    if args.from_queue:
        process_cloudflare_queue(engine, state_store)
        return

    if args.monthly_check:
        run_monthly_batch_checks(engine, force=args.force)
        return

    if args.symbol:
        sym = args.symbol.strip().upper()
        if args.update:
            logger.info(f"Running 30-day delta update for {sym}...")
            engine.perform_monthly_delta_check(sym, force=args.force)
        else:
            logger.info(f"Running full institutional deep dive for {sym}...")
            engine.perform_full_deep_dive(sym, force=args.force)
        return

    parser.print_help()


if __name__ == "__main__":
    main()

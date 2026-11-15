"""
Utility to reset AlphaMinter pipeline state for a 100% clean, first-time run.
Resets:
1. data/reviewed_history.json (45-day review cooldown)
2. data/dispatched_alerts.json (7-day Telegram deduplication shield)
3. data/cache/screens/ (6-hour screener web page cache)
4. data/prepared_queues/ (temporary batch queues)
"""

import sys
import json
import shutil
import argparse
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
QUEUES_DIR = DATA_DIR / "prepared_queues"
HISTORY_PATH = DATA_DIR / "reviewed_history.json"
DISPATCH_PATH = DATA_DIR / "dispatched_alerts.json"


def reset_pipeline_state(clear_dossier_cache: bool = False, backup: bool = True):
    print("=" * 60)
    print("🧹 AlphaMinter Pipeline State Reset (Clean First-Time Run)")
    print("=" * 60)

    # Backup directory
    if backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = DATA_DIR / "backups" / f"backup_{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        if HISTORY_PATH.exists():
            shutil.copy(HISTORY_PATH, backup_dir / "reviewed_history.json")
        if DISPATCH_PATH.exists():
            shutil.copy(DISPATCH_PATH, backup_dir / "dispatched_alerts.json")
        print(f"📦 Backed up current state to: {backup_dir}")

    # 1. Reset reviewed_history.json
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({}, f, indent=2)
    print("✅ Reset data/reviewed_history.json -> {} (45-day review cooldown cleared)")

    # 2. Reset dispatched_alerts.json
    with open(DISPATCH_PATH, "w", encoding="utf-8") as f:
        json.dump({}, f, indent=2)
    print("✅ Reset data/dispatched_alerts.json -> {} (7-day Telegram deduplication cleared)")

    # 3. Clear Screen Web Cache
    screens_cache = CACHE_DIR / "screens"
    if screens_cache.exists():
        for f in screens_cache.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
        print("✅ Cleared data/cache/screens/ (Forces fresh live crawls)")

    # 4. Clear Prepared Queues
    if QUEUES_DIR.exists():
        for f in QUEUES_DIR.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
        print("✅ Cleared data/prepared_queues/ (Temporary batches cleared)")

    # 5. Clear Dossiers Cache if full reset requested
    if clear_dossier_cache:
        dossiers_cache = CACHE_DIR / "dossiers"
        if dossiers_cache.exists():
            for f in dossiers_cache.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
            print("✅ Cleared data/cache/dossiers/ (Forces fresh financial table parsing)")

    print("=" * 60)
    print("✨ Pipeline state is now 100% clean and ready for a fresh first-time run!")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset AlphaMinter state for a clean first-time run")
    parser.add_argument("--full", action="store_true", help="Also clear company financial dossier caches")
    parser.add_argument("--no-backup", action="store_true", help="Do not create a backup in data/backups/")
    args = parser.parse_args()

    reset_pipeline_state(clear_dossier_cache=args.full, backup=not args.no_backup)

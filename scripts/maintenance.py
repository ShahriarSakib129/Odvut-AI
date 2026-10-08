#!/usr/bin/env python3
"""Housekeeping: expire pending questions, purge old logs / cache / sessions.

Run it manually (Render Shell / cron) or let the bot do it automatically --
the bot already calls the same code every ``MAINTENANCE_INTERVAL_SECONDS``.

Usage
-----
    python scripts/maintenance.py                 # run everything
    python scripts/maintenance.py --dry-run       # show what would be removed
    python scripts/maintenance.py --vacuum        # + VACUUM ANALYZE (slow)
    python scripts/maintenance.py --logs-days 14  # keep 14 days of message logs
"""

from __future__ import annotations

import argparse
import sys

from _bootstrap import bootstrap, print_header, print_result

from config import get_settings
from utils.helpers import human_count


def _counts(database) -> dict[str, int]:
    return {
        "admin_messages": database.count_admin_messages(),
        "qa_memory": database.count_qa_pairs(),
        "users": database.count_users(),
        "open_questions": database.count_open_questions(),
        "message_logs": database.count_message_logs(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="INFO GROUP AI BOT - maintenance")
    parser.add_argument("--dry-run", action="store_true",
                        help="only report the current row counts")
    parser.add_argument("--vacuum", action="store_true",
                        help="also run VACUUM ANALYZE on the memory tables")
    parser.add_argument("--logs-days", type=int, default=30,
                        help="message_logs retention (default: 30 days)")
    parser.add_argument("--cache-grace-minutes", type=int, default=60,
                        help="keep expired cache rows this long (default: 60)")
    args = parser.parse_args(argv)

    settings, database = bootstrap()
    print_header("INFO GROUP AI BOT - maintenance")

    if not database.is_available():
        print("[FAIL] database is not reachable - nothing to do")
        return 2

    print_result(True, f"database: {settings.redacted_database_url}")
    before = _counts(database)
    print("\ncurrent rows:", {k: human_count(v) for k, v in before.items()})

    if args.dry_run:
        print("\n(--dry-run: nothing was deleted)")
        return 0

    result = database.run_maintenance(
        log_retention_days=args.logs_days,
        cache_grace_minutes=args.cache_grace_minutes,
    )
    print("\nmaintenance result:", result)
    after = _counts(database)
    print("rows now      :", {k: human_count(v) for k, v in after.items()})

    if args.vacuum:
        print("\nrunning VACUUM ANALYZE (this can take a moment)...")
        database.vacuum_analyze()
        print("[OK] vacuum finished")

    failed = bool(result.get("error"))
    print("\n[DONE]" if not failed else "\n[PARTIAL] check the log lines above")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

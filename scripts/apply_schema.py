"""Create/update the database schema.

Usage
-----
    python scripts/apply_schema.py                 # uses DATABASE_URL
    python scripts/apply_schema.py --dry-run       # only print what would run
    python scripts/apply_schema.py --url "postgresql://..."

Safe to run repeatedly: ``database/schema.sql`` is idempotent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _bootstrap import PROJECT_ROOT, bootstrap, print_header, print_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply database/schema.sql")
    parser.add_argument("--url", help="override DATABASE_URL for this run")
    parser.add_argument("--dry-run", action="store_true", help="do not execute SQL")
    parser.add_argument("--file", default=str(PROJECT_ROOT / "database" / "schema.sql"))
    args = parser.parse_args(argv)

    if args.url:
        import os
        os.environ["DATABASE_URL"] = args.url

    settings, database = bootstrap()
    print_header("INFO GROUP AI BOT - database schema")

    sql_path = Path(args.file)
    if not sql_path.exists():
        print_result(False, f"schema file not found: {sql_path}")
        return 2
    sql = sql_path.read_text(encoding="utf-8")
    print(f"schema file : {sql_path} ({len(sql)} bytes)")
    print(f"database    : {settings.redacted_database_url or '(DATABASE_URL not set)'}")

    if not settings.database_url:
        print_result(False, "DATABASE_URL is empty - set it in .env or pass --url")
        return 2

    if args.dry_run:
        print_result(True, "dry run: SQL not executed")
        return 0

    ok = database.apply_schema(sql)
    print_result(ok, "schema applied" if ok else "schema application failed")
    if not ok:
        return 1

    status = database.schema_status()
    health = database.health()
    print(f"server version : {health.get('server_version')}")
    print(f"pg_trgm        : {status.get('has_pg_trgm')}")
    print(f"missing tables : {status.get('missing_tables') or 'none'}")
    database.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())

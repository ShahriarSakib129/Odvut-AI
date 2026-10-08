"""Validate the environment, the database and the AI provider.

Usage
-----
    python scripts/check_config.py
    python scripts/check_config.py --online     # also tests Groq + Telegram API

Exit code 0 = ready to deploy, 1 = something must be fixed.
"""

from __future__ import annotations

import argparse
import sys

from _bootstrap import bootstrap, print_header, print_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check INFO GROUP AI BOT configuration")
    parser.add_argument("--online", action="store_true",
                        help="also verify the Groq key and the Telegram token")
    args = parser.parse_args(argv)

    settings, database = bootstrap()
    print_header("INFO GROUP AI BOT - configuration check")
    problems = list(settings.problems)
    warnings = list(settings.warnings)

    for name, value in settings.public_summary().items():
        if name in {"warnings", "problems"}:
            continue
        print(f"  {name:26} = {value}")

    print()
    for problem in problems:
        print_result(False, problem)
    for warning in warnings:
        print(f"[WARN] {warning}")

    # ---- database ------------------------------------------------------
    print()
    print("database checks")
    if settings.database_url:
        initialized = database.init()
        health = database.health()
        print_result(initialized, f"connection ({health.get('latency_ms')} ms, "
                                  f"server {health.get('server_version') or '?'})")
        status = database.schema_status()
        if status["missing_tables"]:
            print_result(False, "missing tables: " + ", ".join(status["missing_tables"]))
            problems.append("database schema not applied (run scripts/apply_schema.py)")
        else:
            print_result(True, "all required tables exist")
        print_result(bool(status["has_pg_trgm"]), "pg_trgm extension (fuzzy search)")
    else:
        print_result(False, "DATABASE_URL missing")

    # ---- ai ------------------------------------------------------------
    print()
    print("AI checks")
    from ai.groq_client import GroqClient

    client = GroqClient(settings)
    print(f"  model                = {settings.groq_model}")
    print(f"  enabled              = {client.enabled}")
    if not client.enabled:
        print_result(False, "GROQ_API_KEY missing or GROQ_ENABLED=false "
                            "(the bot still runs, but without AI answers)")
    elif args.online:
        status = client.health_check(cache_seconds=0)
        print_result(bool(status.get("ok")), f"Groq API ({status.get('error_kind')})")
        if status.get("ok") and status.get("model_available") is False:
            print_result(False, f"model '{settings.groq_model}' is not in the Groq model list")
            problems.append("GROQ_MODEL not available for this account")
    else:
        print("  (use --online to verify the key against api.groq.com)")

    # ---- telegram ------------------------------------------------------
    print()
    print("Telegram checks")
    if not settings.bot_token:
        print_result(False, "BOT_TOKEN missing")
    elif args.online:
        import requests

        try:
            response = requests.get(
                f"https://api.telegram.org/bot{settings.bot_token}/getMe", timeout=15)
            payload = response.json()
            print_result(bool(payload.get("ok")),
                         f"getMe -> @{payload.get('result', {}).get('username')}")
        except Exception as exc:
            print_result(False, f"Telegram API unreachable: {exc}")
    else:
        print_result(True, "BOT_TOKEN present (format looks valid)")
    print_result(bool(settings.webhook_url), f"webhook URL = {settings.webhook_url or 'not set'}")
    print_result(bool(settings.target_admin_ids or settings.target_admin_username),
                 "target admin configured")
    print_result(settings.group_id is not None, f"group id = {settings.group_id}")

    database.close_pool()
    print()
    if problems:
        print_result(False, f"{len(problems)} problem(s) must be fixed before deployment")
        return 1
    print_result(True, "configuration looks good" + (" (with warnings)" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

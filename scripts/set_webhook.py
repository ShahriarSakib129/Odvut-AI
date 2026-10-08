"""Register / inspect / delete the Telegram webhook.

Usage
-----
    python scripts/set_webhook.py --info
    python scripts/set_webhook.py --set
    python scripts/set_webhook.py --set --delete-first
    python scripts/set_webhook.py --delete          # needed before long polling

Requires ``PUBLIC_URL`` (and preferably ``WEBHOOK_SECRET``) in the environment.
"""

from __future__ import annotations

import argparse
import json
import sys

import requests

from _bootstrap import bootstrap, print_header, print_result


def api(bot_token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{bot_token}/{method}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the Telegram webhook")
    parser.add_argument("--set", action="store_true", help="register the webhook")
    parser.add_argument("--delete", action="store_true", help="remove the webhook")
    parser.add_argument("--info", action="store_true", help="show webhook status")
    parser.add_argument("--delete-first", action="store_true",
                        help="call deleteWebhook before setWebhook")
    args = parser.parse_args(argv)

    settings, _database = bootstrap()
    print_header("INFO GROUP AI BOT - webhook")

    if not settings.bot_token:
        print_result(False, "BOT_TOKEN is required")
        return 2
    if not any((args.set, args.delete, args.info)):
        args.info = True

    if args.info:
        try:
            payload = requests.get(api(settings.bot_token, "getWebhookInfo"), timeout=15).json()
        except Exception as exc:
            print_result(False, f"getWebhookInfo failed: {exc}")
            return 1
        result = payload.get("result", {})
        print(json.dumps(result, indent=2, ensure_ascii=False))
        expected = settings.webhook_url
        ok = bool(expected) and result.get("url") == expected
        print_result(ok, f"expected url: {expected or '(PUBLIC_URL not set)'}")
        if result.get("last_error_message"):
            print(f"[WARN] last delivery error: {result['last_error_message']}")

    if args.delete:
        response = requests.post(api(settings.bot_token, "deleteWebhook"),
                                 json={"drop_pending_updates": True}, timeout=15)
        print_result(bool(response.json().get("ok")), "deleteWebhook")
        print("Long polling can now be used: python bot.py")

    if args.set:
        if not settings.webhook_url:
            print_result(False, "PUBLIC_URL is not set - cannot register a webhook")
            return 2
        if args.delete_first:
            requests.post(api(settings.bot_token, "deleteWebhook"), timeout=15)
        body = {
            "url": settings.webhook_url,
            "allowed_updates": ["message"],
            "drop_pending_updates": settings.drop_pending_updates,
            "max_connections": settings.webhook_max_connections,
        }
        if settings.webhook_secret:
            body["secret_token"] = settings.webhook_secret
        else:
            print("[WARN] WEBHOOK_SECRET is empty - the webhook will be unprotected")
        response = requests.post(api(settings.bot_token, "setWebhook"), json=body, timeout=20)
        payload = response.json()
        print_result(bool(payload.get("ok")), f"setWebhook -> {payload.get('description')}")
        if not payload.get("ok"):
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

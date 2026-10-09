"""Publish the bot command list to Telegram (the blue "/" menu).

Usage
-----
    python scripts/set_commands.py
    python scripts/set_commands.py --delete     # clear the menu
"""

from __future__ import annotations

import argparse
import sys

import requests

from _bootstrap import bootstrap, print_header, print_result

PRIVATE_SCOPE = {"type": "all_private_chats"}
GROUP_SCOPE = {"type": "all_group_chats"}

PRIVATE_COMMANDS = [
    {"command": "start", "description": "Bot চালু ও পরিচিতি"},
    {"command": "ask", "description": "Admin-এর তথ্যের ভিত্তিতে প্রশ্নের উত্তর"},
    {"command": "help", "description": "সাহায্য ও কমান্ডের তালিকা"},
    {"command": "memory", "description": "আমি কী মনে রেখেছি"},
    {"command": "status", "description": "Bot ও AI স্ট্যাটাস"},
    {"command": "whoami", "description": "তোমার Telegram id / chat id"},
    {"command": "ping", "description": "Bot alive কিনা পরীক্ষা"},
]

GROUP_COMMANDS = [
    {"command": "start", "description": "Bot চালু ও পরিচিতি"},
    {"command": "ask", "description": "প্রশ্ন করো: /ask BTC এখন কেমন?"},
    {"command": "memory", "description": "Memory সারসংক্ষেপ"},
    {"command": "help", "description": "সাহায্য"},
    {"command": "ping", "description": "Bot alive কিনা পরীক্ষা"},
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish bot commands")
    parser.add_argument("--delete", action="store_true", help="remove all commands")
    args = parser.parse_args(argv)

    settings, _database = bootstrap()
    print_header("INFO GROUP AI BOT - command menu")
    if not settings.bot_token:
        print_result(False, "BOT_TOKEN is required")
        return 2

    base = f"https://api.telegram.org/bot{settings.bot_token}"
    ok = True
    if args.delete:
        response = requests.post(f"{base}/deleteMyCommands", json={}, timeout=15).json()
        ok = bool(response.get("ok"))
        print_result(ok, "deleteMyCommands")
        return 0 if ok else 1

    for scope, commands, label in (
        (PRIVATE_SCOPE, PRIVATE_COMMANDS, "private chats"),
        (GROUP_SCOPE, GROUP_COMMANDS, "group chats"),
    ):
        payload = {"commands": commands, "scope": scope}
        response = requests.post(f"{base}/setMyCommands", json=payload, timeout=15).json()
        ok = ok and bool(response.get("ok"))
        print_result(bool(response.get("ok")), f"{len(commands)} commands for {label}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

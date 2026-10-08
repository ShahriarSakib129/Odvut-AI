#!/usr/bin/env python3
"""Insert demo memory rows so you can test retrieval/AI answers without waiting
for a real conversation. **Testing only - never run it on production data.**

Usage
-----
    python scripts/seed_demo_memory.py --dry-run
    python scripts/seed_demo_memory.py
    python scripts/seed_demo_memory.py --clear-first   # wipe this chat's memory first
"""

from __future__ import annotations

import argparse
import sys

from _bootstrap import bootstrap, print_header

from config import get_settings

DEMO_ADMIN = "demo_admin"

DEMO_PAIRS = [
    ("BTC এখন entry নেওয়া যাবে?",
     "এখনই entry না নেওয়াই ভালো। আগে confirmation আসুক, তারপর ভাববো।",
     "entry_exit"),
    ("ETH কেমন আছে ভাই?",
     "ETH 3100 support ধরে আছে, ভাঙলে structure দুর্বল হবে। ৩১০০ তলানিতে কিনতে চাইলে ধৈর্য লাগবে।",
     "market_structure"),
    ("লিভারেজ কত নেওয়া উচিত?",
     "Leverage ৫x এর বেশি নিও না, risk management সবচেয়ে জরুরি। SL সবসময় সেট করবি।",
     "futures_leverage"),
]

DEMO_ADMIN_MESSAGES = [
    "আমি BTC এর জন্য 62000-60500 zone-এ accumulate করার কথা ভাবছি, তার আগে একটা pullback দরকার।",
    "Market structure break না হওয়া পর্যন্ত long এ যাবো না। ধৈর্য ধরো।",
    "Funding rate positive থাকলে long hold করা risky, খেয়াল রাখবি।",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="INFO GROUP AI BOT - demo memory seeder")
    parser.add_argument("--chat-id", type=int, default=None,
                        help="target chat (default: GROUP_ID from .env)")
    parser.add_argument("--admin-id", type=int, default=None,
                        help="target admin id (default: TARGET_ADMIN_ID from .env)")
    parser.add_argument("--dry-run", action="store_true", help="print, do not insert")
    parser.add_argument("--clear-first", action="store_true",
                        help="delete this chat's existing memory before seeding")
    args = parser.parse_args(argv)

    settings, database = bootstrap()
    print_header("INFO GROUP AI BOT - demo memory seeder")

    chat_id = args.chat_id or settings.group_id
    default_admin = next(iter(sorted(settings.target_admin_ids)), 0)
    admin_id = args.admin_id or default_admin
    if not chat_id:
        print("[FAIL] pass --chat-id or set GROUP_ID in .env")
        return 2
    if not admin_id:
        print("[FAIL] pass --admin-id or set TARGET_ADMIN_ID in .env")
        return 2
    if not database.is_available():
        print("[FAIL] database is not reachable")
        return 2

    print(f"chat_id={chat_id} admin_id={admin_id} dry_run={args.dry_run}")
    if args.clear_first and not args.dry_run:
        removed = database.clear_memory("all", chat_id=chat_id)
        print("cleared:", removed)

    if args.dry_run:
        print(f"would insert {len(DEMO_ADMIN_MESSAGES)} admin messages "
              f"and {len(DEMO_PAIRS)} Q&A pairs")
        return 0

    base_message_id = 900000
    for index, text in enumerate(DEMO_ADMIN_MESSAGES, start=1):
        database.save_admin_message(
            chat_id=chat_id, message_id=base_message_id + index, admin_id=admin_id,
            admin_username=DEMO_ADMIN, text=text,
        )

    for index, (question, answer, _topic) in enumerate(DEMO_PAIRS, start=1):
        question_id = base_message_id + 100 + index
        admin_message_id = base_message_id + 200 + index
        database.save_admin_message(
            chat_id=chat_id, message_id=admin_message_id, admin_id=admin_id,
            admin_username=DEMO_ADMIN, text=answer, is_answer=True,
        )
        database.save_qa_memory(
            chat_id=chat_id, question_message_id=question_id, member_user_id=555000222,
            member_username="demo_member", admin_message_id=admin_message_id,
            admin_id=admin_id, question_text=question, admin_answer_text=answer,
            pair_method="reply",
        )

    print(f"[OK] inserted demo memory: {database.count_admin_messages(chat_id)} admin messages, "
          f"{database.count_qa_pairs(chat_id)} Q&A pairs")
    print("next: ask the bot a question in the group, or run scripts/simulate_conversation.py")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

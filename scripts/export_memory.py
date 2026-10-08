#!/usr/bin/env python3
"""Export what the bot remembers to JSON / CSV / Markdown.

Useful before a ``/clear_memory``, for backups, or to review the memory by hand.

Usage
-----
    python scripts/export_memory.py                       # JSON on stdout
    python scripts/export_memory.py --format md --out memory.md
    python scripts/export_memory.py --format csv --limit 500
    python scripts/export_memory.py --chat-id -1001234567890
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone

from _bootstrap import bootstrap, print_header

from config import get_settings


def _json_default(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _rows(database, chat_id: int | None, limit: int) -> tuple[list[dict], list[dict]]:
    """Newest admin messages + Q&A pairs, for one chat or for all chats."""
    if chat_id is not None:
        return (database.recent_admin_messages(chat_id, limit=limit),
                database.recent_qa_pairs(chat_id, limit=limit))
    return database.export_admin_messages(limit), database.export_qa_pairs(limit)


def _as_csv(admin_rows: list[dict], qa_rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["kind", "chat_id", "message_id", "when", "user", "topic", "text"])
    for row in admin_rows:
        writer.writerow(["admin", row.get("chat_id"), row.get("telegram_message_id"),
                         row.get("message_timestamp"), row.get("admin_username") or row.get("admin_id"),
                         row.get("topic"), (row.get("message_text") or "").replace("\n", " ")])
    for row in qa_rows:
        writer.writerow(["qa_question", row.get("chat_id"), row.get("question_message_id"),
                         row.get("timestamp"), row.get("member_username") or row.get("member_user_id"),
                         row.get("topic"), (row.get("question_text") or "").replace("\n", " ")])
        writer.writerow(["qa_answer", row.get("chat_id"), row.get("admin_message_id"),
                         row.get("timestamp"), row.get("admin_id"), row.get("topic"),
                         (row.get("admin_answer_text") or "").replace("\n", " ")])
    return buffer.getvalue()


def _as_markdown(admin_rows: list[dict], qa_rows: list[dict], chat_id: int | None) -> str:
    lines = ["# INFO GROUP AI BOT - memory export", ""]
    lines.append(f"- chat: `{chat_id if chat_id is not None else 'all chats'}`")
    lines.append(f"- exported: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append(f"- admin messages: {len(admin_rows)} | Q&A pairs: {len(qa_rows)}")
    lines += ["", "## Admin messages", ""]
    for row in admin_rows:
        lines.append(f"- `{row.get('message_timestamp')}` **[{row.get('topic')}]** "
                     f"{row.get('message_text')}")
    lines += ["", "## Q&A memory (member question + target admin answer)", ""]
    for row in qa_rows:
        lines.append(f"### {row.get('timestamp')} - topic `{row.get('topic')}` "
                     f"(pair: {row.get('pair_method')})")
        lines.append(f"- **প্রশ্ন:** {row.get('question_text')}")
        lines.append(f"- **উত্তর:** {row.get('admin_answer_text')}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="INFO GROUP AI BOT - export memory")
    parser.add_argument("--format", choices=("json", "csv", "md"), default="json")
    parser.add_argument("--chat-id", type=int, default=None,
                        help="only this chat (default: every chat in the database)")
    parser.add_argument("--limit", type=int, default=1000, help="max rows per table")
    parser.add_argument("--out", default="", help="write to a file instead of stdout")
    args = parser.parse_args(argv)

    settings, database = bootstrap(quiet=True)
    if args.out:
        print_header("INFO GROUP AI BOT - memory export")

    if not database.is_available():
        print("[FAIL] database is not reachable")
        return 2

    admin_rows, qa_rows = _rows(database, args.chat_id, max(1, args.limit))
    if args.format == "json":
        payload = {
            "service": "INFO GROUP AI BOT",
            "chat_id": args.chat_id,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "admin_messages": admin_rows,
            "qa_memory": qa_rows,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    elif args.format == "csv":
        text = _as_csv(admin_rows, qa_rows)
    else:
        text = _as_markdown(admin_rows, qa_rows, args.chat_id)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"[OK] wrote {len(admin_rows)} admin messages + {len(qa_rows)} Q&A pairs -> {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

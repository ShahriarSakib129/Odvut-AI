"""Offline end-to-end simulation - no Telegram, no Groq, no database required.

It runs a scripted conversation through the real tracker, retrieval engine and
answer pipeline using the in-memory fakes from ``tests/fakes.py``. Use it to
verify that memory collection, Q&A pairing, retrieval and the prompt builder all
behave as expected before deploying.

Usage
-----
    python scripts/simulate_conversation.py
    python scripts/simulate_conversation.py --verbose
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from _bootstrap import PROJECT_ROOT, bootstrap, print_header

CHAT_ID = -1001234567890
ADMIN_ID = 777000111
MEMBER_ID = 555000222


def build_script():
    """(role, text, is_reply_to_previous) conversation."""
    return [
        ("member", "BTC এখন entry নেওয়া যাবে?", False),
        ("admin", "এখনই entry না নেওয়াই ভালো। আগে confirmation আসুক, তারপর ভাববো।", True),
        ("admin", "Leverage 5x এর বেশি নিও না, risk management সবচেয়ে জরুরি।", False),
        ("member", "ETH কেমন আছে?", False),
        ("admin", "ETH 3100 support ধরে আছে, ভাঙলে structure দুর্বল হবে।", True),
        # unanswered question: must never become memory
        ("member", "কেউ কি ADA এর খবর বলবেন?", False),
        ("admin", "আজ আমার বাসায় মেহমান আসবে।", False),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulate a group conversation offline")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print the memory context sent to the model")
    args = parser.parse_args(argv)

    settings, _database = bootstrap(quiet=True)

    sys.path.insert(0, str(PROJECT_ROOT))
    from ai.caching import AnswerCache
    from ai.groq_client import AIResult
    from ai.responder import AnswerService
    from ai.retrieval import RetrievalEngine
    from bot_telegram.message_tracker import MessageTracker
    from tests.fakes import FakeDB, make_message, make_user

    print_header("INFO GROUP AI BOT - offline simulation")

    db = FakeDB()
    tracker = MessageTracker(db, settings)
    retriever = RetrievalEngine(db, settings)
    cache = AnswerCache(settings, db)

    class ScriptedGroq:
        enabled = True
        last_error = ""

        def chat(self, messages, **kwargs):
            system = messages[0]["content"]
            answer = ("মনে রাখার মতো কিছু পাওয়া গেছে। " if "### MEMORY" in system
                      else "কোনো নির্দিষ্ট memory পাওয়া যায়নি। ")
            return AIResult(ok=True, text=answer + "এখন ধৈর্য ধরে confirmation এর জন্য অপেক্ষা করো।",
                            model=settings.groq_model, latency_ms=180, total_tokens=210)

        def health_check(self, **kwargs):
            return {"ok": True}

    groq = ScriptedGroq()
    responder = AnswerService(settings, db, retriever, groq, cache)

    member = make_user(MEMBER_ID, "member_one")
    admin = make_user(ADMIN_ID, "target_admin")
    previous = None
    message_id = 5000

    for role, text, is_reply in build_script():
        message_id += 1
        user = admin if role == "admin" else member
        message = make_message(CHAT_ID, message_id, user, text,
                               reply_to=previous if is_reply else None,
                               timestamp=datetime.now(timezone.utc))
        print(f"\n[{role:6}] {text}")
        if role == "admin":
            result = tracker.track_admin_message(message, user)
            print(f"         -> stored admin message: {result['admin_message_id']}, "
                  f"Q&A: {result['qa_id']} ({result['pair_method']})")
        else:
            result = tracker.track_member_message(message, user)
            print(f"         -> question={result['question']} pending={result['pending_id']}")
        previous = message

    print("\n" + "-" * 72)
    print(f"in-memory DB: {len(db.admin_rows)} admin rows, {len(db.qa_rows)} Q&A rows, "
          f"{len(db.pending)} pending")
    print(f"tracker stats: {tracker.stats}")

    question = "BTC এখন entry নেওয়া যাবে?"
    print(f"\nmember asks: {question}")
    answer, context = responder.answer_with_context(question, chat_id=CHAT_ID, user_id=MEMBER_ID)
    print(f"retrieval   : {context.stats()}")
    print(f"answer      : {answer.text}")
    if args.verbose and context.rendered:
        print("\n--- context sent to the model ---")
        print(context.rendered)

    print("\nNote: this simulation uses in-memory fakes. To test with real data run")
    print("      pytest -q  (with TEST_DATABASE_URL set) and deploy as described in README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

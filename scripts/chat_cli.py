"""Talk to the agent from the terminal (no UI). Each run is one conversation.

Examples:
    python scripts/chat_cli.py --customer CUST0064 -q "Where is my refund for B-1729?" -q "and why 75%?"
    python scripts/chat_cli.py --agent -q "Which hub had the most no-shows?"
    python scripts/chat_cli.py --customer CUST0001          # interactive
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ridewise.agent import Agent  # noqa: E402
from ridewise.tools.base import Session  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--customer", help="customer_id, e.g. CUST0001")
    who.add_argument("--agent", action="store_true")
    p.add_argument("-q", "--question", action="append", help="question (repeat for follow-ups)")
    p.add_argument("--trace", action="store_true", help="print tool arguments")
    a = p.parse_args()

    session = Session("agent") if a.agent else Session("customer", a.customer.upper())
    agent = Agent(session)
    history: list[dict] = []
    questions = a.question or iter(lambda: input("\nyou> "), "exit")
    for q in questions:
        reply = agent.respond(history, q)
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": reply.answer}]
        tools = ", ".join(f"{t['tool']}{'' if t['ok'] else '(ERR)'}" for t in reply.trace) or "none"
        print(f"\n> {q}\n{reply.answer}\n   [tools: {tools} | {reply.seconds}s"
              f"{' | DEGRADED' if reply.degraded else ''}]")
        if a.trace:
            for t in reply.trace:
                print("   ·", t["tool"], json.dumps(t["args"], ensure_ascii=False))
        print("   sources:", "; ".join(reply.sources[:6]))


if __name__ == "__main__":
    main()

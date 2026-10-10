"""Degraded mode: answer without the LLM when it is slow or unavailable.

Uses only deterministic tools and the local policy index, so the assistant stays useful
during an outage. Role scoping is identical because it goes through the same registry.
"""
from __future__ import annotations

import re

from ridewise.rules import load_rules
from ridewise.tools.base import Session
from ridewise.tools.registry import execute

BOOKING_RE = re.compile(r"\bB[-\s]?(\d{1,4})\b", re.IGNORECASE)
REFUND_WORDS = ("refund", "cancel", "money back")
CHARGE_WORDS = ("charge", "fee", "fine", "penalt", "late", "battery")
NOTICE = ("⚠️ The AI assistant is temporarily unavailable, so here is the information straight from "
          "RideWise records.")


def _inr(x) -> str:
    return f"₹{x:,.2f}".replace(".00", "")


def respond(session: Session, message: str) -> tuple[str, list[str], list[dict]]:
    """Return (answer, sources, trace) without calling the LLM."""
    contact = load_rules().terms.support_contact
    support = f"For anything else, contact RideWise Support on {contact['phone']} ({contact['hours']})."
    text = message.lower()
    trace: list[dict] = []

    def call(tool: str, args: dict) -> dict:
        r = execute(session, tool, args)
        trace.append({"tool": tool, "args": args, "ok": r.get("ok"), "result": r})
        return r

    m = BOOKING_RE.search(message)
    if m:
        bid = f"B-{int(m.group(1)):04d}"
        if any(w in text for w in REFUND_WORDS):
            r = call("refund_quote", {"booking_id": bid})
            if r["ok"] and r.get("cancellable") is False:
                body = r["explanation"]
            elif r["ok"]:
                body = (f"**Refund for {bid}** ({r['policy_version']}, {r['policy_doc']}): {r['refund_pct']:g}% → "
                        f"{_inr(r['refund_amount'])} + {_inr(r['refund_gst'])} GST = **{_inr(r['refund_total'])}**.\n"
                        f"Rule: {r['rule']}.")
                if r.get("processing"):
                    body += f"\n{r['processing']}"
            else:
                body = r["error"]
            return f"{NOTICE}\n\n{body}\n\n{support}", r.get("sources", []), trace
        if any(w in text for w in CHARGE_WORDS):
            r = call("explain_charges", {"booking_id": bid})
            if not r["ok"]:
                return f"{NOTICE}\n\n{r['error']}\n\n{support}", [], trace
            late = (r["late_fee"]["expected"] or {}).get("amount", 0)
            batt = (r["battery_fee"]["expected"] or {}).get("amount", 0)
            body = (f"**Charges for {bid}**: rental {_inr(r['rental']['expected']['amount'])} (+GST); "
                    f"late fee {_inr(late)}; low-battery fee {_inr(batt)}; "
                    f"damage charged {_inr(r['damage']['expected_charged'])}. "
                    f"Recorded penalties match policy: {'yes' if r['all_penalties_match'] else 'no, please escalate'}.")
            return f"{NOTICE}\n\n{body}\n\n{support}", r.get("sources", []), trace
        r = call("get_booking_details", {"booking_id": bid})
        if not r["ok"]:
            return f"{NOTICE}\n\n{r['error']}\n\n{support}", [], trace
        b, t = r["booking"], r["payments"]["totals"]
        body = (f"**{bid}**: {b['status']} · {b['vehicle']} at {b['hub']}\n"
                f"- Pickup: {b['scheduled_pickup']}\n- Return: {b['scheduled_return']}\n"
                f"- Rental paid: {_inr(t['rental_paid'])} + {_inr(t['rental_gst_paid'])} GST"
                + (f"\n- Refunded: {_inr(t['refunded'])}" if t["refunded"] else ""))
        return f"{NOTICE}\n\n{body}\n\n{support}", r.get("sources", []), trace

    r = call("search_policy", {"query": message[:300]})
    if r.get("ok") and r["results"]:
        top = r["results"][0]
        body = (f"This policy clause may be relevant ({top['source']}):\n\n{top['text'][:900]}\n\n"
                "Tip: include a booking ID (e.g. B-0001) to see that booking's details, refund or charges.")
        return f"{NOTICE}\n\n{body}\n\n{support}", [top["source"]], trace
    return f"{NOTICE}\n\n{support}", [], trace

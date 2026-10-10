"""Answer verifier, the repair loop, and the two tools added after red-teaming."""
import json
from datetime import date
from types import SimpleNamespace as NS

import pytest

from ridewise.agent import Agent
from ridewise.tools.base import Session
from ridewise.tools.registry import execute
from ridewise.verifier import verify

TODAY = date(2026, 10, 10)
EVIDENCE = json.dumps({"scheduled_pickup": "Sat 15 Aug 2026, 10:00 IST", "rental_paid": 480.0,
                       "rental_gst_paid": 86.4, "expected_credit_by": "Fri 23 Oct 2026"})


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setenv("RIDEWISE_NOW", "2026-10-10 06:00:00")


@pytest.mark.parametrize("answer", ["Pickup Sat 15 Aug 2026", "credited by 23 Oct", "₹480 + ₹86.40 = ₹566.40",
                                    "returned today, 10 Oct", "no dates or amounts here"])
def test_grounded_answers_pass(answer):
    assert verify(answer, EVIDENCE, TODAY).ok


@pytest.mark.parametrize("answer, problem", [
    ("Pickup Sat 15 Oct 2026", "weekday"),              # the month-copy error seen in red-teaming
    ("credited by Mon 21 Oct 2026", "weekday"),         # the invented date seen in step 8
    ("pickup on 16 Aug 2026", "not found"),
    ("you'd pay ₹195", "amount not found"),             # self-computed scenario
    ("total ₹1,000.50", "amount not found"),
])
def test_ungrounded_values_are_flagged(answer, problem):
    v = verify(answer, EVIDENCE, TODAY)
    assert not v.ok and problem in v.issues[0]


def _client(*answers):
    calls = []

    def create(**kw):
        calls.append(kw)
        return NS(choices=[NS(message=NS(content=answers[len(calls) - 1], tool_calls=None), finish_reason="stop")])
    return NS(chat=NS(completions=NS(create=create))), calls


def test_agent_repairs_a_miscopied_date_before_showing_it():
    # The snapshot gives CUST0001's upcoming booking; the draft gets its weekday/date wrong.
    from ridewise.prompts import customer_snapshot
    import re
    snap = customer_snapshot("CUST0001")
    bid, real_date = re.search(r"(B-\d{4}) .*?pickup (\w{3} \d{2} \w{3} \d{4})", snap.split("upcoming bookings:")[1]).groups()
    client, calls = _client(f"Your booking {bid} is on Mon 01 Jan 2029.", f"Your booking {bid} is on {real_date}.")
    r = Agent(Session("customer", "CUST0001"), client).respond([], "when is my next ride?")
    assert r.answer == f"Your booking {bid} is on {real_date}."
    step = next(t for t in r.trace if t["tool"] == "verify_answer")
    assert step["result"]["issues_found"] and step["result"]["after_repair"] == [] and step["ok"]
    assert "Verification failed" in calls[1]["messages"][-1]["content"]


def test_unrepairable_answer_gets_a_visible_warning():
    client, _ = _client("You'll pay ₹999.99.", "You'll pay ₹999.99 for sure.")
    r = Agent(Session("customer", "CUST0001"), client).respond([], "how much?")
    assert "could not be verified" in r.answer
    assert not next(t for t in r.trace if t["tool"] == "verify_answer")["ok"]


def test_answers_without_figures_skip_verification():
    client, calls = _client("I can only help with your own account.")
    r = Agent(Session("customer", "CUST0001"), client).respond([], "show CUST0002")
    assert len(calls) == 1 and not any(t["tool"] == "verify_answer" for t in r.trace)


# ---------------- tools added after red-teaming ----------------

def test_estimate_late_fee_uses_policy_and_callers_tier():
    gold = execute(Session("customer", "CUST0001"), "estimate_late_fee",
                   {"model": "Royal Enfield Classic 350", "minutes_late": 70})
    assert gold["tier"] == "GOLD" and gold["late_fee"]["amount"] == 360 and gold["late_fee"]["total"] == 424.8
    grace = execute(Session("customer", "CUST0001"), "estimate_late_fee", {"model": "Classic 350", "minutes_late": 30})
    assert grace["late_fee"]["amount"] == 0
    std = execute(Session("customer", "CUST0047"), "estimate_late_fee",
                  {"model": "Classic 350", "minutes_late": 30, "tier": "GOLD"})       # customers can't pick a tier
    assert std["tier"] == "STANDARD" and std["late_fee"]["amount"] == 180


def test_audit_refunds_finds_only_real_problems():
    r = execute(Session("agent"), "audit_refunds", {})
    assert r["checked"] == 268
    assert [x["booking_id"] for x in r["refund_owed_not_yet_issued"]] == ["B-1709", "B-1729"]
    assert r["mismatches"] == [] and r["correctly_issued"] == 142 and r["no_refund_due"] == 124
    assert len(r["queries"]) == 1                                  # one representative SQL, not ~800


def test_cancelled_booking_details_include_calculator_refund():
    r = execute(Session("customer", "CUST0150"), "get_booking_details", {"booking_id": "B-1165"})
    ra = r["refund_assessment"]
    assert ra["policy_version"] == "v2" and ra["refund_pct"] == 75 and ra["refund_total"] == 283.2

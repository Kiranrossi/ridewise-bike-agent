"""Policy engine tests: PDF worked examples, boundaries, and golden replays of the real DB."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ridewise.config import settings
from ridewise.db import AgentData
from ridewise.policy_engine import (battery_fee, deposit_for, explain_charges, late_fee,
                                    quote_rental, refund_quote, rental_charge)
from ridewise.rules import load_rules
from ridewise.timeutil import parse_utc

UTC = timezone.utc
NOW = parse_utc("2026-10-10 06:00:00")
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
AGENT = AgentData()


def _ids(where: str) -> list[str]:
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    return [r[0] for r in con.execute(f"SELECT booking_id FROM bookings WHERE {where} ORDER BY booking_id")]


def _details(bid: str):
    d = AGENT.booking_details(bid)
    return d["booking"], d["payment_summary"], d["damage_reports"]


# ---------------- worked examples straight from the PDFs (via YAML) ----------------

@pytest.mark.parametrize("ex", load_rules().pricing.rental_charge.worked_examples)
def test_rental_worked_examples(ex):
    assert rental_charge(ex["hours"], ex["hourly"], ex["daily"]).amount == ex["charge"]


@pytest.mark.parametrize("ex", load_rules().penalties.late_return.worked_examples)
def test_late_fee_worked_examples(ex):
    calc = late_fee(T0, T0 + timedelta(minutes=ex["minutes_late"]), 100, "STANDARD")
    assert calc.amount == ex["fee"]
    assert calc.inputs["hours_charged"] == ex["hours_charged"]


def test_refund_worked_example_v1():
    booking = {"booking_id": "X", "booking_status": "CNL_U", "booked_at": "2026-05-10 12:30:00",
               "scheduled_start": "2026-05-20 03:30:00", "cancelled_at": "2026-05-19 15:30:00"}
    pay = {"rental_paid": 500.0, "rental_gst_paid": 90.0, "rental_pending": 0, "refunded": 250.0}
    q = refund_quote(booking, pay, now=NOW)
    assert q["policy_version"] == "v1" and q["refund_pct"] == 50
    assert q["refund_amount"] == 250.0 and q["refund_gst"] == 45.0


# ---------------- boundaries ----------------

def test_rental_rules():
    assert rental_charge(10, 80, 700).total == 826.0          # 700 + 18% GST
    with pytest.raises(ValueError):
        rental_charge(1, 80, 700)                             # below 2h minimum
    with pytest.raises(ValueError):
        rental_charge(2.5, 80, 700)                           # not whole hours


@pytest.mark.parametrize("tier, minutes, fee", [
    ("GOLD", 30, 0), ("GOLD", 30.5, 150), ("GOLD", 31, 150), ("GOLD", 61, 300),
    ("SILVER", 1, 150), ("STANDARD", 0, 0), ("STANDARD", -10, 0),
])
def test_grace_period(tier, minutes, fee):
    assert late_fee(T0, T0 + timedelta(minutes=minutes), 100, tier).amount == fee


@pytest.mark.parametrize("vtype, pct, fee", [
    ("EV_SCOOTER", 9, 150), ("EV_BIKE", 10, 0), ("EV_SCOOTER", 0, 150),
    ("SCOOTER", 3, 0), ("EV_BIKE", None, 0),
])
def test_battery_boundary(vtype, pct, fee):
    assert battery_fee(vtype, pct).amount == fee


def test_deposits():
    assert deposit_for("BIKE", 2)["amount"] == 1000
    assert deposit_for("EV_BIKE", 2)["amount"] == 1000
    assert deposit_for("SCOOTER", 23)["amount"] == 0
    assert deposit_for("EV_SCOOTER", 24)["amount"] == 500


def test_quote_rental():
    q = quote_rental({"model": "Ather 450X", "vehicle_type": "EV_SCOOTER", "hourly_rate": 80, "daily_rate": 700}, 27)
    assert q["rental"]["amount"] == 940 and q["deposit"]["amount"] == 500
    assert q["payable_at_booking"] == round(940 * 1.18 + 500, 2)


def test_ist_cutoff_bookings():
    b, p, _ = _details("B-1165")                  # booked 30 Jun 20:15 UTC = 1 Jul 01:45 IST
    q = refund_quote(b, p, now=NOW)
    assert q["policy_version"] == "v2" and q["refund_pct"] == 75
    assert q["refund_state"] == "issued"


def test_hypothetical_cancellation_of_upcoming_booking():
    b, p, _ = _details(_ids("booking_status='UPC' AND scheduled_start > '2026-10-20'")[0])
    start = parse_utc(b["scheduled_start"])
    assert refund_quote(b, p, cancel_at=start - timedelta(hours=48), now=NOW)["refund_pct"] in (100,)
    assert refund_quote(b, p, cancel_at=start + timedelta(minutes=1), now=NOW)["cancellable"] is False


def test_what_if_for_already_cancelled_booking():
    b, p, _ = _details("B-1729")                              # actually cancelled 46.5h before → 75%
    start = parse_utc(b["scheduled_start"])
    q = refund_quote(b, p, cancel_at=start - timedelta(hours=72), now=NOW)
    assert q["hypothetical"] and q["refund_pct"] == 100 and q["refund_amount"] == 400.0
    assert "refund_state" not in q and q["status"] == "CNL_U"
    assert refund_quote(b, p, now=NOW)["refund_pct"] == 75   # actual outcome unchanged


def test_refund_quote_includes_schedule_and_credit_date():
    b, p, _ = _details("B-1729")
    q = refund_quote(b, p, now=NOW)
    assert "12h or more but less than 48h: 75%" in q["refund_schedule"]
    assert q["time_before_pickup"] == "46 h 30 min"
    assert q["expected_credit_by"] == "Fri 23 Oct 2026"


def test_completed_booking_not_cancellable():
    b, p, _ = _details(_ids("booking_status='CMP'")[0])
    assert refund_quote(b, p, now=NOW)["cancellable"] is False


def test_refund_not_yet_issued():
    for bid in ("B-1709", "B-1729"):
        b, p, _ = _details(bid)
        q = refund_quote(b, p, now=NOW)
        assert q["policy_version"] == "v2" and q["refund_pct"] == 75
        assert q["refund_state"] == "not_yet_issued" and q["processing_overdue"] is False
    assert q["refund_amount"] == 300.0                       # B-1729: 75% of ₹400 (two instalments)


# ---------------- golden replays of the whole database ----------------

def test_golden_customer_cancellations():
    mismatches = []
    for bid in _ids("booking_status='CNL_U'"):
        b, p, _ = _details(bid)
        q = refund_quote(b, p, now=NOW)
        if q["refund_state"] not in ("issued", "none_due"):
            mismatches.append((bid, q["refund_state"]))
        if q["refund_state"] == "issued":
            assert abs(q["refund_gst"] - p["refund_gst"]) < 0.01, bid
    assert mismatches == [("B-1709", "not_yet_issued"), ("B-1729", "not_yet_issued")]


def test_golden_ridewise_cancellations_full_refund():
    for bid in _ids("booking_status='CNL_S'"):
        b, p, _ = _details(bid)
        q = refund_quote(b, p, now=NOW)
        assert q["refund_pct"] == 100 and q["refund_state"] == "issued", bid


def test_golden_no_shows():
    for bid in _ids("booking_status='NSH'"):
        b, p, _ = _details(bid)
        q = refund_quote(b, p, now=NOW)
        assert q["refund_amount"] == 0 and q["refund_already_issued"] == 0, bid


def test_golden_completed_bookings_all_charges_match():
    bad = []
    for bid in _ids("booking_status='CMP'"):
        b, p, d = _details(bid)
        e = explain_charges(b, p, d, now=NOW)
        if not (e["rental"]["matches"] and e["all_penalties_match"] and e["late_fee"]["matches"]
                and e["battery_fee"]["matches"] and e["damage"]["matches"]):
            bad.append(bid)
    assert bad == []


def test_golden_rental_charge_paid_matches_except_pending():
    bad = []
    for bid in _ids("1=1"):
        b, p, d = _details(bid)
        e = explain_charges(b, p, d, now=NOW)
        if not e["rental"]["matches"] and p["rental_pending"] == 0:
            bad.append(bid)
    assert bad == []

"""Deterministic policy calculators.

Every number comes from rules/*.yaml (policy) or the database (vehicle rates,
timestamps, payments). The LLM never does arithmetic: it calls these functions
and explains the structured result, which includes the rule applied, the
source clause and the inputs used, so every answer can be checked.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from ridewise.rules import load_rules
from ridewise.timeutil import IST, fmt_ist, hours_between, now as _now, parse_utc


def _r(x: float) -> float:
    return round(x + 1e-9, 2)


def _inr(x: float) -> str:
    return f"₹{x:,.2f}".replace(".00", "")


@dataclass
class Calc:
    amount: float
    gst: float
    total: float
    rule: str
    source: str
    steps: list[str] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _gst(amount: float) -> float:
    # Same convention as the payment system: GST computed per transaction, round(amount × rate, 2).
    return round(amount * load_rules().pricing.gst_rate, 2)


# ------------------------------------------------------------------ rental

def rental_charge(hours: int, hourly_rate: float, daily_rate: float) -> Calc:
    """RW-POL-01 §4.1: full 24h blocks at the daily rate, remainder hourly but capped at one daily rate."""
    p = load_rules().pricing
    if hours != int(hours) or hours < p.booking.min_hours:
        raise ValueError(f"Rentals are booked in whole hours with a minimum of {p.booking.min_hours} hours "
                         f"({p.booking.source}).")
    hours = int(hours)
    blocks, rem = divmod(hours, p.rental_charge.block_hours)
    rem_cost = rem * hourly_rate
    capped = p.rental_charge.remainder_capped_at_daily_rate and rem_cost > daily_rate
    amount = blocks * daily_rate + (daily_rate if capped else rem_cost)
    steps = []
    if blocks:
        steps.append(f"{blocks} × 24h at daily rate {_inr(daily_rate)} = {_inr(blocks * daily_rate)}")
    if rem:
        line = f"{rem}h × hourly rate {_inr(hourly_rate)} = {_inr(rem_cost)}"
        if capped:
            line += f", capped at one daily rate {_inr(daily_rate)}"
        steps.append(line)
    gst = _gst(amount)
    steps.append(f"GST {p.gst_rate:.0%} = {_inr(gst)}")
    return Calc(_r(amount), gst, _r(amount + gst), "Daily blocks + hourly remainder (capped at daily rate)",
                f"{p.rental_charge.source}; {p.gst_source}", steps,
                {"hours": hours, "hourly_rate": hourly_rate, "daily_rate": daily_rate})


def deposit_for(vehicle_type: str, hours: int) -> dict:
    """RW-POL-01 §5.1: refundable deposit that MAY be collected (no GST)."""
    d = load_rules().pricing.deposit
    for rule in d.rules:
        if vehicle_type in rule.vehicle_types:
            applies = hours >= rule.min_hours
            return {"amount": rule.amount if applies else 0.0, "applies": applies,
                    "condition": f"{'/'.join(rule.vehicle_types)}: "
                                 + ("any rental" if rule.min_hours == 0 else f"rentals of {rule.min_hours:g}h or more"),
                    "gst": d.gst, "note": "Refundable; may be collected at booking; returned within 3 working "
                                          "days of return via the payment gateway.",
                    "source": d.source}
    return {"amount": 0.0, "applies": False, "source": d.source}


def quote_rental(vehicle: dict, hours: int) -> dict:
    """Full upfront quote for a vehicle row (needs hourly_rate, daily_rate, vehicle_type)."""
    rent = rental_charge(hours, vehicle["hourly_rate"], vehicle["daily_rate"])
    dep = deposit_for(vehicle["vehicle_type"], int(hours))
    return {"vehicle": {k: vehicle.get(k) for k in ("model", "vehicle_type", "hourly_rate", "daily_rate")},
            "rental": rent.to_dict(), "deposit": dep,
            "payable_at_booking": _r(rent.total + dep["amount"])}


# ------------------------------------------------------------------ penalties

def late_fee(scheduled_end: datetime, returned_at: datetime, hourly_rate: float, tier: str) -> Calc:
    """RW-POL-04 §1 + Annexure A: each late hour or part-hour at 1.5 × hourly rate; GOLD has a
    30-minute inclusive grace period, beyond which the full lateness is charged."""
    lr = load_rules().penalties.late_return
    late_s = (returned_at - scheduled_end).total_seconds()
    minutes = late_s / 60
    grace = lr.grace_minutes_by_tier.get(tier, 0)
    inputs = {"scheduled_end_ist": fmt_ist(scheduled_end), "returned_at_ist": fmt_ist(returned_at),
              "minutes_late": round(max(minutes, 0), 1), "hourly_rate": hourly_rate, "tier": tier,
              "grace_minutes": grace}
    if late_s <= 0:
        return Calc(0.0, 0.0, 0.0, "Returned on time", lr.source, ["Not late: no fee"], inputs)
    within_grace = minutes <= grace if lr.grace_inclusive else minutes < grace
    if grace and within_grace:
        return Calc(0.0, 0.0, 0.0, f"{tier} grace period ({grace} min, inclusive)", lr.source,
                    [f"{minutes:.0f} min late is within the {grace}-minute {tier} grace period: no fee"], inputs)
    hours_charged = math.ceil(late_s / 3600)
    per_hour = hourly_rate * lr.rate_multiplier
    amount = hours_charged * per_hour
    gst = _gst(amount)
    steps = [f"{minutes:.0f} min late → {hours_charged} late hour(s) (each hour or part-hour counts)"]
    if grace:
        steps.append(f"Beyond the {grace}-minute {tier} grace period, so the full lateness is charged")
    steps += [f"Late-hour rate: {lr.rate_multiplier:g} × {_inr(hourly_rate)} = {_inr(per_hour)} per late hour",
              f"{hours_charged} × {_inr(per_hour)} = {_inr(amount)}",
              f"GST {load_rules().pricing.gst_rate:.0%} = {_inr(gst)}"]
    return Calc(_r(amount), gst, _r(amount + gst), f"{hours_charged}h × {lr.rate_multiplier:g} × hourly rate",
                lr.source, steps, inputs | {"hours_charged": hours_charged, "rate_per_late_hour": _r(per_hour)})


def battery_fee(vehicle_type: str, return_battery_pct: int | None) -> Calc:
    """RW-POL-04 §2: ₹150 flat if an EV is returned strictly below 10% battery."""
    lb = load_rules().penalties.low_battery
    inputs = {"vehicle_type": vehicle_type, "return_battery_pct": return_battery_pct}
    if vehicle_type not in lb.applies_to or return_battery_pct is None:
        return Calc(0.0, 0.0, 0.0, "Not applicable (non-EV or no battery reading)", lb.source, [], inputs)
    band = next((b.name for b in lb.bands if b.from_pct <= return_battery_pct <= b.to_pct), None)
    if return_battery_pct < lb.fee_if_below_pct:
        gst = _gst(lb.fee)
        return Calc(lb.fee, gst, _r(lb.fee + gst), f"Returned at {return_battery_pct}% (below {lb.fee_if_below_pct:g}%)",
                    lb.source, [f"{return_battery_pct}% is below {lb.fee_if_below_pct:g}% ({band}): flat {_inr(lb.fee)}",
                                f"GST {load_rules().pricing.gst_rate:.0%} = {_inr(gst)}"], inputs)
    return Calc(0.0, 0.0, 0.0, f"Returned at {return_battery_pct}% ({band}): no fee", lb.source,
                [f"{return_battery_pct}% is not below {lb.fee_if_below_pct:g}%: no fee"], inputs)


# ------------------------------------------------------------------ refunds

def add_working_days(dt: datetime, days: int) -> datetime:
    """Add Mon–Fri working days (public holidays are not known to the system)."""
    d = dt.astimezone(IST)
    while days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def _schedule_text(version) -> list[str]:
    slabs, out = version.slabs, []
    for i, s in enumerate(slabs):
        upper = slabs[i - 1].min_hours_before if i else None
        if upper is None:
            band = f"{s.min_hours_before:g}h or more before pickup"
        elif s.min_hours_before == 0:
            band = f"less than {upper:g}h before pickup"
        else:
            band = f"{s.min_hours_before:g}h or more but less than {upper:g}h"
        out.append(f"{band}: {s.refund_pct:g}%")
    return out


def refund_quote(booking: dict, payments: dict, cancel_at: datetime | None = None,
                 now: datetime | None = None) -> dict:
    """Refund owed for a booking.

    * CNL_U / CNL_S / NSH: the actual outcome, compared with refunds already issued.
    * UPC: a hypothetical cancellation at `cancel_at` (default: now).
    * ONG / CMP: not cancellable.
    """
    rules = load_rules()
    rp, common = rules.refund, rules.refund.common
    now = now or _now()
    status = booking["booking_status"]
    booked_at = parse_utc(booking["booked_at"])
    start = parse_utc(booking["scheduled_start"])
    paid, gst_paid = payments["rental_paid"], payments["rental_gst_paid"]
    version = rp.version_for(booked_at)
    base = {"booking_id": booking["booking_id"], "status": status,
            "booked_at_ist": fmt_ist(booked_at), "scheduled_start_ist": fmt_ist(start),
            "policy_version": version.id, "policy_doc": version.doc_id,
            "version_rule": f"Version decided by booking time: booked {fmt_ist(booked_at)}, "
                            f"v2 applies to bookings made on/after 01 Jul 2026 00:00 IST",
            "refund_schedule": _schedule_text(version) + [
                f"Cancelled by RideWise: {common['cancelled_by_ridewise_pct']}% (any timing)",
                f"No-show: {common['no_show_pct']}%"],
            "rental_paid": paid, "rental_gst_paid": gst_paid,
            "rental_pending": payments.get("rental_pending", 0.0),
            "deposit_note": "Security deposit (if any) is returned in full on cancellation via the payment "
                            "gateway; it does not appear in RideWise payment records."}

    if status in ("ONG", "CMP"):
        return base | {"cancellable": False,
                       "explanation": "Cancellation is only possible before the scheduled pickup time; "
                                      "this booking has already started or finished."}

    # "What if I had cancelled at X?" for cancelled / no-show bookings is a hypothetical too.
    what_if = cancel_at is not None and status in ("CNL_U", "NSH")
    if what_if:
        base["actual_status_note"] = (f"Hypothetical: what the refund would have been if cancelled at "
                                      f"{fmt_ist(cancel_at)} (actual status: {status}).")
        status = "UPC"
    if status == "NSH":
        pct, rule, source, when = 0, "No-show: no refund", common["sources"]["no_show"], None
    elif status == "CNL_S":
        pct, rule, source = common["cancelled_by_ridewise_pct"], "Cancelled by RideWise: full refund", \
            common["sources"]["cancelled_by_ridewise"]
        when = parse_utc(booking["cancelled_at"])
    else:
        when = parse_utc(booking["cancelled_at"]) if status == "CNL_U" else (cancel_at or now)
        if status == "UPC" and when >= start:
            return base | {"cancellable": False, "hypothetical": True,
                           "explanation": f"The scheduled pickup ({fmt_ist(start)}) is not after "
                                          f"{fmt_ist(when)}; bookings can only be cancelled before pickup."}
        hours_before = hours_between(when, start)
        slab = version.slab_for(hours_before)
        pct, source = slab.refund_pct, version.source
        rule = (f"{version.id}: cancelled {hours_before:.1f}h before pickup → "
                f"slab '≥{slab.min_hours_before:g}h' = {slab.refund_pct:g}%")
        base["hours_before_pickup"] = round(hours_before, 2)
        h, m = divmod(round(hours_before * 60), 60)
        base["time_before_pickup"] = f"{h} h {m} min"

    refund = _r(paid * pct / 100)
    refund_gst = _gst(refund)       # GST on the refunded amount (= same proportion of GST paid)
    out = base | {"cancellable": True, "hypothetical": status == "UPC",
                  "cancelled_at_ist": fmt_ist(when) if when else None,
                  "refund_pct": pct, "refund_amount": refund, "refund_gst": refund_gst,
                  "refund_total": _r(refund + refund_gst), "rule": rule, "source": source,
                  "steps": [f"Rental charge actually paid (successful payments only): {_inr(paid)} "
                            f"+ GST {_inr(gst_paid)}",
                            f"{pct:g}% × {_inr(paid)} = {_inr(refund)}; GST refunded in proportion: {_inr(refund_gst)}"]}
    if base["rental_pending"]:
        out["steps"].append(f"A further {_inr(base['rental_pending'])} is pending gateway confirmation and is "
                            "not included until it succeeds.")

    if status in ("CNL_U", "CNL_S", "NSH"):
        issued = payments["refunded"]
        out["refund_already_issued"] = issued
        if abs(issued - refund) < 0.01:
            out["refund_state"] = "issued" if refund else "none_due"
        elif issued == 0 and when is not None:
            proc = common["processing"]
            by = add_working_days(when, proc["initiated_within_working_days"])
            credit_max = max(int(n) for n in re.findall(r"\d+", str(proc["credited_within_working_days"])))
            credited_by = add_working_days(by, credit_max)
            out["refund_state"] = "not_yet_issued"
            out["processing"] = (f"Refunds are initiated within {proc['initiated_within_working_days']} working days of "
                                 f"cancellation (by about {by.strftime('%a %d %b %Y')}) and credited to the original "
                                 f"payment method within {proc['credited_within_working_days']} working days after that "
                                 f"(by about {credited_by.strftime('%a %d %b %Y')}). Working days exclude weekends; "
                                 f"public holidays may add delay.")
            out["expected_credit_by"] = credited_by.strftime("%a %d %b %Y")
            out["processing_overdue"] = now.astimezone(IST).date() > by.date()
        else:
            out["refund_state"] = "mismatch"
            out["mismatch_note"] = (f"Recorded refund {_inr(issued)} differs from the policy amount {_inr(refund)}; "
                                    "please escalate to a support agent for review.")
    return out


# ------------------------------------------------------------------ charge audit

def _penalty_kind(remarks: str | None) -> str:
    text = (remarks or "").lower()
    if "battery" in text:
        return "battery"
    if "damage" in text:
        return "damage"
    return "late"


def explain_charges(booking: dict, payments: dict, damage_reports: list[dict],
                    now: datetime | None = None) -> dict:
    """Recompute every charge on a booking from policy and compare with what was recorded."""
    now = now or _now()
    start, end = parse_utc(booking["scheduled_start"]), parse_utc(booking["scheduled_end"])
    hours = round(hours_between(start, end))
    rent = rental_charge(hours, booking["hourly_rate"], booking["daily_rate"])
    out = {"booking_id": booking["booking_id"], "status": booking["booking_status"],
           "rental": {"expected": rent.to_dict(), "paid": payments["rental_paid"],
                      "pending": payments["rental_pending"],
                      "matches": abs(rent.amount - payments["rental_paid"]) < 0.01}}

    returned = parse_utc(booking.get("actual_return_at"))
    if returned:
        late = late_fee(end, returned, booking["hourly_rate"], booking["customer_tier"])
        batt = battery_fee(booking["vehicle_type"], booking.get("return_battery_pct"))
    elif booking["booking_status"] == "ONG" and now > end:
        late = late_fee(end, now, booking["hourly_rate"], booking["customer_tier"])
        late.rule = "ACCRUING (not yet returned): " + late.rule
        batt = None
    else:
        late, batt = None, None

    recorded = {"late": 0.0, "battery": 0.0, "damage": 0.0}
    pending = {"late": 0.0, "battery": 0.0, "damage": 0.0}
    for p in payments["payments"]:
        if p["payment_type"] != "PENALTY":
            continue
        kind = _penalty_kind(p["remarks"])
        if p["payment_status"] == "SUCCESS":
            recorded[kind] = _r(recorded[kind] + p["amount"])
        elif p["payment_status"] == "PENDING":
            pending[kind] = _r(pending[kind] + p["amount"])

    claim_rules = load_rules().penalties.damage.claim_status
    damage_items = [{"report_id": d["report_id"], "category": d["damage_category"],
                     "assessed_cost": d["assessed_cost"], "claim_status": d["claim_status"],
                     "charged": claim_rules[d["claim_status"]].charged,
                     "meaning": claim_rules[d["claim_status"]].meaning} for d in damage_reports]
    damage_expected = _r(sum(d["assessed_cost"] for d in damage_items if d["charged"]))

    def comp(calc: Calc | None, kind: str) -> dict:
        exp = calc.amount if calc else 0.0
        return {"expected": calc.to_dict() if calc else None, "recorded_paid": recorded[kind],
                "recorded_pending": pending[kind],
                "matches": abs(exp - (recorded[kind] + pending[kind])) < 0.01}

    out["late_fee"] = comp(late, "late")
    out["battery_fee"] = comp(batt, "battery")
    out["damage"] = {"reports": damage_items, "expected_charged": damage_expected,
                     "recorded_paid": recorded["damage"], "recorded_pending": pending["damage"],
                     "matches": abs(damage_expected - recorded["damage"] - pending["damage"]) < 0.01,
                     "source": load_rules().penalties.damage.source}
    expected_total = _r((late.amount if late else 0) + (batt.amount if batt else 0) + damage_expected)
    out["penalties_expected_total"] = expected_total
    out["penalties_recorded_total"] = _r(sum(recorded.values()) + sum(pending.values()))
    out["all_penalties_match"] = abs(expected_total - out["penalties_recorded_total"]) < 0.01
    if late and late.rule.startswith("ACCRUING"):
        out["note"] = "The vehicle has not been returned yet; the late fee shown is accruing and will change."
    return out

"""Tools available to both roles. Booking tools read through the role's data scope:
customers via CustomerData (own bookings only), agents via AgentData."""
from __future__ import annotations

import difflib

from pydantic import Field

from ridewise import db
from ridewise.policy_engine import explain_charges, late_fee, quote_rental, refund_quote
from ridewise.retrieval import search_policy
from ridewise.rules import load_rules
from ridewise.tools.base import (Args, BookingIdArgs, Session, ToolError, format_booking,
                                 format_payments, parse_user_time)
from ridewise.timeutil import fmt_ist, parse_utc


def _scope(session: Session):
    return db.CustomerData(session.customer_id) if session.role == "customer" else db.AgentData()


def _details_or_fail(session: Session, booking_id: str) -> dict:
    details = _scope(session).booking_details(booking_id)
    if details is None:
        where = "on your account" if session.role == "customer" else "in the system"
        raise ToolError(f"No booking {booking_id} was found {where}.")
    return details


# ------------------------------------------------------------------ search_policy

class SearchPolicyArgs(Args):
    query: str = Field(min_length=2, max_length=300,
                       description="The question rephrased in RideWise policy terms "
                                   "(e.g. 'no-show refund', 'late return grace period GOLD').")
    doc_id: str | None = Field(None, description="Optional: restrict to one document, e.g. RW-POL-03.")


def t_search_policy(session: Session, a: SearchPolicyArgs) -> dict:
    doc_ids = [a.doc_id.upper()] if a.doc_id else None
    hits = search_policy(a.query, k=5, doc_ids=doc_ids)
    if not hits:
        raise ToolError("No matching policy text found.")
    return {"results": [{k: h[k] for k in ("source", "applies_to", "text")} for h in hits],
            "sources": [h["source"] for h in hits[:3]]}


# ------------------------------------------------------------------ get_vehicle_catalog

class CatalogArgs(Args):
    hub: str | None = Field(None, description="Hub name (aliases like 'EC' or 'HSR' are fine).")
    vehicle_type: str | None = Field(None, description="BIKE, SCOOTER, EV_SCOOTER or EV_BIKE (or plain words).")


def t_get_vehicle_catalog(session: Session, a: CatalogArgs) -> dict:
    aliases = load_rules().aliases
    if a.hub and aliases.hub(a.hub) is None:
        raise ToolError(f"Unknown hub '{a.hub}'. RideWise hubs: {', '.join(aliases.hubs)}.")
    if a.vehicle_type and aliases.vehicle_type(a.vehicle_type) is None:
        raise ToolError(f"Unknown vehicle type '{a.vehicle_type}'. Types: {', '.join(aliases.vehicle_types)}.")
    internal = session.role == "agent"
    rows = db.vehicle_catalog(a.hub, a.vehicle_type, internal=internal)
    if internal:
        rows = [{k: r[k] for k in ("vehicle_id", "registration_no", "model", "vehicle_type", "hub_name",
                                   "hourly_rate", "daily_rate", "battery_capacity_kwh", "status")}
                for r in rows]
    return {"vehicles": rows, "count": len(rows),
            "note": None if internal else "Active vehicles only; availability for specific times is "
                                          "shown in the RideWise app.",
            "sources": ["vehicles table"]}


# ------------------------------------------------------------------ quote_rental

class QuoteArgs(Args):
    model: str = Field(description="Vehicle model name, e.g. 'Ather 450X' (partial names are fine).")
    hours: int = Field(description="Rental duration in whole hours (minimum 2).")


def _match_model(name: str) -> dict:
    catalog = db.vehicle_catalog()
    by_name = {r["model"].lower(): r for r in catalog}
    q = name.strip().lower()
    hits = [r for m, r in by_name.items() if q in m] or \
           [by_name[m] for m in difflib.get_close_matches(q, by_name, n=1, cutoff=0.6)]
    if len(hits) != 1:
        options = ", ".join(sorted(r["model"] for r in (hits or catalog)))
        raise ToolError(f"Please specify the model. {'Matches' if hits else 'Available models'}: {options}.")
    return hits[0]


def t_quote_rental(session: Session, a: QuoteArgs) -> dict:
    vehicle = _match_model(a.model)
    try:
        q = quote_rental(vehicle, a.hours)
    except ValueError as e:
        raise ToolError(str(e)) from e
    return q | {"sources": [q["rental"]["source"], q["deposit"]["source"], "vehicles table (rates)"]}


# ------------------------------------------------------------------ get_booking_details

def t_get_booking_details(session: Session, a: BookingIdArgs) -> dict:
    d = _details_or_fail(session, a.booking_id)
    pay = format_payments(d["payment_summary"])
    claim = load_rules().penalties.damage.claim_status
    gst_rate = load_rules().pricing.gst_rate
    damage = [{k: r[k] for k in ("report_id", "damage_category", "severity", "description",
                                 "assessed_cost", "claim_status")}
              | {"reported_at": fmt_ist(parse_utc(r["reported_at"])),
                 "customer_charged": claim[r["claim_status"]].meaning,
                 "gst_if_charged": round(r["assessed_cost"] * gst_rate, 2)}
              for r in d["damage_reports"]]
    return {"booking": format_booking(d["booking"], session.role), "payments": pay,
            "damage_reports": damage,
            "sources": [f"booking {a.booking_id}"] + [p["payment_id"] for p in pay["payments"]]
                       + [r["report_id"] for r in damage]}


# ------------------------------------------------------------------ refund_quote

class RefundArgs(BookingIdArgs):
    cancel_at: str | None = Field(None, description="Hypothetical cancellation time in IST 'YYYY-MM-DD HH:MM': "
                                                    "'if I cancel at …' for an upcoming booking, or 'what if I had "
                                                    "cancelled at …' for a cancelled/no-show booking. Omit for the "
                                                    "actual outcome (or 'now' for an upcoming booking).")


def t_refund_quote(session: Session, a: RefundArgs) -> dict:
    d = _details_or_fail(session, a.booking_id)
    now = session.now()
    q = refund_quote(d["booking"], d["payment_summary"], cancel_at=parse_user_time(a.cancel_at, now), now=now)
    q["status"] = load_rules().aliases.status_label(q["status"], session.role)
    srcs = [f"booking {a.booking_id}"] + ([q["source"]] if q.get("source") else []) + [
        p["payment_id"] for p in d["payment_summary"]["payments"]
        if p["payment_type"] in ("BOOKING", "REFUND") and p["payment_status"] == "SUCCESS"]
    return q | {"sources": srcs}


# ------------------------------------------------------------------ explain_charges

class ExplainArgs(BookingIdArgs):
    assume_return_at: str | None = Field(None, description="For an ongoing/upcoming booking: hypothetical return "
                                                           "time in IST 'YYYY-MM-DD HH:MM' to estimate a late fee.")


def t_explain_charges(session: Session, a: ExplainArgs) -> dict:
    d = _details_or_fail(session, a.booking_id)
    b = d["booking"]
    out = explain_charges(b, d["payment_summary"], d["damage_reports"], now=session.now())
    if a.assume_return_at:
        if b["booking_status"] not in ("UPC", "ONG"):
            raise ToolError("A hypothetical return time only applies to upcoming or ongoing bookings.")
        at = parse_user_time(a.assume_return_at, session.now())
        out["late_fee_estimate"] = late_fee(parse_utc(b["scheduled_end"]), at, b["hourly_rate"],
                                            b["customer_tier"]).to_dict()
    srcs = [f"booking {a.booking_id}", load_rules().penalties.late_return.source,
            load_rules().penalties.low_battery.source]
    srcs += [p["payment_id"] for p in d["payment_summary"]["payments"] if p["payment_type"] == "PENALTY"]
    return out | {"sources": srcs}

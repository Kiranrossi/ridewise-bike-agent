"""Shared plumbing for tools: session context, errors, argument normalisation, formatting."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from ridewise.rules import load_rules
from ridewise.timeutil import IST, UTC, fmt_ist, hours_between, now as _now, parse_utc


@dataclass
class Session:
    """Who is asking. Built by the app from the login; never from LLM output."""
    role: str                                  # "customer" | "agent"
    customer_id: str | None = None
    session_id: str = "local"
    extra: dict = field(default_factory=dict)

    def now(self) -> datetime:
        return _now()


class ToolError(Exception):
    """A user-facing failure (not found, invalid input, not permitted)."""


class Args(BaseModel):
    # Unknown arguments (e.g. an LLM-invented 'customer_id') are silently dropped.
    model_config = ConfigDict(extra="ignore")


def normalise_booking_id(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits or len(digits) > 4:
        raise ValueError("Booking IDs look like B-0001.")
    return f"B-{int(digits):04d}"


def normalise_customer_id(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits or len(digits) > 4:
        raise ValueError("Customer IDs look like CUST0001.")
    return f"CUST{int(digits):04d}"


class BookingIdArgs(Args):
    booking_id: str

    @field_validator("booking_id")
    @classmethod
    def _bid(cls, v: str) -> str:
        return normalise_booking_id(v)


def parse_user_time(value: str | None, now: datetime) -> datetime | None:
    """Parse a time given by the LLM. Naive times are interpreted as IST."""
    if value is None or not str(value).strip():
        return None
    v = str(value).strip()
    if v.lower() == "now":
        return now
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00").replace(" IST", ""))
    except ValueError as e:
        raise ToolError(f"Could not read the time '{v}'. Use 'YYYY-MM-DD HH:MM' (IST).") from e
    return (dt.replace(tzinfo=IST) if dt.tzinfo is None else dt).astimezone(UTC)


def format_booking(b: dict, role: str) -> dict:
    """Booking for display: IST times, plain-language status, duration."""
    aliases = load_rules().aliases
    start, end = parse_utc(b["scheduled_start"]), parse_utc(b["scheduled_end"])
    out = {
        "booking_id": b["booking_id"],
        "status": aliases.status_label(b["booking_status"], role),
        "vehicle": f"{b['model']} ({b['vehicle_type'].replace('_', ' ').title()}, {b['registration_no']})",
        "hub": b["hub_name"],
        "booked_at": fmt_ist(parse_utc(b["booked_at"])),
        "scheduled_pickup": fmt_ist(start),
        "scheduled_return": fmt_ist(end),
        "duration_hours": round(hours_between(start, end)),
        "actual_return": fmt_ist(parse_utc(b["actual_return_at"])),
        "cancelled_at": fmt_ist(parse_utc(b["cancelled_at"])),
        "return_battery_pct": b["return_battery_pct"],
        "rates": {"hourly": b["hourly_rate"], "daily": b["daily_rate"]},
    }
    if role == "agent":
        out |= {"customer_id": b["customer_id"], "customer_tier": b["customer_tier"],
                "status_code": b["booking_status"], "vehicle_id": b["vehicle_id"],
                "scheduled_pickup_utc": b["scheduled_start"], "scheduled_return_utc": b["scheduled_end"]}
    return {k: v for k, v in out.items() if v is not None}


def format_payments(summary: dict) -> dict:
    aliases = load_rules().aliases
    rows = [{
        "payment_id": p["payment_id"],
        "type": aliases.payment_type.get(p["payment_type"], p["payment_type"]),
        "amount": p["amount"], "gst": p["gst_amount"],
        "status": aliases.payment_status.get(p["payment_status"], p["payment_status"]),
        "method": p["payment_method"],
        "time": fmt_ist(parse_utc(p["paid_at"])),
        "remarks": p["remarks"],
    } for p in summary["payments"]]
    totals = {k: v for k, v in summary.items() if k not in ("payments", "failed_attempts")}
    notes = []
    if summary["failed_attempts"]:
        notes.append(f"Failed attempts {', '.join(summary['failed_attempts'])} were not charged.")
    if summary["rental_pending"]:
        notes.append("Part of the rental payment is pending gateway confirmation.")
    notes.append("Security deposit returns are processed by the payment gateway and are not shown in "
                 "RideWise records (RW-POL-01 §5.3).")
    return {"payments": rows, "totals": totals, "notes": notes}

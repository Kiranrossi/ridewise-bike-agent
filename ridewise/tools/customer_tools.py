"""Customer-only tools. None of them takes a customer_id: it comes from the session."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from ridewise import db
from ridewise.rules import load_rules
from ridewise.tools.base import Args, Session, format_booking

STATUS_GROUPS = {
    "upcoming": ["UPC"], "ongoing": ["ONG"], "completed": ["CMP"],
    "cancelled": ["CNL_U", "CNL_S"], "no_show": ["NSH"], "all": None,
}


class NoArgs(Args):
    pass


def t_get_my_profile(session: Session, a: NoArgs) -> dict:
    rules = load_rules()
    me = db.CustomerData(session.customer_id)
    p = me.profile()
    counts: dict[str, int] = {}
    for b in me.bookings(limit=1000):
        label = rules.aliases.status_label(b["booking_status"], "customer")
        counts[label] = counts.get(label, 0) + 1
    grace = rules.penalties.late_return.grace_minutes_by_tier.get(p["customer_tier"], 0)
    return {
        "profile": p,
        "kyc_meaning": rules.terms.kyc[p["kyc_status"]]["meaning"],
        "tier_benefits": (f"{grace}-minute grace period on late returns" if grace
                          else "No late-return grace period") + ". " + rules.terms.tiers["documented_benefits"],
        "booking_counts": counts,
        "sources": ["customers table", rules.terms.kyc["source"], rules.penalties.late_return.source],
    }


class ListMyBookingsArgs(Args):
    status: Literal["upcoming", "ongoing", "completed", "cancelled", "no_show", "all"] = "all"
    limit: int = Field(10, ge=1, le=50)


def t_list_my_bookings(session: Session, a: ListMyBookingsArgs) -> dict:
    rows = db.CustomerData(session.customer_id).bookings(STATUS_GROUPS[a.status], limit=a.limit)
    return {"bookings": [format_booking(b, "customer") for b in rows], "count": len(rows),
            "note": "Newest first by scheduled pickup." + (" More bookings exist; increase limit."
                                                            if len(rows) == a.limit else ""),
            "sources": [f"booking {b['booking_id']}" for b in rows]}

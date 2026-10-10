"""Tool registry: which tools exist, which role may use them, and safe execution.

Two layers of access control:
  1. tools_for(role) only ever exposes the role's allowed tools to the LLM.
  2. execute() re-checks the permission before running anything.
Allowed tools per role come from rules/access_policy.yaml.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from pydantic import ValidationError

from ridewise.config import ROOT
from ridewise.db import record_queries
from ridewise.rules import load_rules
from ridewise.tools import agent_tools as ag
from ridewise.tools import customer_tools as cu
from ridewise.tools import shared as sh
from ridewise.tools.base import Args, BookingIdArgs, Session, ToolError

log = logging.getLogger(__name__)
AUDIT_LOG = ROOT / "logs" / "audit.jsonl"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str | Callable[[], str]
    args: type[Args]
    handler: Callable[[Session, Args], dict]

    def schema(self) -> dict:
        desc = self.description() if callable(self.description) else self.description
        params = self.args.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": desc, "parameters": params}}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("search_policy",
         "Search RideWise policy documents (rental terms, cancellation/refund v1 & v2, penalties, EV guide). "
         "Use for rules, definitions and procedures. Rephrase the user's words into policy terms first "
         "(e.g. 'don't turn up' → 'no-show refund'). Returns cited clauses.",
         sh.SearchPolicyArgs, sh.t_search_policy),
    Tool("get_vehicle_catalog",
         "List RideWise vehicle models with hourly/daily rates and hubs, optionally filtered by hub or type.",
         sh.CatalogArgs, sh.t_get_vehicle_catalog),
    Tool("quote_rental",
         "Price a rental before booking: rental charge (daily blocks + capped hourly remainder), GST and any "
         "security deposit, computed from policy. Use for 'how much would X cost for N hours'.",
         sh.QuoteArgs, sh.t_quote_rental),
    Tool("get_booking_details",
         "Full details of one booking: vehicle, times (IST), status, payments (successful/failed/pending), "
         "refunds and damage claims.",
         BookingIdArgs, sh.t_get_booking_details),
    Tool("refund_quote",
         "Refund for a booking computed from the correct refund policy version: the actual refund for "
         "cancelled/no-show bookings (and whether it has been issued), or a hypothetical refund if an upcoming "
         "booking were cancelled now or at cancel_at.",
         sh.RefundArgs, sh.t_refund_quote),
    Tool("explain_charges",
         "Recompute and explain every charge on a booking (rental, late fee, low-battery fee, damage) and compare "
         "with what was recorded. Optionally estimate a late fee for a hypothetical return time.",
         sh.ExplainArgs, sh.t_explain_charges),
    Tool("estimate_late_fee",
         "Calculate a hypothetical late-return fee from policy: '1 hour 10 minutes late on a Royal Enfield', "
         "'what if I'm 40 minutes late with booking B-xxxx'. Applies part-hour rounding, 1.5× rate, tier grace "
         "and GST. Use instead of computing late fees yourself.",
         sh.LateFeeArgs, sh.t_estimate_late_fee),
    Tool("get_my_profile",
         "The logged-in customer's own profile: tier and its benefits, KYC status and meaning, booking counts.",
         cu.NoArgs, cu.t_get_my_profile),
    Tool("list_my_bookings",
         "The logged-in customer's own bookings, newest first, optionally filtered by status.",
         cu.ListMyBookingsArgs, cu.t_list_my_bookings),
    Tool("find_customer",
         "Find customers by customer ID, full phone number, email or name. Returns masked candidates; if more "
         "than one matches, ask which one.",
         ag.FindCustomerArgs, ag.t_find_customer),
    Tool("get_customer",
         "One customer's profile (contact details masked), tier, KYC status, booking counts and recent bookings.",
         ag.CustomerIdArgs, ag.t_get_customer),
    Tool("list_damage_reports",
         "Damage reports/claims filtered by report ID, booking, customer, claim status, category, severity or hub.",
         ag.DamageArgs, ag.t_list_damage_reports),
    Tool("audit_refunds",
         "Find cancelled/no-show bookings whose refund is owed but not yet issued, or whose recorded refund "
         "differs from policy. Recomputes every refund with the policy calculator. ALWAYS use this (not SQL) for "
         "'which refunds are pending/missing/wrong' — a booking with no refund row is often correctly owed 0%.",
         ag.AuditArgs, ag.t_audit_refunds),
    Tool("run_readonly_sql", ag.sql_tool_description, ag.SqlArgs, ag.t_run_readonly_sql),
]}


def _validate_registry() -> None:
    for role in load_rules().access.roles.values():
        unknown = set(role.tools) - set(TOOLS)
        if unknown:
            raise RuntimeError(f"access_policy.yaml references unknown tools: {sorted(unknown)}")


_validate_registry()


def allowed(role: str) -> list[str]:
    return load_rules().access.tools_for(role)


def tools_for(role: str) -> list[dict]:
    """OpenAI tool schemas for the role. Tools outside the role are never shown to the LLM."""
    return [TOOLS[name].schema() for name in allowed(role)]


def _audit(session: Session, name: str, args: dict, ok: bool, ms: int, error: str | None) -> None:
    try:
        AUDIT_LOG.parent.mkdir(exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "session": session.session_id,
                 "role": session.role, "tool": name, "arg_keys": sorted(args), "ok": ok, "ms": ms,
                 "error": error}
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        log.warning("could not write audit log")


def execute(session: Session, name: str, arguments: str | dict | None) -> dict:
    """Run a tool call from the LLM. Always returns a JSON-serialisable dict, never raises."""
    t0 = time.monotonic()
    args: dict = {}
    queries: list[dict] = []
    try:
        if name not in TOOLS or name not in allowed(session.role):
            raise ToolError(f"The tool '{name}' is not available.")
        if isinstance(arguments, str):
            args = json.loads(arguments or "{}")
        else:
            args = dict(arguments or {})
        tool = TOOLS[name]
        with record_queries() as queries:
            result = tool.handler(session, tool.args.model_validate(args))
        # 'queries' = exact SQL run (parameters inlined) for the UI; stripped before reaching the LLM.
        out = {"ok": True, **result, "queries": queries}
        _audit(session, name, args, True, int((time.monotonic() - t0) * 1000), None)
        return out
    except ValidationError as e:
        msg = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        err = f"Invalid arguments: {msg}"
    except json.JSONDecodeError:
        err = "Invalid arguments: not valid JSON."
    except (ToolError, PermissionError) as e:
        err = str(e)
    except Exception:
        log.exception("tool %s failed", name)
        err = "The tool failed unexpectedly. Please try again or rephrase."
    _audit(session, name, args, False, int((time.monotonic() - t0) * 1000), err)
    return {"ok": False, "error": err, "queries": queries}

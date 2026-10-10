"""Safe, read-only access to ridewise.db.

Two paths:
  * Fixed, parameterised queries (CustomerData / AgentData) used by the tools.
    CustomerData is bound to one customer_id at construction and every query
    filters on it, so a customer can never read another customer's records.
  * Guarded free-form SQL (run_readonly_sql), agents only, for analytics. A
    SQLite authorizer denies anything but reads and returns PII columns as NULL.
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from typing import Any

from ridewise.config import settings
from ridewise.privacy import mask_customer, wrap_untrusted
from ridewise.rules import load_rules

CUSTOMER_ID_RE = re.compile(r"^CUST\d{4}$")
BOOKING_ID_RE = re.compile(r"^B-\d{4}$")

BOOKING_SELECT = """
    SELECT b.booking_id, b.customer_id, b.vehicle_id, b.booked_at, b.scheduled_start,
           b.scheduled_end, b.actual_return_at, b.cancelled_at, b.booking_status,
           b.return_battery_pct, v.model, v.vehicle_type, v.hub_name, v.registration_no,
           v.hourly_rate, v.daily_rate, c.customer_tier
    FROM bookings b
    JOIN vehicles v ON v.vehicle_id = b.vehicle_id
    JOIN customers c ON c.customer_id = b.customer_id
"""


class SqlRejected(Exception):
    """Raised when a free-form SQL query is not allowed."""


def connect() -> sqlite3.Connection:
    """Read-only connection: opened with mode=ro and query_only, so writes are impossible."""
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with closing(connect()) as con:
        return [dict(r) for r in con.execute(sql, params)]


def _one(sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------- shared queries

def customer_exists(customer_id: str) -> bool:
    if not CUSTOMER_ID_RE.match(customer_id or ""):
        return False
    return _one("SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)) is not None


def customer_phone_matches(customer_id: str, phone: str) -> bool:
    row = _one("SELECT phone FROM customers WHERE customer_id = ?", (customer_id,))
    return bool(row) and re.sub(r"\D", "", phone or "")[-10:] == row["phone"]


def payment_summary(booking_id: str) -> dict:
    """Payments for one booking with totals that follow RideWise payment semantics:
    only SUCCESS rows count as money moved; FAILED attempts are never charged;
    refunds are stored as negative amounts; instalments are summed."""
    rows = _rows(
        """SELECT payment_id, payment_type, amount, gst_amount, payment_method,
                  payment_status, paid_at, remarks
           FROM payments WHERE booking_id = ? ORDER BY paid_at""",
        (booking_id,),
    )

    def total(ptype: str, status: str, field: str = "amount") -> float:
        return round(sum(r[field] for r in rows if r["payment_type"] == ptype and r["payment_status"] == status), 2)

    for r in rows:
        r["remarks"] = wrap_untrusted(r["remarks"]) if r["remarks"] else None
    return {
        "payments": rows,
        "rental_paid": total("BOOKING", "SUCCESS"),
        "rental_gst_paid": total("BOOKING", "SUCCESS", "gst_amount"),
        "rental_pending": total("BOOKING", "PENDING"),
        "refunded": -total("REFUND", "SUCCESS"),
        "refund_gst": -total("REFUND", "SUCCESS", "gst_amount"),
        "penalties_paid": total("PENALTY", "SUCCESS"),
        "penalties_gst_paid": total("PENALTY", "SUCCESS", "gst_amount"),
        "penalties_pending": total("PENALTY", "PENDING"),
        "deposit_paid": total("DEPOSIT", "SUCCESS"),
        "deposit_pending": total("DEPOSIT", "PENDING"),
        "failed_attempts": [r["payment_id"] for r in rows if r["payment_status"] == "FAILED"],
    }


def damage_for_booking(booking_id: str) -> list[dict]:
    rows = _rows(
        """SELECT report_id, booking_id, vehicle_id, reported_by, damage_category, severity,
                  description, assessed_cost, claim_status, reported_at
           FROM damage_reports WHERE booking_id = ? ORDER BY reported_at""",
        (booking_id,),
    )
    for r in rows:
        r["description"] = wrap_untrusted(r["description"])
    return rows


def vehicle_catalog(hub: str | None = None, vehicle_type: str | None = None,
                    internal: bool = False) -> list[dict]:
    """Customers: active models with rates and the hubs they're at.
    Agents (internal=True): individual vehicles including status and registration."""
    aliases = load_rules().aliases
    where, params = [], []
    if hub:
        canonical = aliases.hub(hub)
        if canonical is None:
            return []
        where.append("hub_name = ?")
        params.append(canonical)
    if vehicle_type:
        canonical = aliases.vehicle_type(vehicle_type)
        if canonical is None:
            return []
        where.append("vehicle_type = ?")
        params.append(canonical)
    if internal:
        sql = "SELECT * FROM vehicles"
        sql += (" WHERE " + " AND ".join(where)) if where else ""
        return _rows(sql + " ORDER BY hub_name, vehicle_id", tuple(params))
    where.append("status = 'ACTIVE'")
    sql = f"""SELECT model, vehicle_type, hourly_rate, daily_rate, battery_capacity_kwh,
                     group_concat(DISTINCT hub_name) AS hubs
              FROM vehicles WHERE {' AND '.join(where)}
              GROUP BY model, vehicle_type, hourly_rate, daily_rate, battery_capacity_kwh
              ORDER BY vehicle_type, hourly_rate"""
    return _rows(sql, tuple(params))


def _booking_details(booking: dict | None) -> dict | None:
    if booking is None:
        return None
    return {
        "booking": booking,
        "payment_summary": payment_summary(booking["booking_id"]),
        "damage_reports": damage_for_booking(booking["booking_id"]),
    }


# ---------------------------------------------------------------- customer scope

class CustomerData:
    """Data access bound to one customer. customer_id comes from the login session,
    never from the LLM, and is part of every WHERE clause."""

    def __init__(self, customer_id: str):
        if not customer_exists(customer_id):
            raise PermissionError("unknown customer")
        self.customer_id = customer_id

    def profile(self) -> dict:
        row = _one("SELECT * FROM customers WHERE customer_id = ?", (self.customer_id,))
        out = mask_customer(row, "self")
        out["city"] = load_rules().aliases.city(row["city"]) or row["city"]
        return out

    def bookings(self, statuses: list[str] | None = None, limit: int = 50) -> list[dict]:
        sql = BOOKING_SELECT + " WHERE b.customer_id = ?"
        params: list[Any] = [self.customer_id]
        if statuses:
            sql += f" AND b.booking_status IN ({','.join('?' * len(statuses))})"
            params += statuses
        sql += " ORDER BY b.scheduled_start DESC LIMIT ?"
        return _rows(sql, (*params, limit))

    def booking(self, booking_id: str) -> dict | None:
        """None both when the booking doesn't exist and when it belongs to someone else."""
        return _one(BOOKING_SELECT + " WHERE b.booking_id = ? AND b.customer_id = ?",
                    (booking_id, self.customer_id))

    def booking_details(self, booking_id: str) -> dict | None:
        return _booking_details(self.booking(booking_id))


# ---------------------------------------------------------------- agent scope

class AgentData:
    """Data access for support staff: all operational records, PII masked."""

    def booking(self, booking_id: str) -> dict | None:
        return _one(BOOKING_SELECT + " WHERE b.booking_id = ?", (booking_id,))

    def booking_details(self, booking_id: str) -> dict | None:
        return _booking_details(self.booking(booking_id))

    def customer(self, customer_id: str) -> dict | None:
        row = _one("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
        if row is None:
            return None
        out = mask_customer(row, "agent_single")
        out["city"] = load_rules().aliases.city(row["city"]) or row["city"]
        out["booking_counts"] = {
            r["booking_status"]: r["n"] for r in _rows(
                "SELECT booking_status, count(*) AS n FROM bookings WHERE customer_id = ? GROUP BY 1",
                (customer_id,))
        }
        return out

    def customer_bookings(self, customer_id: str, statuses: list[str] | None = None,
                          limit: int = 50) -> list[dict]:
        sql = BOOKING_SELECT + " WHERE b.customer_id = ?"
        params: list[Any] = [customer_id]
        if statuses:
            sql += f" AND b.booking_status IN ({','.join('?' * len(statuses))})"
            params += statuses
        return _rows(sql + " ORDER BY b.scheduled_start DESC LIMIT ?", (*params, limit))

    def find_customers(self, query: str, limit: int = 10) -> list[dict]:
        """Exact match on ID / email / phone, partial match on name. Returns masked candidates."""
        q = (query or "").strip()
        if not q:
            return []
        digits = re.sub(r"\D", "", q)
        if CUSTOMER_ID_RE.match(q.upper()):
            where, params = "customer_id = ?", (q.upper(),)
        elif "@" in q:
            where, params = "lower(email) = lower(?)", (q,)
        elif len(digits) >= 10:
            where, params = "phone = ?", (digits[-10:],)
        else:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where, params = "full_name LIKE ? ESCAPE '\\'", (f"%{escaped}%",)
        rows = _rows(
            f"""SELECT customer_id, full_name, phone, email, city, kyc_status, customer_tier, created_at
                FROM customers WHERE {where} ORDER BY customer_id LIMIT ?""",
            (*params, limit),
        )
        aliases = load_rules().aliases
        out = []
        for r in rows:
            m = mask_customer(r, "agent_list")
            m["city"] = aliases.city(r["city"]) or r["city"]
            out.append(m)
        return out

    def damage_reports(self, claim_status: str | None = None, category: str | None = None,
                       severity: str | None = None, hub: str | None = None,
                       customer_id: str | None = None, report_id: str | None = None,
                       booking_id: str | None = None, limit: int = 50) -> list[dict]:
        where, params = [], []
        for col, val in (("d.claim_status", claim_status), ("d.damage_category", category),
                         ("d.severity", severity), ("b.customer_id", customer_id),
                         ("d.report_id", report_id), ("d.booking_id", booking_id)):
            if val:
                where.append(f"{col} = ?")
                params.append(val.upper())
        if hub:
            canonical = load_rules().aliases.hub(hub)
            if canonical is None:
                return []
            where.append("v.hub_name = ?")
            params.append(canonical)
        sql = """SELECT d.report_id, d.booking_id, b.customer_id, d.vehicle_id, v.model, v.hub_name,
                        d.reported_by, d.damage_category, d.severity, d.description,
                        d.assessed_cost, d.claim_status, d.reported_at
                 FROM damage_reports d
                 JOIN bookings b ON b.booking_id = d.booking_id
                 JOIN vehicles v ON v.vehicle_id = d.vehicle_id"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = _rows(sql + " ORDER BY d.reported_at DESC LIMIT ?", (*params, limit))
        for r in rows:
            r["description"] = wrap_untrusted(r["description"])
        return rows

    def vehicle(self, ref: str) -> dict | None:
        return _one("SELECT * FROM vehicles WHERE vehicle_id = ? OR registration_no = ?",
                    ((ref or "").upper(), (ref or "").upper()))


# ---------------------------------------------------------------- guarded SQL (agents)

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    sqlite3.SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}
_LEADING_COMMENTS_RE = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*", re.DOTALL)


def _authorizer(allowed_tables: set[str], blocked: dict[str, set[str]]):
    def check(action, arg1, arg2, db_name, trigger):
        if action not in _ALLOWED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            table, column = (arg1 or "").lower(), (arg2 or "").lower()
            if table not in allowed_tables:
                return sqlite3.SQLITE_DENY
            if column in blocked.get(table, set()):
                return sqlite3.SQLITE_IGNORE        # column reads as NULL
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return check


def run_readonly_sql(sql: str) -> dict:
    """Execute one SELECT for a support agent, with PII columns returned as NULL."""
    rules = load_rules()
    policy = rules.access.sql
    body = _LEADING_COMMENTS_RE.sub("", sql or "", count=1).strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", body, re.IGNORECASE):
        raise SqlRejected("Only a single SELECT (or WITH … SELECT) statement is allowed.")
    # Multiple statements are refused by sqlite3 itself ("one statement at a time"),
    # and the authorizer denies any non-read action even if one slipped through.

    blocked = {t: {c.lower() for c in cols} for t, cols in policy.blocked_columns.items()}
    blocked_names = set().union(*blocked.values())
    with closing(connect()) as con:
        con.create_function("norm_city", 1, lambda v: rules.aliases.city(v) or (v or "").strip(),
                            deterministic=True)
        con.create_function("norm_hub", 1, lambda v: rules.aliases.hub(v) or (v or "").strip(),
                            deterministic=True)
        con.set_authorizer(_authorizer({t.lower() for t in policy.allowed_tables}, blocked))
        deadline = time.monotonic() + policy.timeout_seconds
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cur = con.execute(body)
            rows = cur.fetchmany(policy.max_rows + 1)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "interrupted" in msg:
                raise SqlRejected(f"Query exceeded the {policy.timeout_seconds:g}s time limit.") from e
            if "not authorized" in msg or "prohibited" in msg:
                raise SqlRejected("Query uses an operation or table that is not permitted.") from e
            raise SqlRejected(f"SQL error: {msg}") from e
        except sqlite3.ProgrammingError as e:
            raise SqlRejected(f"SQL error: {e}") from e

    columns = [d[0] for d in cur.description]
    restricted = [c for c in columns if c.lower() in blocked_names]
    data = [[("[restricted]" if (v is None and columns[i] in restricted) else v)
             for i, v in enumerate(r)] for r in rows[: policy.max_rows]]
    return {
        "sql": body,
        "columns": columns,
        "rows": data,
        "row_count": len(data),
        "truncated": len(rows) > policy.max_rows,
        "note": "Personal-data and free-text columns are not available through SQL and read as NULL."
                if restricted else None,
    }

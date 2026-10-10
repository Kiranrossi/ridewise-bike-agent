"""Tool layer: role exposure, permission re-checks, ownership, argument handling, outputs."""
import json

import pytest

from ridewise.tools.base import Session
from ridewise.tools.registry import TOOLS, execute, tools_for

AGENT_ONLY = {"find_customer", "get_customer", "list_damage_reports", "run_readonly_sql"}
CUSTOMER_ONLY = {"get_my_profile", "list_my_bookings"}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setenv("RIDEWISE_NOW", "2026-10-10 06:00:00")


def names(role):
    return {t["function"]["name"] for t in tools_for(role)}


def test_role_exposure():
    assert not names("customer") & AGENT_ONLY
    assert not names("agent") & CUSTOMER_ONLY
    assert names("customer") | names("agent") == set(TOOLS)


def test_schemas_are_valid_openai_tools():
    for t in tools_for("agent") + tools_for("customer"):
        json.dumps(t)
        assert t["type"] == "function" and t["function"]["parameters"]["type"] == "object"
    sql = next(t for t in tools_for("agent") if t["function"]["name"] == "run_readonly_sql")
    assert "phone [restricted]" in sql["function"]["description"]
    assert "norm_city" in sql["function"]["description"]


@pytest.mark.parametrize("tool", sorted(AGENT_ONLY))
def test_customer_cannot_call_agent_tools_even_if_requested(tool):
    r = execute(Session("customer", "CUST0001"), tool, {"sql": "select 1", "query": "x", "customer_id": "CUST0002"})
    assert r == {"ok": False, "error": f"The tool '{tool}' is not available."}


def test_unknown_tool():
    assert execute(Session("agent"), "delete_everything", {})["ok"] is False


def test_foreign_booking_not_found_for_customer():
    for tool in ("get_booking_details", "refund_quote", "explain_charges"):
        r = execute(Session("customer", "CUST0001"), tool, {"booking_id": "B-0412"})
        assert r == {"ok": False, "error": "No booking B-0412 was found on your account."}


def test_agent_can_open_any_booking():
    r = execute(Session("agent"), "get_booking_details", {"booking_id": "B-0412"})
    assert r["ok"] and r["booking"]["customer_id"] == "CUST0013"


def test_injected_customer_id_argument_is_ignored():
    r = execute(Session("customer", "CUST0106"), "list_my_bookings",
                {"status": "all", "limit": 50, "customer_id": "CUST0001"})
    assert r["ok"] and r["count"] > 0
    own = execute(Session("agent"), "get_customer", {"customer_id": "CUST0106"})
    own_ids = {b["booking_id"] for b in own["recent_bookings"]}
    assert own_ids <= {b["booking_id"] for b in r["bookings"]}


@pytest.mark.parametrize("raw", ["B-1729", "b1729", "1729", "B 1729"])
def test_booking_id_normalisation(raw):
    r = execute(Session("customer", "CUST0064"), "refund_quote", {"booking_id": raw})
    assert r["ok"] and r["booking_id"] == "B-1729"


def test_refund_quote_output():
    r = execute(Session("customer", "CUST0064"), "refund_quote", {"booking_id": "B-1729"})
    assert r["refund_pct"] == 75 and r["refund_total"] == 354.0 and r["refund_state"] == "not_yet_issued"
    assert r["status"] == "Cancelled by you"
    assert any("RW-POL-03" in s for s in r["sources"])


def test_hypothetical_cancellation_with_ist_time():
    s = Session("agent")
    upcoming = execute(s, "run_readonly_sql", {"sql": "SELECT booking_id FROM bookings WHERE booking_status='UPC' "
                                                      "AND scheduled_start > '2026-10-25' ORDER BY 1 LIMIT 1"})
    bid = upcoming["rows"][0][0]
    r = execute(s, "refund_quote", {"booking_id": bid, "cancel_at": "2026-10-11 10:00"})
    assert r["ok"] and r["hypothetical"] and r["refund_pct"] == 100
    bad = execute(s, "refund_quote", {"booking_id": bid, "cancel_at": "next tuesday"})
    assert bad["ok"] is False and "YYYY-MM-DD" in bad["error"]


def test_quote_rental_and_validation():
    s = Session("customer", "CUST0001")
    assert execute(s, "quote_rental", {"model": "ather", "hours": 27})["rental"]["amount"] == 940
    assert "minimum of 2 hours" in execute(s, "quote_rental", {"model": "ather", "hours": 1})["error"]
    assert "specify the model" in execute(s, "quote_rental", {"model": "honda", "hours": 4})["error"]
    assert execute(s, "quote_rental", {"model": "ather"})["error"].startswith("Invalid arguments")


def test_catalog_views_differ_by_role():
    pub = execute(Session("customer", "CUST0001"), "get_vehicle_catalog", {"hub": "EC"})
    internal = execute(Session("agent"), "get_vehicle_catalog", {"hub": "EC"})
    assert "registration_no" not in pub["vehicles"][0] and "registration_no" in internal["vehicles"][0]
    assert "Unknown hub" in execute(Session("agent"), "get_vehicle_catalog", {"hub": "Mars"})["error"]


def test_customer_status_labels_and_profile():
    p = execute(Session("customer", "CUST0001"), "get_my_profile", {})
    assert p["profile"]["customer_tier"] == "GOLD" and "30-minute grace" in p["tier_benefits"]
    assert "date_of_birth" not in p["profile"]


def test_agent_tools():
    a = Session("agent")
    found = execute(a, "find_customer", {"query": "Anil Naidu"})
    assert found["count"] == 2 and found["note"]
    c = execute(a, "get_customer", {"customer_id": "cust0001"})
    assert c["customer"]["phone"] == "******2345"
    d = execute(a, "list_damage_reports", {"report_id": "DMG0009"})
    assert d["reports"][0]["description"].startswith("<untrusted_data>")
    q = execute(a, "run_readonly_sql", {"sql": "DELETE FROM bookings"})
    assert q["ok"] is False


def test_explain_charges_with_hypothetical_return():
    a = Session("agent")
    ong = execute(a, "run_readonly_sql", {"sql": "SELECT booking_id, scheduled_end FROM bookings "
                                                 "WHERE booking_status='ONG' LIMIT 1"})["rows"][0]
    r = execute(a, "explain_charges", {"booking_id": ong[0], "assume_return_at": "2026-12-31 23:00"})
    assert r["ok"] and r["late_fee_estimate"]["amount"] > 0


def test_audit_log_has_no_argument_values(tmp_path, monkeypatch):
    import ridewise.tools.registry as reg
    monkeypatch.setattr(reg, "AUDIT_LOG", tmp_path / "audit.jsonl")
    execute(Session("agent"), "find_customer", {"query": "9845012345"})
    line = (tmp_path / "audit.jsonl").read_text()
    assert "9845012345" not in line and '"arg_keys": ["query"]' in line

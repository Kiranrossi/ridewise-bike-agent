"""Every SQL shown in the UI must reproduce the same rows in a plain SQLite client (e.g. DB Browser):
no app-registered functions, no authorizer, just the database file."""
import sqlite3

import pytest

from ridewise.config import settings
from ridewise.db import inline_sql, portable_sql
from ridewise.tools.base import Session
from ridewise.tools.registry import execute


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setenv("RIDEWISE_NOW", "2026-10-10 06:00:00")


def plain_sqlite(sql: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)   # what DB Browser does
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


CASES = [
    (Session("customer", "CUST0001"), "get_booking_details", {"booking_id": "B-0001"}),
    (Session("customer", "CUST0064"), "refund_quote", {"booking_id": "B-1729"}),
    (Session("customer", "CUST0106"), "list_my_bookings", {"status": "completed", "limit": 5}),
    (Session("customer", "CUST0001"), "get_my_profile", {}),
    (Session("customer", "CUST0001"), "get_vehicle_catalog", {"hub": "EC"}),
    (Session("agent"), "explain_charges", {"booking_id": "B-0003"}),
    (Session("agent"), "find_customer", {"query": "D'Souza"}),
    (Session("agent"), "get_customer", {"customer_id": "CUST0013"}),
    (Session("agent"), "list_damage_reports", {"claim_status": "DISPUTED", "hub": "Whitefield"}),
]


@pytest.mark.parametrize("session, tool, args", CASES, ids=[c[1] for c in CASES])
def test_shown_sql_reproduces_row_counts_in_plain_sqlite(session, tool, args):
    r = execute(session, tool, args)
    assert r["ok"] and r["queries"], r
    for q in r["queries"]:
        assert "?" not in q["sql"]
        assert len(plain_sqlite(q["sql"])) == q["rows"], q["sql"]


def test_foreign_booking_query_returns_zero_rows_in_plain_sqlite():
    r = execute(Session("customer", "CUST0001"), "get_booking_details", {"booking_id": "B-0412"})
    assert r["ok"] is False
    assert "customer_id = 'CUST0001'" in r["queries"][0]["sql"]
    assert plain_sqlite(r["queries"][0]["sql"]) == []


def test_agent_sql_with_app_functions_runs_unchanged_elsewhere():
    sql = ("SELECT norm_city(city) AS city, count(*) AS n FROM customers "
           "GROUP BY 1 ORDER BY 2 DESC, 1")
    r = execute(Session("agent"), "run_readonly_sql", {"sql": sql})
    shown = r["queries"][0]["sql"]
    assert "norm_city" not in shown
    assert plain_sqlite(shown) == [tuple(row) for row in r["rows"]]
    assert r["rows"][0] == ["Bengaluru", 164]


def test_payment_totals_come_from_reproducible_sql():
    r = execute(Session("customer", "CUST0064"), "get_booking_details", {"booking_id": "B-1729"})
    totals_sql = next(q["sql"] for q in r["queries"] if "GROUP BY payment_type" in q["sql"])
    rows = plain_sqlite(totals_sql)
    assert ("BOOKING", "SUCCESS", 2, 400.0, 72.0) in rows
    assert r["payments"]["totals"]["rental_paid"] == 400.0


def test_inline_sql_quotes_safely():
    assert inline_sql("SELECT ? , ?, ?", ("D'Souza", 5, None)) == "SELECT 'D''Souza' , 5, NULL;"
    assert portable_sql("select norm_hub(hub_name) from vehicles").count("CASE") == 1


def test_queries_are_never_sent_to_the_model():
    from types import SimpleNamespace as NS
    import json
    from ridewise.agent import Agent

    sent = []

    def create(**kw):
        sent.append(kw)
        if len(sent) == 1:
            tc = NS(id="c1", function=NS(name="get_booking_details", arguments=json.dumps({"booking_id": "B-0001"})))
            return NS(choices=[NS(message=NS(content="", tool_calls=[tc]), finish_reason="tool_calls")])
        return NS(choices=[NS(message=NS(content="done", tool_calls=None), finish_reason="stop")])

    reply = Agent(Session("customer", "CUST0001"), NS(chat=NS(completions=NS(create=create)))).respond([], "B-0001?")
    tool_msg = next(m for m in sent[1]["messages"] if m["role"] == "tool")
    assert "queries" not in json.loads(tool_msg["content"])
    assert reply.trace[0]["result"]["queries"]                 # but the UI still gets them

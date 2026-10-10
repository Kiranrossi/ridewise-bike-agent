import hashlib

import pytest

from ridewise.config import settings
from ridewise.db import (AgentData, CustomerData, SqlRejected, customer_exists,
                         customer_phone_matches, run_readonly_sql)


def _sha256() -> str:
    return hashlib.sha256(settings.db_path.read_bytes()).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def db_is_never_modified():
    before = _sha256()
    yield
    assert _sha256() == before, "ridewise.db was modified!"


# ---------------- customer isolation ----------------

def test_customer_sees_own_booking():
    me = CustomerData("CUST0013")
    assert me.booking("B-0412")["customer_id"] == "CUST0013"


def test_foreign_booking_looks_like_missing_booking():
    me = CustomerData("CUST0001")
    assert me.booking("B-0412") is None          # belongs to CUST0013
    assert me.booking("B-9999") is None          # does not exist
    assert me.booking_details("B-0412") is None


def test_customer_bookings_are_all_own():
    rows = CustomerData("CUST0106").bookings(limit=500)
    assert rows and {r["customer_id"] for r in rows} == {"CUST0106"}


@pytest.mark.parametrize("evil", ["B-0001' OR '1'='1", "B-0001; DROP TABLE bookings", "' OR 1=1 --"])
def test_injection_in_parameters_is_inert(evil):
    assert CustomerData("CUST0001").booking(evil) is None
    assert AgentData().booking(evil) is None


def test_unknown_customer_rejected():
    with pytest.raises(PermissionError):
        CustomerData("CUST9999")
    assert not customer_exists("CUST0001' OR '1'='1")


def test_phone_login_check():
    assert customer_phone_matches("CUST0001", "9845012345")
    assert customer_phone_matches("CUST0001", "+91 98450 12345")
    assert not customer_phone_matches("CUST0001", "9999999999")


def test_customer_profile_hides_dob_and_masks_licence():
    p = CustomerData("CUST0001").profile()
    assert "date_of_birth" not in p
    assert "*" in p["driving_license_no"]


# ---------------- agent fixed queries ----------------

def test_agent_find_customer_masks_pii():
    rows = AgentData().find_customers("Anil Naidu")
    assert len(rows) == 2                         # duplicate names → caller must disambiguate
    for r in rows:
        assert "*" in r["phone"] and "***@" in r["email"]
        assert "driving_license_no" not in r and "date_of_birth" not in r


def test_agent_find_customer_by_phone_and_apostrophe_name():
    assert AgentData().find_customers("9845012345")[0]["customer_id"] == "CUST0001"
    assert len(AgentData().find_customers("D'Souza")) >= 2


def test_agent_single_customer_shows_last4_only():
    c = AgentData().customer("CUST0001")
    assert c["phone"] == "******2345"
    assert c["city"] == "Bengaluru"


def test_damage_descriptions_are_wrapped_untrusted():
    for r in AgentData().damage_reports(limit=100):
        assert r["description"].startswith("<untrusted_data>")


# ---------------- payment semantics ----------------

def test_instalments_are_summed():
    s = AgentData().booking_details("B-1729")["payment_summary"]
    assert s["rental_paid"] == 400.0 and s["refunded"] == 0


def test_failed_attempt_not_counted():
    s = AgentData().booking_details("B-1709")["payment_summary"]
    assert s["rental_paid"] == 300.0
    assert len(s["failed_attempts"]) == 1


def test_pending_payment_not_counted_as_paid():
    s = AgentData().booking_details("B-1700")["payment_summary"]
    assert s["rental_paid"] == 0 and s["rental_pending"] == 1000.0


# ---------------- guarded SQL ----------------

@pytest.mark.parametrize("sql", [
    "SELECT phone, email FROM customers",
    "SELECT phone AS x FROM customers",
    "SELECT (SELECT email FROM customers LIMIT 1) AS e",
    "WITH t AS (SELECT phone p FROM customers) SELECT p FROM t",
    "SELECT substr(phone, 1, 10) FROM customers",
    "SELECT group_concat(email) FROM customers",
    "SELECT driving_license_no, date_of_birth FROM customers",
    "SELECT remarks FROM payments",
    "SELECT description FROM damage_reports",
])
def test_pii_reads_as_null(sql):
    r = run_readonly_sql(sql)
    flat = [v for row in r["rows"] for v in row]
    assert all(v in (None, "[restricted]") for v in flat), flat[:5]


def test_select_star_restricts_only_pii_columns():
    r = run_readonly_sql("SELECT * FROM customers")
    pii = {"phone", "email", "driving_license_no", "date_of_birth"}
    for row in r["rows"]:
        for col, val in zip(r["columns"], row):
            if col in pii:
                assert val == "[restricted]"
    assert r["rows"][0][r["columns"].index("full_name")] == "Aarav Sharma"


def test_pii_cannot_be_used_to_filter():
    r = run_readonly_sql("SELECT customer_id FROM customers WHERE phone = '9845012345'")
    assert r["rows"] == []


@pytest.mark.parametrize("sql", [
    "DELETE FROM bookings",
    "UPDATE customers SET kyc_status = 'VERIFIED'",
    "DROP TABLE bookings",
    "INSERT INTO payments VALUES (1)",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA table_info(customers)",
    "SELECT 1; DROP TABLE bookings",
    "WITH x AS (SELECT 1) DELETE FROM bookings",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM pragma_table_info('customers')",
    "SELECT load_extension('evil')",
    "",
])
def test_writes_and_meta_access_rejected(sql):
    with pytest.raises(SqlRejected):
        run_readonly_sql(sql)


def test_row_limit_and_truncation():
    r = run_readonly_sql("SELECT payment_id FROM payments")
    assert r["row_count"] == 200 and r["truncated"] is True


def test_runaway_query_times_out():
    with pytest.raises(SqlRejected, match="time limit"):
        run_readonly_sql("SELECT count(*) FROM payments a, payments b, payments c")


def test_city_normalisation_function():
    r = run_readonly_sql("SELECT norm_city(city), count(*) FROM customers GROUP BY 1 ORDER BY 2 DESC")
    assert r["rows"][0] == ["Bengaluru", 164]


def test_analytics_query_works():
    r = run_readonly_sql("""
        SELECT v.hub_name, round(sum(p.amount), 2) AS revenue
        FROM payments p JOIN bookings b ON b.booking_id = p.booking_id
        JOIN vehicles v ON v.vehicle_id = b.vehicle_id
        WHERE p.payment_type = 'BOOKING' AND p.payment_status = 'SUCCESS'
        GROUP BY 1 ORDER BY 2 DESC""")
    assert len(r["rows"]) == 7

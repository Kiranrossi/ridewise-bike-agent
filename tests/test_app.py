"""Auth checks and UI flows via Streamlit's AppTest (no LLM calls are made)."""
import pytest
from streamlit.testing.v1 import AppTest

from ridewise import auth
from ridewise.config import ROOT


def test_customer_login_demo_mode():
    assert auth.login_customer("cust1").user_id == "CUST0001"
    assert auth.login_customer("CUST9999").error == auth.GENERIC_FAILURE
    for junk in ("' OR 1=1 --", "abc12", "B-0011", "CUST00011"):
        assert auth.login_customer(junk).ok is False, junk


def test_customer_login_strict_mode(monkeypatch):
    monkeypatch.setattr(auth, "settings", type("S", (), {"auth_mode": "strict"})())
    assert auth.login_customer("CUST0001", "9845012345").ok
    assert auth.login_customer("CUST0001", "0000000000").error == auth.GENERIC_FAILURE
    assert auth.login_customer("CUST0001").ok is False


def test_staff_login_uses_hashes():
    assert auth.login_staff("agent01", "RideWise@2026").role == "agent"
    assert auth.login_staff("AGENT01", "RideWise@2026").ok
    assert auth.login_staff("agent01", "wrong").error == auth.GENERIC_FAILURE
    assert auth.login_staff("ghost", "RideWise@2026").error == auth.GENERIC_FAILURE
    assert "RideWise@2026" not in auth.STAFF_FILE.read_text()


@pytest.fixture
def app():
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()


def test_login_page_defaults_to_customer(app):
    assert not app.exception
    assert [t.label for t in app.text_input] == ["Customer ID"]
    assert app.button[0].label == "Support staff sign-in"


def test_customer_flow_new_chat_and_sign_out(app):
    app.text_input[0].input("CUST0001")
    app.button[1].click().run()
    assert not app.exception and app.session_state.user["id"] == "CUST0001"
    assert app.session_state.agent.session.customer_id == "CUST0001"
    app.session_state.messages = [{"role": "user", "content": "hi"}]
    next(b for b in app.sidebar.button if b.label == "New chat").click().run()
    assert app.session_state.messages == []
    next(b for b in app.sidebar.button if b.label == "Sign out").click().run()
    assert app.session_state.user is None and [t.label for t in app.text_input] == ["Customer ID"]


def test_staff_flow(app):
    app.button[0].click().run()
    app.text_input[0].input("agent01")
    app.text_input[1].input("RideWise@2026")
    app.button[1].click().run()
    assert not app.exception
    assert app.session_state.user["role"] == "agent"
    assert app.session_state.agent.session.customer_id is None


def test_login_lockout_after_repeated_failures(app):
    for _ in range(auth.MAX_ATTEMPTS):
        app.text_input[0].input("CUST9999")
        app.button[1].click().run()
    app.text_input[0].input("CUST0001")
    app.button[1].click().run()
    assert app.session_state.user is None
    assert any("Too many attempts" in e.value for e in app.error)

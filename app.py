"""RideWise Assist — Streamlit chat UI.

Run: streamlit run app.py
"""
from __future__ import annotations

import html
import re
import json
import time
import uuid

import streamlit as st

from ridewise import auth
from ridewise.agent import Agent
from ridewise.config import settings
from ridewise.db import CustomerData
from ridewise.sources import label as cite_label
from ridewise.tools.base import Session

st.set_page_config(page_title="RideWise Assist", page_icon="🛵", layout="wide",
                   initial_sidebar_state="expanded")

LOGO = """<svg width="34" height="34" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
<circle cx="12" cy="32" r="8" stroke="#0E7C6B" stroke-width="3.2"/><circle cx="36" cy="32" r="8" stroke="#0E7C6B" stroke-width="3.2"/>
<path d="M12 32 L20 18 H30 L36 32 M20 18 L26 32 H12 M28 12 H33 L30 18" stroke="#1F3864" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>"""

CSS = """
<style>
#MainMenu, footer, [data-testid="stDecoration"] {visibility: hidden;}
header[data-testid="stHeader"] {background: transparent;}
.block-container {padding-top: 1.4rem; padding-bottom: 6rem; max-width: 980px;}
[data-testid="stSidebar"] {background: #F3F7F6; border-right: 1px solid #E2EAE8;}
.rw-brand {display:flex; align-items:center; gap:.6rem;}
.rw-brand .name {font-size:1.35rem; font-weight:700; color:#1F3864; letter-spacing:-.01em;}
.rw-brand .name span {color:#0E7C6B;}
.rw-brand .tag {font-size:.78rem; color:#5B6B7A; margin-top:-.15rem;}
.rw-hero {text-align:center; margin: 2.2rem 0 1.2rem;}
.rw-hero h1 {font-size:2rem; color:#1F3864; margin-bottom:.3rem; letter-spacing:-.02em;}
.rw-hero p {color:#5B6B7A; font-size:1rem;}
.rw-card {background:#FFFFFF; border:1px solid #E2EAE8; border-radius:16px; padding:1.4rem 1.5rem;
          box-shadow:0 6px 24px rgba(16,40,60,.06);}
.rw-badge {display:inline-block; padding:.12rem .55rem; border-radius:999px; font-size:.72rem;
           font-weight:600; letter-spacing:.02em; margin-right:.3rem;}
.rw-badge.role {background:#E6F4F1; color:#0E7C6B;}
.rw-badge.agent {background:#E8EDF7; color:#1F3864;}
.rw-badge.gold {background:#FFF4D6; color:#8A6100;}
.rw-badge.silver {background:#EEF1F4; color:#4A5563;}
.rw-badge.standard {background:#F1F5F9; color:#475569;}
.rw-badge.ok {background:#E7F6EC; color:#1E7B3A;}
.rw-badge.warn {background:#FFF1E6; color:#B4530B;}
.rw-badge.bad {background:#FDECEC; color:#B42318;}
.rw-identity {background:#FFFFFF; border:1px solid #E2EAE8; border-radius:14px; padding:.9rem 1rem; margin-bottom:.8rem;}
.rw-identity .who {font-weight:650; color:#1B2430; font-size:1rem;}
.rw-identity .sub {color:#5B6B7A; font-size:.8rem; margin:.1rem 0 .45rem;}
.rw-muted {color:#5B6B7A; font-size:.8rem;}
.rw-welcome {background:linear-gradient(135deg,#E6F4F1 0%,#F3F7F6 60%); border:1px solid #D7EBE6;
             border-radius:16px; padding:1.3rem 1.4rem; margin:.4rem 0 1rem;}
.rw-welcome h3 {margin:0 0 .25rem; color:#1F3864; font-size:1.2rem;}
.rw-welcome p {margin:0; color:#3D4B59; font-size:.92rem;}
.rw-meta {color:#6B7A88; font-size:.75rem; margin-top:.15rem;}
.rw-step {border-left:3px solid #0E7C6B; padding:.15rem 0 .15rem .7rem; margin:.45rem 0;}
.rw-step.err {border-left-color:#B42318;}
.rw-step .t {font-weight:600; font-size:.86rem; color:#1B2430;}
.rw-step .d {font-size:.8rem; color:#5B6B7A;}
div[data-testid="stChatMessage"] {border-radius:14px;}
.stButton > button {border-radius:10px;}
div[data-testid="stForm"] {border:none; padding:0;}
</style>
"""

CUSTOMER_SUGGESTIONS = [
    ("📅", "What's the status of my upcoming booking?"),
    ("💸", "If I cancel my upcoming booking now, how much will I get back?"),
    ("⏱️", "Why was I charged a late fee on my last ride?"),
    ("🔋", "What happens if I return an EV below 10% battery?"),
    ("🛵", "How much would an Ather 450X cost for 27 hours?"),
    ("⭐", "What benefits does my tier give me?"),
]
AGENT_SUGGESTIONS = [
    ("📊", "Which hub had the most no-shows, and what's the no-show rule?"),
    ("🧾", "Is the refund on booking B-1165 correct?"),
    ("🔎", "Find customer Anil Naidu"),
    ("🛠️", "Show open or disputed damage claims at Whitefield"),
    ("💰", "Revenue by hub for September 2026 (successful rental payments)"),
    ("⏳", "Which customers have refunds that are not yet issued?"),
]

TOOL_LABELS = {
    "search_policy": "Searched policy documents",
    "get_vehicle_catalog": "Checked the vehicle catalogue",
    "quote_rental": "Calculated a rental quote",
    "get_booking_details": "Looked up booking records",
    "refund_quote": "Calculated the refund from policy",
    "explain_charges": "Recomputed charges from policy",
    "get_my_profile": "Checked your profile",
    "list_my_bookings": "Listed your bookings",
    "find_customer": "Searched customers (PII masked)",
    "get_customer": "Opened customer record (PII masked)",
    "list_damage_reports": "Looked up damage reports",
    "run_readonly_sql": "Ran a read-only analytics query",
    "content_filter": "Blocked by the platform safety filter",
    "account_snapshot": "Used your account summary (fetched from records at the start of this turn)",
}


# ------------------------------------------------------------------ state & startup

def init_state() -> None:
    defaults = {"user": None, "messages": [], "agent": None, "login_mode": "customer",
                "attempts": 0, "locked_until": 0.0, "pending": None, "session_id": uuid.uuid4().hex[:12]}
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


@st.cache_resource(show_spinner="Preparing RideWise policy index…")
def warm_up() -> bool:
    """Load rules, build/verify the policy index and embedding models once per server."""
    from ridewise.ingest import ensure_index
    from ridewise.retrieval import dense_model, sparse_model
    from ridewise.rules import load_rules
    load_rules()
    ensure_index()
    dense_model()
    sparse_model()
    return True


def start_session(result: auth.LoginResult) -> None:
    st.session_state.user = {"role": result.role, "id": result.user_id, "name": result.display_name,
                             "team": result.team}
    new_conversation()
    st.session_state.attempts = 0


def new_conversation() -> None:
    u = st.session_state.user
    session = Session(u["role"], u["id"] if u["role"] == "customer" else None,
                      session_id=st.session_state.session_id)
    st.session_state.agent = Agent(session)
    st.session_state.messages = []
    st.session_state.pending = None


def sign_out() -> None:
    for k in ("user", "agent", "messages", "pending"):
        st.session_state[k] = None if k != "messages" else []
    st.session_state.session_id = uuid.uuid4().hex[:12]
    st.session_state.login_mode = "customer"


# ------------------------------------------------------------------ login screens

def brand(tagline: str = "Support for rentals, bookings and policies") -> None:
    st.markdown(f'<div class="rw-brand">{LOGO}<div><div class="name">RideWise <span>Assist</span></div>'
                f'<div class="tag">{tagline}</div></div></div>', unsafe_allow_html=True)


def locked() -> bool:
    remaining = st.session_state.locked_until - time.time()
    if remaining > 0:
        st.error(f"Too many attempts. Please wait {int(remaining) + 1} seconds and try again.")
        return True
    return False


def attempt_login(result: auth.LoginResult) -> None:
    if result.ok:
        start_session(result)
        st.rerun()
    st.session_state.attempts += 1
    if st.session_state.attempts >= auth.MAX_ATTEMPTS:
        st.session_state.locked_until = time.time() + 60
        st.session_state.attempts = 0
    st.error(result.error)


def login_page() -> None:
    top_l, top_r = st.columns([3, 1])
    with top_l:
        brand()
    with top_r:
        if st.session_state.login_mode == "customer":
            if st.button("Support staff sign-in", icon=":material/badge:", width="stretch"):
                st.session_state.login_mode = "staff"
                st.rerun()
        elif st.button("Customer sign-in", icon=":material/arrow_back:", width="stretch"):
            st.session_state.login_mode = "customer"
            st.rerun()

    staff = st.session_state.login_mode == "staff"
    st.markdown(
        '<div class="rw-hero"><h1>' + ("Staff console" if staff else "Hi there, how can we help?") + "</h1><p>"
        + ("Sign in with your RideWise staff credentials to assist customers and review operations."
           if staff else "Sign in to ask about your bookings, refunds, charges and RideWise policies.")
        + "</p></div>", unsafe_allow_html=True)

    _, mid, _ = st.columns([1, 1.6, 1])
    with mid:
        with st.container(border=True):
            if staff:
                st.markdown("**Support staff sign-in**")
                with st.form("staff_login"):
                    sid = st.text_input("Staff ID", placeholder="e.g. agent01")
                    pwd = st.text_input("Password", type="password")
                    go = st.form_submit_button("Sign in", type="primary", width="stretch")
                if go and not locked():
                    attempt_login(auth.login_staff(sid, pwd))
            else:
                st.markdown("**Customer sign-in**")
                strict = settings.auth_mode == "strict"
                with st.form("customer_login"):
                    cid = st.text_input("Customer ID", placeholder="e.g. CUST0001")
                    phone = (st.text_input("Password", type="password", placeholder="Your registered mobile number",
                                           help="Your password is the 10-digit mobile number registered "
                                                "with RideWise.") if strict else None)
                    go = st.form_submit_button("Continue", type="primary", width="stretch")
                if not strict:
                    st.caption("Demo mode: sign in with your customer ID only.")
                if go and not locked():
                    attempt_login(auth.login_customer(cid, phone))
        st.markdown('<p class="rw-muted" style="text-align:center;margin-top:.8rem;">🔒 Your data is only ever '
                    'shown to you. RideWise Support: +91 80 4000 1234 (7 AM–11 PM IST)</p>', unsafe_allow_html=True)


# ------------------------------------------------------------------ sidebar

def sidebar() -> None:
    u = st.session_state.user
    with st.sidebar:
        brand("Customer & staff assistant")
        st.write("")
        if u["role"] == "customer":
            p = CustomerData(u["id"]).profile()
            tier = p["customer_tier"]
            kyc_cls = {"VERIFIED": "ok", "PENDING": "warn", "REJECTED": "bad"}[p["kyc_status"]]
            st.markdown(
                f'<div class="rw-identity"><div class="who">{html.escape(p["full_name"])}</div>'
                f'<div class="sub">{u["id"]} · {html.escape(p.get("city") or "")}</div>'
                f'<span class="rw-badge role">Customer</span><span class="rw-badge {tier.lower()}">{tier}</span>'
                f'<span class="rw-badge {kyc_cls}">KYC {p["kyc_status"].title()}</span></div>',
                unsafe_allow_html=True)
        else:
            st.markdown(
                f'<div class="rw-identity"><div class="who">{html.escape(u["name"])}</div>'
                f'<div class="sub">{html.escape(u["id"])} · {html.escape(u.get("team") or "")}</div>'
                f'<span class="rw-badge agent">Support staff</span><span class="rw-badge ok">PII masked</span></div>',
                unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        if c1.button("New chat", icon=":material/add_comment:", width="stretch"):
            new_conversation()
            st.rerun()
        if c2.button("Sign out", icon=":material/logout:", width="stretch"):
            sign_out()
            st.rerun()

        st.markdown("##### Try asking")
        for icon, q in (CUSTOMER_SUGGESTIONS if u["role"] == "customer" else AGENT_SUGGESTIONS):
            if st.button(f"{icon}  {q}", key=f"sb_{q}", width="stretch"):
                st.session_state.pending = q
                st.rerun()

        st.divider()
        last = next((m for m in reversed(st.session_state.messages) if m["role"] == "assistant"), None)
        degraded = bool(last and last.get("degraded")) or st.session_state.agent.client is None
        st.markdown(f'<span class="rw-badge {"warn" if degraded else "ok"}">'
                    f'{"● Limited mode (AI unavailable)" if degraded else "● AI assistant online"}</span>',
                    unsafe_allow_html=True)
        st.caption("Answers come from RideWise records and published policies; amounts and refunds are "
                   "calculated by policy rules in code, not estimated by the AI. Every answer lists its "
                   "sources. The assistant cannot change bookings.")


# ------------------------------------------------------------------ chat rendering

def _step_detail(step: dict) -> str:
    args, res = step.get("args") or {}, step.get("result") or {}
    if not step.get("ok"):
        return html.escape(res.get("error", "failed"))
    name = step["tool"]
    if name == "search_policy":
        return "Query: “" + html.escape(str(args.get("query", ""))) + "”"
    if name == "refund_quote" and "rule" in res:
        return html.escape(f"{res['rule']} → {res.get('refund_pct', 0):g}% ({res.get('policy_doc', '')})")
    if name in ("get_booking_details", "explain_charges", "refund_quote") and args.get("booking_id"):
        return "Booking " + html.escape(args["booking_id"])
    if name == "run_readonly_sql":
        return f"{res.get('row_count', 0)} row(s)" + (" (truncated)" if res.get("truncated") else "")
    if name == "quote_rental":
        return html.escape(f"{args.get('model')} · {args.get('hours')} h")
    return html.escape(", ".join(f"{k}={v}" for k, v in args.items()) or "—")


_SQL_BREAKS = re.compile(r"\s+(FROM|LEFT JOIN|JOIN|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|UNION ALL)\b",
                         re.IGNORECASE)


def pretty_sql(sql: str) -> str:
    """Put each main clause on its own line (display only; the text stays valid SQL)."""
    if "CASE lower(trim(" in sql:          # long portable CASE expressions read better unbroken
        return sql
    parts = sql.split("'")                 # even indexes are outside string literals ('' escapes stay paired)
    return "'".join(_SQL_BREAKS.sub(lambda m: "\n" + m.group(1).upper(), p) if i % 2 == 0 else p
                    for i, p in enumerate(parts))


def _calc_steps(step: dict) -> list[str]:
    res = step.get("result") or {}
    if step["tool"] == "refund_quote":
        return res.get("steps", [])
    if step["tool"] == "quote_rental":
        return (res.get("rental") or {}).get("steps", [])
    if step["tool"] == "explain_charges":
        out = []
        for key in ("late_fee", "battery_fee"):
            exp = (res.get(key) or {}).get("expected") or {}
            out += exp.get("steps", [])
        est = res.get("late_fee_estimate") or {}
        return out + est.get("steps", [])
    return []


def render_details(msg: dict, is_agent: bool) -> None:
    trace, sources = msg.get("trace") or [], msg.get("sources") or []
    if sources:
        with st.expander(f"Sources ({len(sources)})", icon=":material/menu_book:"):
            pdfs = [s for s in sources if s["kind"] == "pdf"]
            dbs = [s for s in sources if s["kind"] == "db"]
            other = [s for s in sources if s["kind"] not in ("pdf", "db")]
            if pdfs:
                st.markdown("**📄 Policy documents (PDF)**")
                for s in pdfs:
                    st.markdown(f"- {cite_label(s)}")
            if dbs:
                st.markdown("**🗄️ Database records (ridewise.db)**")
                for s in dbs:
                    st.markdown(f"- {cite_label(s)}")
            for s in other:
                st.markdown(f"- {cite_label(s)}")
    elif msg["role"] == "assistant" and not msg.get("degraded"):
        st.markdown('<div class="rw-meta">No records or policy documents were needed for this reply.</div>',
                    unsafe_allow_html=True)
    if trace:
        with st.expander("How I got this", icon=":material/account_tree:"):
            for i, step in enumerate(trace, 1):
                label = TOOL_LABELS.get(step["tool"], step["tool"])
                cls = "" if step.get("ok") else " err"
                ms = f" · {step['ms']} ms" if step.get("ms") is not None else ""
                st.markdown(f'<div class="rw-step{cls}"><div class="t">{i}. {label}</div>'
                            f'<div class="d">{_step_detail(step)}{ms}</div></div>', unsafe_allow_html=True)
                for line in _calc_steps(step):
                    st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;• {line}")
                for q in (step.get("result") or {}).get("queries") or []:
                    st.markdown(f'<div class="rw-meta">SQL run · {q["rows"]} row(s) returned</div>',
                                unsafe_allow_html=True)
                    st.code(pretty_sql(q["sql"]), language="sql")
            if any((s.get("result") or {}).get("queries") for s in trace):
                st.caption("Paste any query above into DB Browser for SQLite → Execute SQL to see the same rows. "
                           "The database stores times in UTC (answers show IST) and amounts excluding GST.")
            if is_agent and st.toggle("Show raw tool data", key=f"raw_{id(msg)}_{len(trace)}"):
                st.json(json.loads(json.dumps(trace, default=str)), expanded=False)


def render_message(msg: dict, is_agent: bool) -> None:
    avatar = ":material/person:" if msg["role"] == "user" else ":material/two_wheeler:"
    with st.chat_message(msg["role"], avatar=avatar):
        if msg.get("degraded"):
            st.markdown('<span class="rw-badge warn">Limited mode — answered directly from records</span>',
                        unsafe_allow_html=True)
        # Model output is rendered as Markdown only (no raw HTML); '$' escaped so it never becomes LaTeX.
        st.markdown(msg["content"].replace("$", "\\$"))
        if msg["role"] == "assistant":
            tools = len([t for t in msg.get("trace") or [] if t["tool"] != "content_filter"])
            st.markdown(f'<div class="rw-meta">{msg.get("seconds", 0)} s · {tools} lookup(s)</div>',
                        unsafe_allow_html=True)
            render_details(msg, is_agent)


def welcome(u: dict) -> None:
    first = (u["name"] or "").split()[0] if u["name"] else ""
    if u["role"] == "customer":
        title, body = (f"Hi {html.escape(first)} 👋",
                       "Ask me about your bookings, payments, refunds and charges, or any RideWise rule — "
                       "rentals, cancellations, late returns, deposits and EVs.")
        suggestions = CUSTOMER_SUGGESTIONS
    else:
        title, body = (f"Welcome, {html.escape(first)}",
                       "Look up bookings and customers (contact details masked), verify refunds and charges "
                       "against policy, review damage claims, or run operational analytics.")
        suggestions = AGENT_SUGGESTIONS
    st.markdown(f'<div class="rw-welcome"><h3>{title}</h3><p>{body}</p></div>', unsafe_allow_html=True)
    cols = st.columns(2)
    for i, (icon, q) in enumerate(suggestions[:4]):
        if cols[i % 2].button(f"{icon}  {q}", key=f"wel_{q}", width="stretch"):
            st.session_state.pending = q
            st.rerun()


def chat_page() -> None:
    u = st.session_state.user
    is_agent = u["role"] == "agent"
    sidebar()

    head_l, head_r = st.columns([4, 1])
    with head_l:
        st.markdown("### " + ("Staff console" if is_agent else "Your RideWise assistant"))
    with head_r:
        st.markdown(f'<div style="text-align:right;padding-top:.6rem;"><span class="rw-badge '
                    f'{"agent" if is_agent else "role"}">{"Support staff" if is_agent else "Customer"}</span></div>',
                    unsafe_allow_html=True)

    if not st.session_state.messages and not st.session_state.pending:
        welcome(u)

    for msg in st.session_state.messages:
        render_message(msg, is_agent)

    typed = st.chat_input("Ask about bookings, refunds, charges or policies…")
    question = typed or st.session_state.pending
    st.session_state.pending = None
    if question:
        history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages]
        user_msg = {"role": "user", "content": question}
        st.session_state.messages.append(user_msg)
        render_message(user_msg, is_agent)
        with st.chat_message("assistant", avatar=":material/two_wheeler:"):
            with st.spinner("Checking RideWise records…"):
                reply = st.session_state.agent.respond(history, question)
        st.session_state.messages.append({"role": "assistant", "content": reply.answer, "sources": reply.sources,
                                          "trace": reply.trace, "degraded": reply.degraded,
                                          "seconds": reply.seconds})
        st.rerun()


# ------------------------------------------------------------------ main

def main() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()
    try:
        warm_up()
    except Exception as e:      # index/model problems should not take the whole app down
        st.warning(f"Policy search is unavailable ({type(e).__name__}). Data lookups still work.")
    if st.session_state.user is None:
        login_page()
    else:
        chat_page()


main()

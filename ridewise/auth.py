"""Login checks for the two roles.

Customers: customer_id (AUTH_MODE=demo) or customer_id + registered phone (AUTH_MODE=strict).
Staff: staff ID + password, verified against salted PBKDF2 hashes in config/staff_users.json.
Failures return one generic message so the login never reveals which IDs exist.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass

from ridewise import db
from ridewise.config import ROOT, settings

STAFF_FILE = ROOT / "config" / "staff_users.json"
GENERIC_FAILURE = "We couldn't verify those details. Please check and try again."
MAX_ATTEMPTS = 5
PBKDF2_ITERATIONS = 200_000


@dataclass
class LoginResult:
    ok: bool
    role: str | None = None
    user_id: str | None = None
    display_name: str | None = None
    team: str | None = None
    error: str | None = None


CUSTOMER_INPUT_RE = re.compile(r"^\s*(?:cust)?\s*-?\s*(\d{1,4})\s*$", re.IGNORECASE)


def login_customer(customer_id: str, phone: str | None = None) -> LoginResult:
    m = CUSTOMER_INPUT_RE.match(customer_id or "")      # accepts CUST0001, cust1, 0001 — nothing else
    if not m:
        return LoginResult(False, error=GENERIC_FAILURE)
    cid = f"CUST{int(m.group(1)):04d}"
    if not db.customer_exists(cid):
        return LoginResult(False, error=GENERIC_FAILURE)
    if settings.auth_mode == "strict" and not db.customer_phone_matches(cid, phone or ""):
        return LoginResult(False, error=GENERIC_FAILURE)
    name = db.CustomerData(cid).profile()["full_name"]
    return LoginResult(True, "customer", cid, name)


def hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": PBKDF2_ITERATIONS}


def _staff_users() -> dict[str, dict]:
    if not STAFF_FILE.exists():
        return {}
    return {u["staff_id"].lower(): u for u in json.loads(STAFF_FILE.read_text())["users"]}


def login_staff(staff_id: str, password: str) -> LoginResult:
    user = _staff_users().get((staff_id or "").strip().lower())
    # Always run the hash so response time does not reveal whether the ID exists.
    salt = bytes.fromhex(user["salt"]) if user else b"\x00" * 16
    iterations = user["iterations"] if user else PBKDF2_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt, iterations).hex()
    if not user or not hmac.compare_digest(digest, user["hash"]):
        return LoginResult(False, error=GENERIC_FAILURE)
    return LoginResult(True, "agent", user["staff_id"], user["name"], user.get("team"))

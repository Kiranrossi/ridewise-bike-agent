"""PII masking, free-text scrubbing and untrusted-data wrapping."""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Indian mobile numbers: optional +91/91/0 prefix, 10 digits starting 6-9, optional separators.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
UNTRUSTED_TAG_RE = re.compile(r"</?\s*untrusted_data\s*>", re.IGNORECASE)


def mask_phone(phone: str | None, keep_last: int = 0) -> str | None:
    if not phone:
        return phone
    digits = re.sub(r"\D", "", phone)
    if keep_last:
        return "*" * (len(digits) - keep_last) + digits[-keep_last:]
    return digits[:2] + "*" * (len(digits) - 4) + digits[-2:]


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    user, domain = email.split("@", 1)
    return f"{user[0]}***@{domain}"


def mask_licence(licence: str | None) -> str | None:
    if not licence or len(licence) < 8:
        return "****" if licence else licence
    return licence[:4] + "*" * (len(licence) - 7) + licence[-3:]


def scrub_text(text: str | None) -> str | None:
    """Mask any email address or phone number embedded in free text."""
    if not text:
        return text
    text = EMAIL_RE.sub(lambda m: mask_email(m.group()), text)
    return PHONE_RE.sub(lambda m: mask_phone(m.group()), text)


def wrap_untrusted(text: str | None) -> str | None:
    """Scrub and wrap free text so the LLM treats it as data, never as instructions."""
    if text is None:
        return None
    clean = UNTRUSTED_TAG_RE.sub("", scrub_text(text))
    return f"<untrusted_data>{clean}</untrusted_data>"


def mask_customer(row: dict, view: str) -> dict:
    """Apply the PII view for a customer record.

    view: 'self'        - the customer's own record
          'agent_list'  - staff search results / lists
          'agent_single'- staff looking up one customer
    """
    out = dict(row)
    out.pop("date_of_birth", None)
    if view == "self":
        out["driving_license_no"] = mask_licence(out.get("driving_license_no"))
    elif view == "agent_single":
        out["phone"] = mask_phone(out.get("phone"), keep_last=4)
        out["email"] = mask_email(out.get("email"))
        out["driving_license_no"] = mask_licence(out.get("driving_license_no"))
    elif view == "agent_list":
        out["phone"] = mask_phone(out.get("phone"))
        out["email"] = mask_email(out.get("email"))
        out.pop("driving_license_no", None)
    else:
        raise ValueError(f"unknown PII view: {view}")
    return out

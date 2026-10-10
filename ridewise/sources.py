"""Turn raw tool source strings into precise, typed citations.

Every citation says where the information came from:
  * kind 'pdf': a policy document — doc ID, title, section/page and the PDF file name
  * kind 'db' : ridewise.db — the table and the record IDs
"""
from __future__ import annotations

import re
from pathlib import Path

from ridewise.rules import load_rules

DB_FILE = "ridewise.db"
_DOC_RE = re.compile(r"RW-POL-\d+")
_DB_PATTERNS = [
    (re.compile(r"^booking (B-\d{4})"), "bookings"),
    (re.compile(r"^(PAY\d+)$"), "payments"),
    (re.compile(r"^(DMG\d+)$"), "damage_reports"),
    (re.compile(r"^customer (CUST\d{4})"), "customers"),
]


def _pdf(doc_id: str, detail: str) -> dict:
    doc = load_rules().documents.get(doc_id)
    detail = detail.strip(" ,;")
    scanned = bool(re.search(r"scanned|transcribed|image", detail, re.IGNORECASE))
    detail = re.sub(r"\s*\((?:transcribed from )?scanned[^)]*\)|\s*\(transcribed[^)]*\)|\s*\(image\)", "", detail)
    title = doc.title if "version" in doc.title.lower() else f"{doc.title} (v{doc.version})"
    return {"kind": "pdf", "doc_id": doc_id, "title": title,
            "detail": detail.strip(" ,"), "file": Path(doc.file).name, "scanned": scanned}


def _parse(raw: str) -> list[dict]:
    raw = raw.strip()
    for pattern, table in _DB_PATTERNS:
        m = pattern.match(raw)
        if m:
            return [{"kind": "db", "table": table, "ids": [m.group(1)]}]
    if raw.startswith("customers table"):
        return [{"kind": "db", "table": "customers", "ids": []}]
    if raw.startswith("vehicles table"):
        return [{"kind": "db", "table": "vehicles", "ids": []}]
    if raw.startswith("Read-only analytics query"):
        return [{"kind": "db", "table": "analytics query", "ids": []}]
    if _DOC_RE.search(raw):
        out, current = [], None
        for part in raw.split(";"):
            m = _DOC_RE.search(part)
            if m:
                current = m.group()
                part = part.replace(current, "", 1)
            if current:
                out.append(_pdf(current, part))
        return out
    return [{"kind": "other", "label": raw}]


def normalise(raw_sources: list[str], session_customer: str | None = None) -> list[dict]:
    """Parse, merge DB records per table and de-duplicate policy citations."""
    db: dict[str, list[str]] = {}
    pdf: dict[tuple, dict] = {}
    other: list[dict] = []
    for raw in raw_sources:
        for s in _parse(raw):
            if s["kind"] == "db":
                ids = db.setdefault(s["table"], [])
                if s["table"] == "customers" and not s["ids"] and session_customer:
                    s["ids"] = [session_customer]
                ids += [i for i in s["ids"] if i not in ids]
            elif s["kind"] == "pdf":
                key = (s["doc_id"], s["detail"])
                pdf.setdefault(key, s)
            elif s not in other:
                other.append(s)
    out = list(pdf.values())
    out += [{"kind": "db", "table": t, "ids": ids, "file": DB_FILE} for t, ids in db.items()]
    return out + other


def label(source: dict) -> str:
    """One-line human-readable citation."""
    if source["kind"] == "pdf":
        detail = f" · {source['detail']}" if source["detail"] else ""
        scanned = " (scanned page, transcribed)" if source["scanned"] else ""
        return f"{source['doc_id']} {source['title']}{detail}{scanned} — {source['file']}"
    if source["kind"] == "db":
        if source["table"] == "analytics query":
            return f"{DB_FILE} · read-only analytics query (SQL under How I got this)"
        ids = f": {', '.join(source['ids'])}" if source["ids"] else ""
        return f"{DB_FILE} · {source['table']} table{ids}"
    return source["label"]

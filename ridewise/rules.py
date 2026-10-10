"""Business rules loaded from rules/*.yaml and validated with Pydantic.

The YAML files are the single source of truth for every policy number. The app
refuses to start if a file is malformed or the files contradict each other.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ridewise.config import ROOT, settings


def _require_aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        raise ValueError(f"datetime {dt} must include a timezone offset")
    return dt


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


# ---------- documents.yaml ----------

class ImageOnly(_Model):
    page: int
    content: str
    transcription: str


class Document(_Model):
    doc_id: str
    file: str
    title: str
    version: str
    effective_from: datetime | None = None
    applies_to: str | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    topics: list[str] = []
    image_only: list[ImageOnly] = []

    _aware = field_validator("effective_from")(_require_aware)

    @field_validator("file")
    @classmethod
    def _file_exists(cls, v: str) -> str:
        if not (ROOT / v).exists():
            raise ValueError(f"document file not found: {v}")
        return v


class Documents(_Model):
    documents: list[Document]

    def get(self, doc_id: str) -> Document:
        for d in self.documents:
            if d.doc_id == doc_id:
                return d
        raise KeyError(doc_id)


# ---------- refund_policy.yaml ----------

class Slab(_Model):
    min_hours_before: float = Field(ge=0)
    refund_pct: float = Field(ge=0, le=100)


class RefundVersion(_Model):
    id: str
    doc_id: str
    effective_from: datetime
    effective_to: datetime | None = None
    slabs: list[Slab]
    source: str

    _aware = field_validator("effective_from", "effective_to")(_require_aware)

    @field_validator("slabs")
    @classmethod
    def _slabs_ordered(cls, slabs: list[Slab]) -> list[Slab]:
        mins = [s.min_hours_before for s in slabs]
        if mins != sorted(mins, reverse=True) or len(set(mins)) != len(mins):
            raise ValueError("slabs must be in strictly descending min_hours_before order")
        if mins[-1] != 0:
            raise ValueError("last slab must start at 0 hours so every cancellation matches")
        return slabs

    def slab_for(self, hours_before: float) -> Slab:
        """First slab whose threshold is met (thresholds are inclusive: 'X hours or more')."""
        for s in self.slabs:
            if hours_before >= s.min_hours_before:
                return s
        return self.slabs[-1]


class RefundPolicy(_Model):
    version_selector: Literal["booked_at"]
    versions: list[RefundVersion]
    common: dict
    worked_examples: list[dict] = []

    @model_validator(mode="after")
    def _contiguous(self) -> RefundPolicy:
        vs = self.versions
        for a, b in zip(vs, vs[1:]):
            if a.effective_to != b.effective_from:
                raise ValueError(f"refund versions {a.id} and {b.id} must be contiguous")
        if vs[-1].effective_to is not None:
            raise ValueError("the latest refund version must have effective_to: null")
        return self

    def version_for(self, booked_at: datetime) -> RefundVersion:
        """Policy version that governs a booking, decided by when it was made."""
        _require_aware(booked_at)
        for v in self.versions:
            if booked_at >= v.effective_from and (v.effective_to is None or booked_at < v.effective_to):
                return v
        raise ValueError(f"no refund policy covers bookings made at {booked_at.isoformat()}")


# ---------- pricing.yaml ----------

class DepositRule(_Model):
    vehicle_types: list[str]
    min_hours: float = Field(ge=0)
    amount: float = Field(ge=0)


class Deposit(_Model):
    rules: list[DepositRule]
    gst: bool
    source: str


class Booking(_Model):
    whole_hours_only: bool
    min_hours: int = Field(ge=1)
    source: str


class RentalCharge(_Model):
    block_hours: int
    remainder_capped_at_daily_rate: bool
    source: str
    worked_examples: list[dict] = []


class Pricing(_Model):
    gst_rate: float = Field(gt=0, lt=1)
    gst_source: str
    booking: Booking
    rental_charge: RentalCharge
    payment: dict
    deposit: Deposit


# ---------- penalties.yaml ----------

class LateReturn(_Model):
    rate_multiplier: float = Field(gt=0)
    rounding: Literal["ceil_hour"]
    grace_minutes_by_tier: dict[str, int]
    grace_inclusive: bool
    beyond_grace: Literal["full_lateness"]
    source: str
    worked_examples: list[dict] = []


class BatteryBand(_Model):
    name: str
    from_pct: int
    to_pct: int
    note: str


class LowBattery(_Model):
    applies_to: list[str]
    fee_if_below_pct: float
    fee: float = Field(ge=0)
    stacks_with_late_fee: bool
    bands: list[BatteryBand]
    source: str


class ClaimStatus(_Model):
    charged: bool
    meaning: str


class Damage(_Model):
    claim_status: dict[str, ClaimStatus]
    charge_basis: str
    lost_key: str
    source: str


class Penalties(_Model):
    late_return: LateReturn
    low_battery: LowBattery
    damage: Damage


# ---------- rental_terms.yaml ----------

class RentalTerms(_Model):
    eligibility: dict
    kyc: dict
    no_show: dict
    tiers: dict
    ev: dict
    support_contact: dict


# ---------- aliases.yaml ----------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_.]", " ", text.strip().lower()))


class Aliases(_Model):
    cities: dict[str, list[str]]
    hubs: dict[str, list[str]]
    vehicle_types: dict[str, list[str]]
    booking_status: dict[str, dict[str, str]]
    payment_type: dict[str, str]
    payment_status: dict[str, str]
    vehicle_status: dict[str, str]

    @model_validator(mode="after")
    def _no_ambiguous_aliases(self) -> Aliases:
        for group in ("cities", "hubs", "vehicle_types"):
            seen: dict[str, str] = {}
            for canonical, names in getattr(self, group).items():
                for n in [canonical, *names]:
                    key = _norm(n)
                    if seen.get(key, canonical) != canonical:
                        raise ValueError(f"alias '{n}' maps to both '{seen[key]}' and '{canonical}' in {group}")
                    seen[key] = canonical
        return self

    def _lookup(self, group: str, value: str | None) -> str | None:
        """Exact alias match first; then a conservative fuzzy match for typos
        ('Whitfield' → Whitefield). Short inputs are never fuzzy-matched."""
        if not value:
            return None
        key = _norm(value)
        index = {_norm(n): canonical
                 for canonical, names in getattr(self, group).items() for n in [canonical, *names]}
        if key in index:
            return index[key]
        if len(key) >= 5:
            close = difflib.get_close_matches(key, index, n=1, cutoff=0.85)
            if close:
                return index[close[0]]
        return None

    def city(self, value: str | None) -> str | None:
        return self._lookup("cities", value)

    def hub(self, value: str | None) -> str | None:
        return self._lookup("hubs", value)

    def vehicle_type(self, value: str | None) -> str | None:
        return self._lookup("vehicle_types", value)

    def status_label(self, code: str, role: str) -> str:
        return self.booking_status.get(code, {}).get(role, code)


# ---------- access_policy.yaml ----------

class Role(_Model):
    description: str
    data_scope: str
    tools: list[str]


class SqlPolicy(_Model):
    allowed_tables: list[str]
    blocked_columns: dict[str, list[str]]
    max_rows: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)


class AccessPolicy(_Model):
    roles: dict[str, Role]
    pii: dict
    free_text: list[str]
    sql: SqlPolicy

    @model_validator(mode="after")
    def _pii_always_blocked_in_sql(self) -> AccessPolicy:
        for table, cols in self.pii["columns"].items():
            missing = set(cols) - set(self.sql.blocked_columns.get(table, []))
            if missing:
                raise ValueError(f"PII columns {sorted(missing)} of {table} must be blocked in SQL")
        return self

    def tools_for(self, role: str) -> list[str]:
        return self.roles[role].tools


# ---------- container ----------

class Rules(_Model):
    documents: Documents
    refund: RefundPolicy
    pricing: Pricing
    penalties: Penalties
    terms: RentalTerms
    aliases: Aliases
    access: AccessPolicy

    @model_validator(mode="after")
    def _cross_file_consistency(self) -> Rules:
        doc_ids = {d.doc_id for d in self.documents.documents}
        for v in self.refund.versions:
            if v.doc_id not in doc_ids:
                raise ValueError(f"refund version {v.id} references unknown document {v.doc_id}")
        tiers = set(self.terms.tiers["values"])
        if set(self.penalties.late_return.grace_minutes_by_tier) != tiers:
            raise ValueError("grace_minutes_by_tier must define exactly the tiers in rental_terms.yaml")
        known_types = set(self.aliases.vehicle_types)
        used = set(self.penalties.low_battery.applies_to)
        for r in self.pricing.deposit.rules:
            used |= set(r.vehicle_types)
        if not used <= known_types:
            raise ValueError(f"unknown vehicle types in rules: {sorted(used - known_types)}")
        return self


_FILES = {
    "documents": "documents.yaml",
    "refund": "refund_policy.yaml",
    "pricing": "pricing.yaml",
    "penalties": "penalties.yaml",
    "terms": "rental_terms.yaml",
    "aliases": "aliases.yaml",
    "access": "access_policy.yaml",
}


def _read(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_rules(rules_dir: Path | None = None) -> Rules:
    rules_dir = rules_dir or settings.rules_dir
    return Rules(**{key: _read(rules_dir / name) for key, name in _FILES.items()})

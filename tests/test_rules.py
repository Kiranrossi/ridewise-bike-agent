import shutil
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from pydantic import ValidationError

from ridewise.config import settings
from ridewise.rules import load_rules

UTC = timezone.utc


def utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


@pytest.fixture(scope="module")
def rules():
    return load_rules()


def test_all_rule_files_load(rules):
    assert len(rules.documents.documents) == 5
    assert rules.pricing.gst_rate == 0.18


@pytest.mark.parametrize("booked_at_utc, expected", [
    ("2026-06-30 17:45:00", "v1"),   # B-1163: 23:15 IST on 30 June
    ("2026-06-30 18:29:59", "v1"),   # one second before cutoff
    ("2026-06-30 18:30:00", "v2"),   # exactly 1 July 00:00 IST
    ("2026-06-30 19:33:00", "v2"),   # B-1164: UTC date is 30 June but IST is 1 July
    ("2026-06-30 20:15:00", "v2"),   # B-1165
    ("2025-01-21 07:33:00", "v1"),   # earliest booking in the DB
])
def test_refund_version_uses_ist_cutoff(rules, booked_at_utc, expected):
    assert rules.refund.version_for(utc(booked_at_utc)).id == expected


def test_version_for_rejects_naive_datetime(rules):
    with pytest.raises(ValueError):
        rules.refund.version_for(datetime(2026, 7, 1))


@pytest.mark.parametrize("version, hours, pct", [
    ("v1", 24, 100), ("v1", 23.99, 50), ("v1", 6, 50), ("v1", 5.99, 0),
    ("v2", 48, 100), ("v2", 47.99, 75), ("v2", 12, 75), ("v2", 11.99, 25),
    ("v2", 2, 25), ("v2", 1.99, 0), ("v2", 0, 0),
])
def test_slab_thresholds_are_inclusive(rules, version, hours, pct):
    v = next(v for v in rules.refund.versions if v.id == version)
    assert v.slab_for(hours).refund_pct == pct


@pytest.mark.parametrize("raw, canonical", [
    ("BLR", "Bengaluru"), ("bangalore", "Bengaluru"), (" Bangalore ", "Bengaluru"),
    ("Mysore", "Mysuru"), ("Chennai", "Chennai"),
])
def test_city_aliases(rules, raw, canonical):
    assert rules.aliases.city(raw) == canonical


def test_hub_and_vehicle_aliases(rules):
    assert rules.aliases.hub("EC") == "Electronic City"
    assert rules.aliases.hub("hsr") == "HSR Layout"
    assert rules.aliases.hub("Indira Nagar") == "Indiranagar"
    assert rules.aliases.hub("Mars") is None
    assert rules.aliases.vehicle_type("electric scooter") == "EV_SCOOTER"
    assert rules.aliases.vehicle_type("e-bike") == "EV_BIKE"


@pytest.mark.parametrize("typo, canonical", [
    ("Whitfield", "Whitefield"), ("Koramangla", "Koramangala"), ("Indranagar", "Indiranagar"),
    ("Bengalore", "Bengaluru"), ("Jayangar", "Jayanagar"),
])
def test_fuzzy_typos(rules, typo, canonical):
    assert (rules.aliases.hub(typo) or rules.aliases.city(typo)) == canonical


def test_fuzzy_is_conservative(rules):
    assert rules.aliases.hub("Hosur") is None        # not a hub
    assert rules.aliases.hub("ecc") is None          # too short to fuzzy-match
    assert rules.aliases.city("Delhi") is None


def test_status_labels_per_role(rules):
    assert rules.aliases.status_label("CNL_U", "customer") == "Cancelled by you"
    assert rules.aliases.status_label("CNL_U", "agent") == "Cancelled by customer (CNL_U)"


def test_tier_grace(rules):
    assert rules.penalties.late_return.grace_minutes_by_tier == {"STANDARD": 0, "SILVER": 0, "GOLD": 30}
    assert rules.penalties.late_return.grace_inclusive is True


def test_pii_blocked_in_sql(rules):
    blocked = rules.access.sql.blocked_columns["customers"]
    assert {"phone", "email", "driving_license_no", "date_of_birth"} <= set(blocked)


def test_customer_cannot_use_agent_tools(rules):
    customer = set(rules.access.tools_for("customer"))
    assert not customer & {"run_readonly_sql", "find_customer", "get_customer", "list_damage_reports"}


# ---- malformed rules must be rejected (fail fast) ----

def _broken_copy(tmp_path, filename, mutate):
    d = tmp_path / "rules"
    shutil.copytree(settings.rules_dir, d)
    data = yaml.safe_load((d / filename).read_text())
    mutate(data)
    (d / filename).write_text(yaml.safe_dump(data))
    load_rules.cache_clear()
    return d


def test_rejects_unordered_slabs(tmp_path):
    d = _broken_copy(tmp_path, "refund_policy.yaml",
                     lambda y: y["versions"][1]["slabs"].reverse())
    with pytest.raises(ValidationError):
        load_rules(d)
    load_rules.cache_clear()


def test_rejects_gap_between_versions(tmp_path):
    def gap(y):
        y["versions"][0]["effective_to"] = "2026-06-01T00:00:00+05:30"
    d = _broken_copy(tmp_path, "refund_policy.yaml", gap)
    with pytest.raises(ValidationError):
        load_rules(d)
    load_rules.cache_clear()


def test_rejects_unblocked_pii(tmp_path):
    def unblock(y):
        y["sql"]["blocked_columns"]["customers"].remove("phone")
    d = _broken_copy(tmp_path, "access_policy.yaml", unblock)
    with pytest.raises(ValidationError):
        load_rules(d)
    load_rules.cache_clear()

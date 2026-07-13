"""
Offline regression test for the rule-closing guard in
``airtable_io.upsert_pricing_rules``.

Background (see CLAUDE.md sections 5e / 6 / 10): ``/upload-master`` calls
``upsert_pricing_rules(rules, close_keys_at_date=vf)``. The close pass must
NOT close a still-open rule whose ``valid_from`` is on/after the close date
-- same-date or *future-dated* rules (e.g. a future RPI master, or a
``/add-support`` support rule) must stay active. That guard reads
``valid_from``, so the field MUST be in the projection used to fetch the
existing rules. If ``valid_from`` is dropped from that projection (the
original bug), uploading an *earlier*-dated master silently closes a
future-dated open rule at ``vf``, producing an inverted/empty interval
(``valid_to <= valid_from``).

No Airtable network access: ``airtable_io._list_all`` and ``._batch`` are
replaced with in-memory fakes. The fake ``_list_all`` honors the ``fields=``
projection exactly like Airtable does, so omitting ``valid_from`` from the
real projection makes the future-dated scenario fail -- which is the whole
point of the test.

Run standalone (exit 0 = pass, 1 = fail):

    python test_upsert_close_guard.py

Also discoverable by pytest (the ``test_*`` functions), but pytest is not a
project dependency.
"""

from __future__ import annotations

import os
from datetime import date

# airtable_io reads these at import time; set dummy values so importing it
# does not require real Render secrets. We never touch the network.
os.environ.setdefault("AIRTABLE_TOKEN", "test-token")
os.environ.setdefault("AIRTABLE_BASE_ID", "appTEST")

import airtable_io  # noqa: E402
from reconcile import Rule  # noqa: E402

# ---- synthetic master: one site, one product ----
SITE_ID = "001"
PROD_CODE = "PKEG1"
SITE_REC = "rec_site_001"
PROD_REC = "rec_prod_PKEG1"

SITES = [{"id": SITE_REC, "fields": {"site_id": SITE_ID}}]
PRODUCTS = [
    {"id": PROD_REC, "fields": {"product_code": PROD_CODE, "description": "Test Keg"}}
]


def _pr_record(rec_id: str, rule_key: str, valid_from=None, valid_to=None) -> dict:
    """A synthetic PricingRules record linked to the one site+product above."""
    fields: dict = {"rule_key": rule_key, "site": [SITE_REC], "product": [PROD_REC]}
    if valid_from is not None:
        fields["valid_from"] = valid_from
    if valid_to is not None:
        fields["valid_to"] = valid_to
    return {"id": rec_id, "fields": fields}


def _new_rule(valid_from: date) -> Rule:
    return Rule(
        site_id=SITE_ID,
        product_code=PROD_CODE,
        product_desc="Test Keg",
        tenant_price=100.0,
        fb_price=120.0,
        valid_from=valid_from,
        status="tenanted",
        source="test",
    )


def _run_upsert(existing_pricing_records, new_rules, close_at):
    """Install in-memory fakes, run upsert_pricing_rules, capture _batch calls."""
    calls: list[tuple] = []
    tables = {
        airtable_io.T["Sites"]: SITES,
        airtable_io.T["Products"]: PRODUCTS,
        airtable_io.T["PricingRules"]: existing_pricing_records,
    }

    def fake_list_all(table_id, fields=None, filter_by_formula=None):
        # filter_by_formula (server-side scope) is verified against live Airtable
        # separately; the fake returns the full set so the in-Python close pass —
        # what these tests assert — runs over every record.
        out = []
        for rec in tables.get(table_id, []):
            f = rec["fields"]
            if fields is not None:
                # Mimic Airtable: only the projected fields come back.
                f = {k: f[k] for k in fields if k in f}
            out.append({"id": rec["id"], "fields": dict(f)})
        return out

    def fake_batch(records, op, table_id):
        calls.append((op, table_id, [dict(r) for r in records]))
        if op == "create":
            return [
                {"id": f"rec_new_{i}", "fields": r["fields"]}
                for i, r in enumerate(records)
            ]
        return [{"id": r.get("id"), "fields": r.get("fields", {})} for r in records]

    orig_list, orig_batch = airtable_io._list_all, airtable_io._batch
    airtable_io._list_all = fake_list_all
    airtable_io._batch = fake_batch
    try:
        result = airtable_io.upsert_pricing_rules(new_rules, close_at)
    finally:
        airtable_io._list_all = orig_list
        airtable_io._batch = orig_batch
    return result, calls


def _closed_ids(calls) -> dict:
    """Records closed by the close pass: an update whose ONLY field is valid_to."""
    out: dict = {}
    for op, table_id, records in calls:
        if op != "update" or table_id != airtable_io.T["PricingRules"]:
            continue
        for r in records:
            f = r.get("fields", {})
            if set(f.keys()) == {"valid_to"}:
                out[r["id"]] = f["valid_to"]
    return out


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_future_dated_rule_is_not_closed():
    """THE BUG: an earlier-dated master upload must not close a future rule."""
    existing = [
        _pr_record("rec_future", "001|PKEG1|2026-12-01", valid_from="2026-12-01")
    ]
    (created, updated, closed), calls = _run_upsert(
        existing, [_new_rule(date(2026, 6, 1))], date(2026, 6, 1)
    )
    closed_ids = _closed_ids(calls)
    assert "rec_future" not in closed_ids, (
        "future-dated open rule was closed at an EARLIER date -> "
        f"inverted interval (closed_ids={closed_ids})"
    )
    assert closed == 0, f"expected 0 closed, got {closed}"


def test_earlier_dated_rule_is_closed():
    """Positive control: a genuinely prior open rule still gets closed at vf."""
    existing = [
        _pr_record("rec_old", "001|PKEG1|2026-01-01", valid_from="2026-01-01")
    ]
    (created, updated, closed), calls = _run_upsert(
        existing, [_new_rule(date(2026, 6, 1))], date(2026, 6, 1)
    )
    closed_ids = _closed_ids(calls)
    assert closed_ids.get("rec_old") == "2026-06-01", (
        f"expected rec_old closed at 2026-06-01, got {closed_ids}"
    )
    assert closed == 1, f"expected 1 closed, got {closed}"


def test_same_rule_key_reupload_is_not_closed():
    """Same-date re-upload: the matching key is updated in place, never closed."""
    existing = [
        _pr_record("rec_same", "001|PKEG1|2026-06-01", valid_from="2026-06-01")
    ]
    (created, updated, closed), calls = _run_upsert(
        existing, [_new_rule(date(2026, 6, 1))], date(2026, 6, 1)
    )
    closed_ids = _closed_ids(calls)
    assert "rec_same" not in closed_ids, f"same-key rule was closed: {closed_ids}"
    assert closed == 0, f"expected 0 closed, got {closed}"
    assert updated == 1, f"expected the same-key rule updated in place, got {updated}"
    assert created == 0, f"expected 0 created, got {created}"


def test_open_rule_without_valid_from_is_still_closed():
    """Guard must not over-protect: an open rule with no valid_from still closes."""
    existing = [_pr_record("rec_open", "001|PKEG1|open", valid_from=None)]
    (created, updated, closed), calls = _run_upsert(
        existing, [_new_rule(date(2026, 6, 1))], date(2026, 6, 1)
    )
    closed_ids = _closed_ids(calls)
    assert closed_ids.get("rec_open") == "2026-06-01", (
        f"open (no valid_from) rule should be closed at vf, got {closed_ids}"
    )
    assert closed == 1, f"expected 1 closed, got {closed}"


def test_successors_are_created_before_originals_are_closed():
    """Crash-safety (pre-launch assessment): the create/update pass must run
    BEFORE the close pass, so a process killed mid-apply never leaves a
    (site, product) with its original closed and no successor."""
    existing = [
        _pr_record("rec_old", "001|PKEG1|2026-01-01", valid_from="2026-01-01")
    ]
    (_c, _u, closed), calls = _run_upsert(
        existing, [_new_rule(date(2026, 6, 1))], date(2026, 6, 1)
    )
    pricing = airtable_io.T["PricingRules"]
    ops = [op for op, t, _ in calls if t == pricing]
    first_create = ops.index("create") if "create" in ops else 10**9
    # the close pass is the update whose only field is valid_to
    close_idx = next(
        (i for i, (op, t, recs) in enumerate(calls)
         if t == pricing and op == "update"
         and all(set(r.get("fields", {}).keys()) == {"valid_to"} for r in recs)),
        -1,
    )
    assert first_create < close_idx, (
        f"successors must be created before originals are closed "
        f"(create at {first_create}, close at {close_idx})"
    )
    assert closed == 1


def test_reapplied_increase_does_not_compound():
    """Idempotence: after an increase runs, re-running (e.g. after a partial /
    timed-out apply) must NOT increase again the successors it already made."""
    from datetime import date as _date
    from reconcile import Rule
    from airtable_io import MasterSnapshot
    from master_changes import build_universal_increase

    eff = _date(2026, 7, 4)
    # A successor from a prior run: open, tenanted, valid_from == effective.
    already = Rule(
        site_id="001", product_code="PKEG1", product_desc="Keg",
        tenant_price=207.0, fb_price=124.2, retro_pct=0.0,
        valid_from=eff, valid_to=None, status="tenanted",
        reason="annual increase +3.5% (was 200.0)", source="increase:me",
    )
    snap = MasterSnapshot(
        sites={"001": {"name": "T"}}, rules=[already], site_ids={"001": "r"},
        product_ids={"PKEG1": "p"}, rule_ids={}, banner_info={},
    )
    new_rules, stats = build_universal_increase(snap, 3.5, eff, "me")
    assert new_rules == [], "a price already dated on the effective date must not be re-increased"
    assert stats["skipped_already_at_date"] == 1


TESTS = [
    test_future_dated_rule_is_not_closed,
    test_earlier_dated_rule_is_closed,
    test_same_rule_key_reupload_is_not_closed,
    test_open_rule_without_valid_from_is_still_closed,
    test_successors_are_created_before_originals_are_closed,
    test_reapplied_increase_does_not_compound,
]


def main() -> int:
    failures = 0
    for t in TESTS:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}\n      {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"ok    {t.__name__}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

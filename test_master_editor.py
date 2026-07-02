"""
Offline tests for the master-editor foundation (design §5.2, phases 0-1):

  - ``airtable_io.end_pricing_rule``  (the new targeted-close primitive)
  - ``master_changes.validate_master_change``  (pure, table-driven)
  - ``master_changes.preview_master_change``   (pure: winner-on-date, warnings)
  - ``master_changes.apply_master_change``     (dispatch onto the primitives,
    including THROUGH-THE-EDITOR re-assertions of the upsert close guard)

No Airtable network access: reuses the projection-honouring fake
``_list_all`` / recording ``_batch`` pattern (and the synthetic master
fixtures) from test_upsert_close_guard.py.

Phase 2 adds the /master* route smoke tests (FastAPI TestClient, auth
stubbed at auth_supabase._resolve_supabase, Airtable via the same fakes,
and the async cache-refresh kick disarmed so no background thread can
outlive the fakes and attempt a real network call).

Run standalone (exit 0 = pass, 1 = fail):

    python test_master_editor.py

Also discoverable by pytest, but pytest is not a project dependency.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import date, timedelta
from html import unescape

os.environ.setdefault("AIRTABLE_TOKEN", "test-token")
os.environ.setdefault("AIRTABLE_BASE_ID", "appTEST")

import airtable_io  # noqa: E402
from airtable_io import MasterSnapshot  # noqa: E402
from master_changes import (  # noqa: E402
    MasterChange,
    apply_master_change,
    change_rule_key,
    preview_master_change,
    validate_master_change,
)
from reconcile import Rule  # noqa: E402

# Reuse the close-guard test's synthetic master (same site/product fixtures).
from test_upsert_close_guard import (  # noqa: E402
    PROD_CODE,
    PROD_REC,
    PRODUCTS,
    SITE_ID,
    SITE_REC,
    SITES,
    _pr_record,
)

SITE2_ID, SITE2_REC = "002", "rec_site_002"
PR = lambda: airtable_io.T["PricingRules"]  # noqa: E731


def _key(vf: str | None) -> str:
    return f"{SITE_ID}|{PROD_CODE}|{vf or 'open'}"


class FakeAirtable:
    """In-memory Airtable: projection-honouring _list_all + recording _batch
    (the exact contract of test_upsert_close_guard's fakes), plus an
    invalidate_master_cache counter. Context manager installs/restores."""

    def __init__(self, pricing, sites=None, products=None):
        self.tables = {
            airtable_io.T["Sites"]: [dict(r) for r in (sites if sites is not None else SITES)],
            airtable_io.T["Products"]: [dict(r) for r in (products if products is not None else PRODUCTS)],
            airtable_io.T["PricingRules"]: [dict(r) for r in pricing],
        }
        self.calls: list[tuple] = []
        self.invalidations = 0

    def _list_all(self, table_id, fields=None):
        out = []
        for rec in self.tables.get(table_id, []):
            f = rec["fields"]
            if fields is not None:
                # Mimic Airtable: only the projected fields come back.
                f = {k: f[k] for k in fields if k in f}
            out.append({"id": rec["id"], "fields": dict(f)})
        return out

    def _batch(self, records, op, table_id):
        self.calls.append((op, table_id, [dict(r) for r in records]))
        if op == "create":
            created = []
            for i, r in enumerate(records):
                rec = {
                    "id": f"rec_new_{len(self.tables[table_id]) + i}",
                    "fields": dict(r["fields"]),
                }
                # Persist so follow-up lookups (e.g. auto-created sites) resolve.
                self.tables[table_id].append(rec)
                created.append(rec)
            return created
        return [{"id": r.get("id"), "fields": r.get("fields", {})} for r in records]

    def __enter__(self):
        self._orig = (
            airtable_io._list_all,
            airtable_io._batch,
            airtable_io.invalidate_master_cache,
        )
        airtable_io._list_all = self._list_all
        airtable_io._batch = self._batch

        def _inval():
            self.invalidations += 1

        airtable_io.invalidate_master_cache = _inval
        return self

    def __exit__(self, *exc):
        (
            airtable_io._list_all,
            airtable_io._batch,
            airtable_io.invalidate_master_cache,
        ) = self._orig
        return False


def _updates(fa: FakeAirtable) -> list[dict]:
    return [r for op, t, recs in fa.calls if op == "update" and t == PR() for r in recs]


def _creates(fa: FakeAirtable, table_id=None) -> list[dict]:
    tid = table_id or PR()
    return [r for op, t, recs in fa.calls if op == "create" and t == tid for r in recs]


def _rule(
    vf=None, vt=None, tenant=100.0, fb=120.0, retro=0.0,
    status="tenanted", site=SITE_ID, code=PROD_CODE, reason="orig",
) -> Rule:
    return Rule(
        site_id=site, product_code=code, product_desc="Test Keg",
        tenant_price=tenant, fb_price=fb, retro_pct=retro,
        valid_from=vf, valid_to=vt, status=status, reason=reason, source="test",
    )


def _snap(rules: list[Rule]) -> MasterSnapshot:
    """Pure MasterSnapshot over the standard synthetic site/product."""
    rule_ids = {
        airtable_io._rule_key(r.site_id, r.product_code, r.valid_from): f"rec_{i}"
        for i, r in enumerate(rules)
    }
    return MasterSnapshot(
        sites={SITE_ID: {"name": "Test Tavern", "status": "tenanted",
                         "country": "england", "notes": "", "_rec_id": SITE_REC}},
        rules=rules,
        site_ids={SITE_ID: SITE_REC},
        product_ids={PROD_CODE: PROD_REC},
        rule_ids=rule_ids,
        banner_info={},
    )


# --------------------------------------------------------------------------
# end_pricing_rule
# --------------------------------------------------------------------------

def test_end_rule_closes_open_rule():
    rec = _pr_record("rec_open", _key("2026-01-01"), valid_from="2026-01-01")
    rec["fields"]["reason"] = "original reason"
    with FakeAirtable([rec]) as fa:
        rec_id = airtable_io.end_pricing_rule(
            _key("2026-01-01"), date(2026, 7, 1), "delisted", "editor:me@x"
        )
    assert rec_id == "rec_open", f"expected rec_open, got {rec_id}"
    ups = _updates(fa)
    assert len(ups) == 1, f"expected exactly one PATCH, got {ups}"
    f = ups[0]["fields"]
    assert set(f) == {"valid_to", "reason", "source"}, (
        f"PATCH must carry valid_to+reason+source and NOTHING else, got {sorted(f)}"
    )
    assert f["valid_to"] == "2026-07-01"
    assert f["reason"] == "delisted; original reason", (
        f"reason must be prepended, not replaced: {f['reason']!r}"
    )
    assert f["source"] == "editor:me@x"
    assert fa.invalidations == 1, (
        f"end_pricing_rule must invalidate the master cache exactly once, got {fa.invalidations}"
    )


def test_end_rule_no_old_reason():
    rec = _pr_record("rec_open", _key("2026-01-01"), valid_from="2026-01-01")
    with FakeAirtable([rec]) as fa:
        airtable_io.end_pricing_rule(_key("2026-01-01"), date(2026, 7, 1), "delisted", "editor:me@x")
    assert _updates(fa)[0]["fields"]["reason"] == "delisted"


def test_end_rule_missing_key_raises():
    with FakeAirtable([]) as fa:
        try:
            airtable_io.end_pricing_rule(_key("2026-01-01"), date(2026, 7, 1), "r", "s")
        except ValueError as e:
            assert "no pricing rule" in str(e)
        else:
            raise AssertionError("missing rule_key must raise ValueError")
    assert not fa.calls or all(op != "update" for op, *_ in fa.calls), "nothing may be written"
    assert fa.invalidations == 0, "no cache invalidation on refusal"


def test_end_rule_already_ended_raises():
    rec = _pr_record("rec_done", _key("2026-01-01"), valid_from="2026-01-01", valid_to="2026-06-01")
    with FakeAirtable([rec]) as fa:
        try:
            airtable_io.end_pricing_rule(_key("2026-01-01"), date(2026, 7, 1), "r", "s")
        except ValueError as e:
            assert "already ended" in str(e)
        else:
            raise AssertionError("already-ended rule must raise ValueError")
    assert fa.invalidations == 0


def test_end_rule_inverted_interval_raises():
    rec = _pr_record("rec_open", _key("2026-06-01"), valid_from="2026-06-01")
    with FakeAirtable([rec]) as fa:
        for bad in (date(2026, 6, 1), date(2026, 1, 1)):  # equal and earlier
            try:
                airtable_io.end_pricing_rule(_key("2026-06-01"), bad, "r", "s")
            except ValueError as e:
                assert "must be after" in str(e)
            else:
                raise AssertionError(f"valid_to={bad} <= valid_from must raise ValueError")
    assert not _updates(fa) and fa.invalidations == 0


# --------------------------------------------------------------------------
# apply_master_change: price_change (incl. through-the-editor close-guard)
# --------------------------------------------------------------------------

def _price_change(d: date, tenant=182.0) -> MasterChange:
    return MasterChange(
        op="price_change", site_id=SITE_ID, product_code=PROD_CODE,
        tenant_price=tenant, valid_from=d, reason="LWC list increase",
    )


def test_price_change_closes_prior_open_and_creates_successor():
    sites = SITES + [{"id": SITE2_REC, "fields": {"site_id": SITE2_ID}}]
    other_site_rec = {
        "id": "rec_other_site",
        "fields": {
            "rule_key": f"{SITE2_ID}|{PROD_CODE}|2026-01-01",
            "valid_from": "2026-01-01",
            "site": [SITE2_REC], "product": [PROD_REC],
        },
    }
    existing = [
        _pr_record("rec_old", _key("2026-01-01"), valid_from="2026-01-01"),
        other_site_rec,
    ]
    with FakeAirtable(existing, sites=sites) as fa:
        res = apply_master_change(_price_change(date(2026, 7, 2)), "me@x")
    assert (res.created, res.updated, res.closed) == (1, 0, 1), (
        f"expected (1,0,1), got {(res.created, res.updated, res.closed)}"
    )
    closes = [u for u in _updates(fa) if set(u["fields"]) == {"valid_to"}]
    assert [u["id"] for u in closes] == ["rec_old"], (
        f"only THIS site's prior open rule may be closed (keys_in_new scoping), got {closes}"
    )
    assert closes[0]["fields"]["valid_to"] == "2026-07-02"
    created = _creates(fa)
    assert len(created) == 1
    cf = created[0]["fields"]
    assert cf["rule_key"] == _key("2026-07-02")
    assert cf["source"] == "editor:me@x", f"provenance stamp wrong: {cf.get('source')!r}"
    assert res.rule_keys_touched == [_key("2026-07-02")]


def test_price_change_does_not_close_future_dated_sibling():
    """THE close guard, re-asserted through the editor path."""
    existing = [_pr_record("rec_future", _key("2026-12-01"), valid_from="2026-12-01")]
    with FakeAirtable(existing) as fa:
        res = apply_master_change(_price_change(date(2026, 7, 2)), "me@x")
    assert res.closed == 0, f"future-dated sibling was closed (closed={res.closed})"
    assert not any(
        u["id"] == "rec_future" for u in _updates(fa)
    ), "future-dated open rule must NOT be touched"
    assert res.created == 1


def test_price_change_same_key_patches_in_place():
    """Same-key belt: re-applying the same effective date updates, not layers."""
    existing = [_pr_record("rec_same", _key("2026-07-02"), valid_from="2026-07-02")]
    with FakeAirtable(existing) as fa:
        res = apply_master_change(_price_change(date(2026, 7, 2)), "me@x")
    assert (res.created, res.updated, res.closed) == (0, 1, 0), (
        f"expected in-place PATCH (0,1,0), got {(res.created, res.updated, res.closed)}"
    )
    assert not any(set(u["fields"]) == {"valid_to"} for u in _updates(fa)), (
        "same-key rule must never be closed by its own re-apply"
    )


# --------------------------------------------------------------------------
# apply_master_change: fix_in_place / add_rule
# --------------------------------------------------------------------------

def test_fix_in_place_updates_not_creates_and_prepends_reason():
    rec = _pr_record("rec_fix", _key("2026-01-01"), valid_from="2026-01-01")
    rec["fields"]["tenant_price"] = 180.0
    rec["fields"]["reason"] = "june upload"
    change = MasterChange(
        op="fix_in_place", site_id=SITE_ID, product_code=PROD_CODE,
        tenant_price=182.0, valid_from=date(2026, 1, 1), reason="typo in upload",
    )
    with FakeAirtable([rec]) as fa:
        res = apply_master_change(change, "me@x")
    assert (res.created, res.updated, res.closed) == (0, 1, 0), (
        f"fix_in_place must PATCH in place, got {(res.created, res.updated, res.closed)}"
    )
    patched = [u for u in _updates(fa) if u["id"] == "rec_fix"]
    assert len(patched) == 1
    reason = patched[0]["fields"]["reason"]
    assert reason.startswith("corrected by me@x"), f"missing correction stamp: {reason!r}"
    assert "was £180.00" in reason, f"old figure not recorded: {reason!r}"
    assert "typo in upload" in reason
    assert reason.endswith("june upload"), (
        f"old reason must be PREPENDED to, not replaced: {reason!r}"
    )
    assert "valid_to" not in patched[0]["fields"], (
        "fix_in_place must not touch valid_to (None-strip keeps the stored value)"
    )


def test_add_rule_new_key_creates_and_autocreates_site_product():
    change = MasterChange(
        op="add_rule", site_id="999", product_code="PNEW", product_desc="New Keg",
        tenant_price=95.0, valid_from=date(2026, 7, 1), reason="new line",
        create_missing_site=True, create_missing_product=True,
    )
    with FakeAirtable([]) as fa:
        res = apply_master_change(change, "me@x")
    assert (res.created, res.updated, res.closed) == (1, 0, 0)
    site_creates = _creates(fa, airtable_io.T["Sites"])
    assert len(site_creates) == 1 and site_creates[0]["fields"] == {
        "site_id": "999", "status": "tenanted", "country": "england",
    }, f"site auto-create defaults wrong: {site_creates}"
    prod_creates = _creates(fa, airtable_io.T["Products"])
    assert len(prod_creates) == 1 and prod_creates[0]["fields"]["supplier"] == "LWC", (
        f"product auto-create defaults wrong: {prod_creates}"
    )
    created = _creates(fa)
    assert created[0]["fields"]["rule_key"] == "999|PNEW|2026-07-01"


def test_apply_end_rule_dispatches_to_end_pricing_rule():
    rec = _pr_record("rec_open", _key("2026-01-01"), valid_from="2026-01-01")
    change = MasterChange(
        op="end_rule", site_id=SITE_ID, product_code=PROD_CODE,
        valid_from=date(2026, 1, 1), valid_to=date(2026, 7, 1), reason="delist",
    )
    with FakeAirtable([rec]) as fa:
        res = apply_master_change(change, "me@x")
    assert (res.created, res.updated, res.closed) == (0, 1, 1)
    f = _updates(fa)[0]["fields"]
    assert f["valid_to"] == "2026-07-01" and f["source"] == "editor:me@x"
    assert res.rule_keys_touched == [_key("2026-01-01")]


# --------------------------------------------------------------------------
# validate_master_change — table-driven over the §2.3 invariants
# --------------------------------------------------------------------------

def _mc(**kw) -> MasterChange:
    base = dict(
        op="price_change", site_id=SITE_ID, product_code=PROD_CODE,
        tenant_price=182.0, valid_from=date(2026, 7, 2), reason="ok",
    )
    base.update(kw)
    return MasterChange(**base)


def test_validate_table():
    snap_open = _snap([_rule(vf=date(2026, 1, 1))])
    snap_ended = _snap([_rule(vf=date(2026, 1, 1), vt=date(2026, 6, 1))])
    snap_with_successor = _snap([
        _rule(vf=date(2026, 1, 1)),
        _rule(vf=date(2026, 7, 1)),  # covers the end date below
    ])
    snap_gap_successor = _snap([
        _rule(vf=date(2026, 1, 1)),
        _rule(vf=date(2026, 9, 1)),
    ])
    end_ok = dict(
        op="end_rule", tenant_price=None,
        valid_from=date(2026, 1, 1), valid_to=date(2026, 7, 1),
    )

    # (name, snap, change, expected error substring or None,
    #  expected warning substring or None)
    cases = [
        ("ok price change", snap_open, _mc(), None, None),
        ("reason required (inv 6)", snap_open, _mc(reason="  "), "reason is required", None),
        ("tenant_price required (inv 2)", snap_open, _mc(tenant_price=None), "tenant_price is required", None),
        ("tenant_price positive (inv 2)", snap_open, _mc(tenant_price=-5.0), "must be positive", None),
        ("fb_price positive (inv 2)", snap_open, _mc(fb_price=0.0), "must be positive", None),
        ("sanity band warns not blocks (inv 2)", snap_open, _mc(tenant_price=600.0), None, "sanity band"),
        ("unknown site blocks (inv 1)", snap_open, _mc(site_id="998"), "not in the master", None),
        ("unknown product blocks (inv 1)", snap_open, _mc(product_code="NOPE"), "not in the master", None),
        ("add_rule needs create opt-in (inv 1)", snap_open,
         _mc(op="add_rule", product_code="PNEW"), "tick 'create new product'", None),
        ("add_rule opt-in needs desc (inv 1)", snap_open,
         _mc(op="add_rule", product_code="PNEW", create_missing_product=True),
         "product_desc is required", None),
        ("add_rule opt-in ok, warns defaults (inv 1)", snap_open,
         _mc(op="add_rule", product_code="PNEW", product_desc="New Keg",
             create_missing_product=True), None, "auto-created"),
        ("effective date required (inv 3)", snap_open, _mc(valid_from=None),
         "effective date", None),
        ("inverted interval blocks (inv 3)", snap_open,
         _mc(valid_to=date(2026, 7, 2)), "must be after", None),
        ("key collision blocks price_change (inv 4)", snap_open,
         _mc(valid_from=date(2026, 1, 1)), "already starts on that date", None),
        ("key collision blocks add_rule (inv 4)", snap_open,
         _mc(op="add_rule", valid_from=date(2026, 1, 1)), "already starts on that date", None),
        ("collision allowed for fix_in_place (inv 4)", snap_open,
         _mc(op="fix_in_place", valid_from=date(2026, 1, 1)), None, None),
        ("fix_in_place unknown key blocks", snap_open,
         _mc(op="fix_in_place", valid_from=date(2026, 2, 2)), "no pricing rule", None),
        ("fix_in_place nothing to change", snap_open,
         _mc(op="fix_in_place", valid_from=date(2026, 1, 1), tenant_price=None),
         "nothing to change", None),
        ("fix_in_place mismatches caveat surfaced (§2.2)", snap_open,
         _mc(op="fix_in_place", valid_from=date(2026, 1, 1)), None, "duplicate mismatch"),
        ("status typo blocks pre-typecast (inv 9)", snap_open,
         _mc(status="Tenanted"), "status", None),
        ("managed warns (§2.1)", snap_open, _mc(status="managed"), None, "managed"),
        ("end_rule ok + delist warn (inv 5)", snap_open, _mc(**end_ok), None, "delist"),
        ("end_rule already ended blocks (inv 5)", snap_ended, _mc(**end_ok), "already ended", None),
        ("end_rule inverted blocks (inv 5)", snap_open,
         _mc(**{**end_ok, "valid_to": date(2026, 1, 1)}), "must be after", None),
        ("end_rule end date required (inv 5)", snap_open,
         _mc(**{**end_ok, "valid_to": None}), "end date", None),
        ("end_rule missing key blocks (inv 5)", snap_open,
         _mc(**{**end_ok, "valid_from": date(2025, 1, 1)}), "no pricing rule", None),
        ("end_rule covered by successor: no warn (inv 5)", snap_with_successor,
         _mc(**end_ok), None, None),
        ("end_rule gap to successor warns (inv 8)", snap_gap_successor,
         _mc(**end_ok), None, "gap"),
        ("add_rule after ended rule warns gap (inv 8)", snap_ended,
         _mc(op="add_rule", valid_from=date(2026, 8, 1)), None, "gap"),
        ("bad op rejected", snap_open, _mc(op="delete_rule"), "unknown op", None),
    ]

    failures = []
    for name, snap, change, err_sub, warn_sub in cases:
        errors, warnings = validate_master_change(change, snap)
        if err_sub is None:
            if errors:
                failures.append(f"{name}: unexpected errors {errors}")
        elif not any(err_sub in e for e in errors):
            failures.append(f"{name}: expected error containing {err_sub!r}, got {errors}")
        if warn_sub is not None and not any(warn_sub in w for w in warnings):
            failures.append(f"{name}: expected warning containing {warn_sub!r}, got {warnings}")
    assert not failures, "validate_master_change table failures:\n  " + "\n  ".join(failures)


def test_validate_never_forbids_overlap():
    """Overlaps are LOAD-BEARING (supports): a support layered over the open
    standard rule must validate cleanly for a different valid_from."""
    today = date.today()
    snap = _snap([
        _rule(vf=today - timedelta(days=180)),                                   # open standard
        _rule(vf=today - timedelta(days=1), vt=today + timedelta(days=30),
              status="supported"),                                                # bounded support
    ])
    change = _mc(valid_from=today + timedelta(days=7))
    errors, _warnings = validate_master_change(change, snap)
    assert not errors, f"overlapping windows must never be a blocking error: {errors}"


# --------------------------------------------------------------------------
# preview_master_change — pure
# --------------------------------------------------------------------------

def test_preview_price_change_close_create_winner():
    snap = _snap([_rule(vf=date(2026, 1, 1), tenant=180.0)])
    p = preview_master_change(_price_change(date(2026, 7, 2)), snap)
    assert not p.errors, p.errors
    assert len(p.will_close) == 1 and p.will_close[0]["valid_to"] == "2026-07-02"
    assert p.will_close[0]["rule_key"] == _key("2026-01-01")
    assert len(p.will_create) == 1 and p.will_create[0]["rule_key"] == _key("2026-07-02")
    assert "new rule wins" in p.winner_note, p.winner_note
    assert "closed at 2026-07-02" in p.summary, p.summary


def test_preview_close_pass_skips_bounded_support_and_future_rule():
    snap = _snap([
        _rule(vf=date(2026, 1, 1)),                                          # closes
        _rule(vf=date(2026, 6, 1), vt=date(2026, 8, 1), status="supported"),  # bounded: stays
        _rule(vf=date(2026, 12, 1)),                                         # future open: stays
    ])
    p = preview_master_change(_price_change(date(2026, 7, 2)), snap)
    assert [c["rule_key"] for c in p.will_close] == [_key("2026-01-01")], (
        f"only the prior open rule closes (guard mirrored), got {p.will_close}"
    )
    # On 2026-07-02 the new rule (valid_from 2026-07-02) is newest and wins.
    assert "new rule wins" in p.winner_note
    # ...but the future open rule (2026-12-01) reclaims the win from its start —
    # preview must say so, and it's a warning, not a block.
    assert not p.errors, p.errors
    assert "2026-12-01" in p.winner_note, p.winner_note
    assert any("2026-12-01" in w and "2026-07-02..2026-12-01" in w for w in p.warnings), p.warnings


def test_backdated_price_change_behind_standing_open_rule_warns_with_window():
    # The QA-found trap: change effective 2026-06-01 behind an open rule that
    # started 2026-06-15. The close pass can't close the newer rule, so the old
    # price keeps winning from 2026-06-15 — the change only bites 06-01..06-15.
    snap = _snap([_rule(vf=date(2026, 6, 15), tenant=200.0)])
    p = preview_master_change(_price_change(date(2026, 6, 1)), snap)
    assert not p.errors, p.errors  # legitimate for a scheduled future rule; warn, don't block
    assert not p.will_close, "the newer open rule must NOT be closed"
    assert any("2026-06-15" in w and "2026-06-01..2026-06-15" in w for w in p.warnings), p.warnings
    assert "2026-06-15" in p.winner_note and "only applies" in p.winner_note, p.winner_note


def test_compute_margin_math():
    from master_changes import compute_margin
    # no retro: net cost = fb; margin = tenant - fb; % of tenant
    m = compute_margin(200.0, 120.0, 0.0)
    assert m.net_cost == 120.0 and m.gross_gbp == 80.0 and m.net_gbp == 80.0
    assert abs(m.pct - 40.0) < 1e-9
    # with retro: net cost = fb*(1-retro); margin includes the rebate
    m = compute_margin(200.0, 120.0, 0.125)  # net cost 105 -> net margin 95
    assert abs(m.net_cost - 105.0) < 1e-9 and m.gross_gbp == 80.0
    assert abs(m.net_gbp - 95.0) < 1e-9 and abs(m.pct - 47.5) < 1e-9
    # selling under cost -> negative margin
    m = compute_margin(100.0, 120.0, 0.0)
    assert m.net_gbp == -20.0 and abs(m.pct - (-20.0)) < 1e-9
    # partial data / div-by-zero guards -> None, never raises
    assert compute_margin(200.0, None, 0.0).net_gbp is None
    assert compute_margin(None, 120.0, 0.0).net_gbp is None
    assert compute_margin(0.0, 120.0, 0.0).pct is None  # no divide-by-zero
    # managed-style zero margin
    assert compute_margin(120.0, 120.0, 0.0).net_gbp == 0.0


def test_retro_pct_ge_one_is_blocked():
    # retro >= the full FB list price (fraction >= 1) would make the net price
    # zero/negative — block it rather than warn-through. (The £ form converts to
    # a fraction before this runs, so this guards a mistyped/huge figure.)
    snap = _snap([_rule(vf=date(2026, 1, 1))])
    change = MasterChange(
        op="price_change", site_id=SITE_ID, product_code=PROD_CODE,
        tenant_price=100.0, fb_price=120.0, retro_pct=12.5,
        valid_from=date(2026, 7, 2), reason="typo test",
    )
    errors, _ = validate_master_change(change, snap)
    assert any(
        "at or above the FB list price" in e or "net price would be zero" in e
        for e in errors
    ), errors
    # a proper fraction is fine
    change2 = replace(change, retro_pct=0.125)
    errors2, _ = validate_master_change(change2, snap)
    assert not any("retro" in e.lower() for e in errors2), errors2


def test_retro_gbp_form_converts_to_fraction():
    # The edit/add forms collect retro as £/keg (retro_gbp); the codec stores it
    # as a fraction of the FB list price. £22.50 on a £180 list -> 0.125.
    from master_pages import parse_master_change_form
    base = {
        "op": "price_change", "site_id": SITE_ID, "product_code": PROD_CODE,
        "tenant_price": "200", "fb_price": "180", "retro_gbp": "22.50",
        "status": "tenanted", "valid_from": "2026-07-02", "reason": "retro £ test",
    }
    change, errs = parse_master_change_form(base)
    assert not errs, errs
    assert change.retro_pct is not None and abs(change.retro_pct - 0.125) < 1e-12

    # The confirm→apply path carries the exact fraction (retro_pct hidden field);
    # it must win over any stale £ field so the round-trip stays bit-exact.
    both = {**base, "retro_pct": "0.1"}
    change2, _ = parse_master_change_form(both)
    assert change2.retro_pct == 0.1

    # £ retro with no list price to divide by -> a loud error, never a silent drop.
    no_fb = {k: v for k, v in base.items() if k != "fb_price"}
    change3, errs3 = parse_master_change_form(no_fb)
    assert any("FB list price" in e for e in errs3), errs3

    # Blank retro -> None (the "keep existing" / "no retro" path), no error.
    blank = {**base, "retro_gbp": ""}
    change4, errs4 = parse_master_change_form(blank)
    assert change4.retro_pct is None and not errs4


def test_preview_winner_overlapping_support_wins_today():
    """§5.2: winner-on-date with an overlapping support rule — newest
    valid_from first, mirroring reconcile._index_rules."""
    today = date.today()
    std_vf = today - timedelta(days=180)
    sup_vf = today - timedelta(days=1)
    snap = _snap([
        _rule(vf=std_vf, tenant=180.0),
        _rule(vf=sup_vf, vt=today + timedelta(days=30), tenant=150.0, status="supported"),
    ])
    change = MasterChange(
        op="fix_in_place", site_id=SITE_ID, product_code=PROD_CODE,
        tenant_price=182.0, valid_from=std_vf, reason="fix",
    )
    p = preview_master_change(change, snap)
    sup_key = _key(sup_vf.isoformat())
    assert sup_key in p.winner_note and "wins" in p.winner_note, (
        f"the overlapping support (newest valid_from) must be named the winner: {p.winner_note!r}"
    )


def test_preview_end_rule_delist_and_takeover():
    # No successor: delist warning + membership note.
    snap = _snap([_rule(vf=date(2026, 1, 1))])
    change = MasterChange(
        op="end_rule", site_id=SITE_ID, product_code=PROD_CODE,
        valid_from=date(2026, 1, 1), valid_to=date(2026, 7, 1), reason="delist",
    )
    p = preview_master_change(change, snap)
    assert not p.errors, p.errors
    assert len(p.will_update) == 1 and p.will_update[0]["new"]["valid_to"] == "2026-07-01"
    assert not p.will_create and not p.will_close, "end_rule creates/closes nothing else"
    assert "drops off" in p.winner_note, p.winner_note
    assert any("delist" in w for w in p.warnings), p.warnings
    # With a covering successor: it takes over, no delist warning.
    snap2 = _snap([_rule(vf=date(2026, 1, 1)), _rule(vf=date(2026, 7, 1))])
    p2 = preview_master_change(change, snap2)
    assert _key("2026-07-01") in p2.winner_note and "takes over" in p2.winner_note
    assert not any("delist" in w for w in p2.warnings), p2.warnings


def test_change_rule_key_normalises_codes():
    # _to_str_code: floats lose the trailing .0 (Excel artefact), strings are stripped.
    change = _mc(site_id=809.0, product_code=" PKEG1 ", valid_from=None)
    assert change_rule_key(change) == "809|PKEG1|open"


# --------------------------------------------------------------------------
# Route smoke tests (design §5.2 last bullet) — FastAPI TestClient, offline.
#
# The TestClient is deliberately NOT used as a context manager: starlette only
# runs lifespan (and webapp's master-cache warm-up thread, which would fetch
# through whatever _list_all is installed at the time) inside `with`; skipping
# it keeps requests strictly inside the FakeAirtable window.
# --------------------------------------------------------------------------

def _reset_master_cache() -> None:
    """Blow away the real module-level snapshot cache so each route test reads
    through the CURRENT fakes (FakeAirtable's patched invalidate_master_cache
    is a counter and does not clear the real cache)."""
    with airtable_io._MASTER_CACHE_LOCK:
        airtable_io._MASTER_CACHE["snapshot"] = None
        airtable_io._MASTER_CACHE["ts"] = 0.0
        airtable_io._MASTER_CACHE["gen"] += 1


class FakeAuthClient:
    """Context manager yielding a TestClient with:
      - auth stubbed: auth_supabase._resolve_supabase returns a principal of
        the given role (the require_drinks_role closure looks it up at call
        time in the auth_supabase module namespace);
      - webapp.refresh_master_cache_async disarmed (a background rebuild
        thread could outlive the FakeAirtable window and hit the network);
      - the real master cache reset on entry and exit.
    """

    EMAIL = "tester@example.com"

    def __init__(self, role: str):
        self.role = role

    def __enter__(self):
        import auth_supabase
        import webapp
        from fastapi.testclient import TestClient

        self._auth = auth_supabase
        self._webapp = webapp
        self._orig_resolve = auth_supabase._resolve_supabase
        self._orig_refresh = webapp.refresh_master_cache_async
        role, email = self.role, self.EMAIL
        auth_supabase._resolve_supabase = lambda request: (
            auth_supabase.DrinksPrincipal(email=email, role=role),
            None,
        )
        webapp.refresh_master_cache_async = lambda: None
        _reset_master_cache()
        # raise_server_exceptions=False: unhandled errors run through webapp's
        # own 500 handler (as in prod) instead of failing the test harness.
        return TestClient(webapp.app, raise_server_exceptions=False)

    def __exit__(self, *exc):
        self._auth._resolve_supabase = self._orig_resolve
        self._webapp.refresh_master_cache_async = self._orig_refresh
        _reset_master_cache()
        return False


def _extract_hidden(html_text: str) -> dict[str, str]:
    """Pull the confirm page's hidden inputs (we control the renderer, so a
    regex over the fixed attribute order is reliable)."""
    return {
        unescape(m.group(1)): unescape(m.group(2))
        for m in re.finditer(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)"', html_text
        )
    }


def _grid_rule(rec_id: str = "rec_open", vf: str = "2026-01-01") -> dict:
    rec = _pr_record(rec_id, _key(vf), valid_from=vf)
    rec["fields"]["tenant_price"] = 180.0
    rec["fields"]["status"] = "tenanted"
    return rec


def test_route_grid_viewer_ok_edit_admin_only():
    """Viewer can GET /master (no edit links); mutating pages are admin-gated;
    admin sees the links and the edit page renders both forms."""
    with FakeAirtable([_grid_rule()]):
        with FakeAuthClient("viewer") as client:
            r = client.get("/master")
            assert r.status_code == 200, r.text[:300]
            assert PROD_CODE in r.text
            assert "/master/edit" not in r.text, "edit links must be hidden from viewers"
            r2 = client.get("/master/edit", params={"rule_key": _key("2026-01-01")})
            assert r2.status_code == 403, f"viewer must get 403 on /master/edit, got {r2.status_code}"
        with FakeAuthClient("admin") as client:
            r3 = client.get("/master")
            assert r3.status_code == 200 and "/master/edit" in r3.text
            r4 = client.get("/master/edit", params={"rule_key": _key("2026-01-01")})
            assert r4.status_code == 200
            assert "Change price from a date" in r4.text and "Fix a mistake" in r4.text
            # status prefill: a price-only fix must not silently reset status
            assert '<option value="tenanted" selected>' in r4.text


def test_route_end_add_forms_and_404():
    with FakeAirtable([_grid_rule()]):
        with FakeAuthClient("admin") as client:
            r = client.get("/master/end", params={"rule_key": _key("2026-01-01")})
            assert r.status_code == 200 and "delist" in r.text.lower()
            r2 = client.get("/master/add")
            assert r2.status_code == 200 and "create this product" in r2.text.lower()
            r3 = client.get("/master/edit", params={"rule_key": "999|NOPE|open"})
            assert r3.status_code == 404, f"stale rule_key must 404 politely, got {r3.status_code}"


def test_route_preview_apply_roundtrip():
    """§5.2: admin preview→apply carries the change intact through the hidden
    inputs; preview writes NOTHING; apply closes the prior rule, creates the
    successor, and stamps provenance from the signed-in principal."""
    form = {
        "op": "price_change", "site_id": SITE_ID, "product_code": PROD_CODE,
        "tenant_price": "182.0", "status": "tenanted",
        "valid_from": "2026-07-02", "reason": "LWC list increase Jul-26",
    }
    with FakeAirtable([_grid_rule("rec_old")]) as fa:
        with FakeAuthClient("admin") as client:
            r = client.post("/master/preview", data=form)
            assert r.status_code == 200, r.text[:500]
            assert "Confirm change" in r.text
            assert not fa.calls, f"preview must write NOTHING, got {fa.calls}"
            hidden = _extract_hidden(r.text)
            assert hidden["op"] == "price_change"
            assert hidden["tenant_price"] == "182.0"
            assert hidden["valid_from"] == "2026-07-02"
            assert hidden["reason"] == "LWC list increase Jul-26", (
                f"change must round-trip the confirm page intact: {hidden}"
            )
            r2 = client.post("/master/apply", data=hidden)
            assert r2.status_code == 200, r2.text[:500]
    closes = [u for u in _updates(fa) if set(u["fields"]) == {"valid_to"}]
    assert [u["id"] for u in closes] == ["rec_old"]
    assert closes[0]["fields"]["valid_to"] == "2026-07-02"
    created = _creates(fa)
    assert len(created) == 1 and created[0]["fields"]["rule_key"] == _key("2026-07-02")
    assert created[0]["fields"]["source"] == f"editor:{FakeAuthClient.EMAIL}", (
        f"provenance stamp wrong: {created[0]['fields'].get('source')!r}"
    )
    assert "Rules created" in r2.text and "Back to master" in r2.text


def test_route_apply_revalidates_tampered_hidden_inputs():
    """/master/apply must re-validate server-side: a tampered/stale hidden
    form (key collision for op=price_change) is refused with nothing written."""
    data = {
        "op": "price_change", "site_id": SITE_ID, "product_code": PROD_CODE,
        "tenant_price": "182.0", "status": "tenanted",
        "valid_from": "2026-01-01",  # collides with the existing rule's key
        "reason": "tampered",
    }
    with FakeAirtable([_grid_rule("rec_old")]) as fa:
        with FakeAuthClient("admin") as client:
            r = client.post("/master/apply", data=data)
            assert r.status_code == 400, f"expected 400 refusal, got {r.status_code}"
            assert "already starts on that date" in r.text
    assert not fa.calls, f"a refused apply must write NOTHING, got {fa.calls}"
    assert fa.invalidations == 0


def test_route_cross_origin_post_rejected():
    """§3.3: _is_cross_origin on BOTH mutating POSTs — a foreign Origin is
    rejected before any parsing or writing."""
    with FakeAirtable([_grid_rule()]) as fa:
        with FakeAuthClient("admin") as client:
            for path in ("/master/preview", "/master/apply"):
                r = client.post(
                    path,
                    data={"op": "end_rule", "site_id": SITE_ID,
                          "product_code": PROD_CODE, "valid_from": "2026-01-01",
                          "valid_to": "2026-07-01", "reason": "csrf"},
                    headers={"Origin": "https://evil.example"},
                )
                assert r.status_code == 403, f"{path}: expected 403, got {r.status_code}"
    assert not fa.calls, "cross-origin POSTs must never reach a write"


TESTS = [
    test_end_rule_closes_open_rule,
    test_end_rule_no_old_reason,
    test_end_rule_missing_key_raises,
    test_end_rule_already_ended_raises,
    test_end_rule_inverted_interval_raises,
    test_price_change_closes_prior_open_and_creates_successor,
    test_price_change_does_not_close_future_dated_sibling,
    test_price_change_same_key_patches_in_place,
    test_fix_in_place_updates_not_creates_and_prepends_reason,
    test_add_rule_new_key_creates_and_autocreates_site_product,
    test_apply_end_rule_dispatches_to_end_pricing_rule,
    test_validate_table,
    test_validate_never_forbids_overlap,
    test_preview_price_change_close_create_winner,
    test_preview_close_pass_skips_bounded_support_and_future_rule,
    test_backdated_price_change_behind_standing_open_rule_warns_with_window,
    test_compute_margin_math,
    test_retro_pct_ge_one_is_blocked,
    test_retro_gbp_form_converts_to_fraction,
    test_preview_winner_overlapping_support_wins_today,
    test_preview_end_rule_delist_and_takeover,
    test_change_rule_key_normalises_codes,
    test_route_grid_viewer_ok_edit_admin_only,
    test_route_end_add_forms_and_404,
    test_route_preview_apply_roundtrip,
    test_route_apply_revalidates_tampered_hidden_inputs,
    test_route_cross_origin_post_rejected,
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

"""
master_changes.py — the single seam through which every pricing-master edit flows.

Design: docs/master-editor-design.md §3.1. Every editor mutation (and, in
phase 2, every approved email-extracted change) is expressed as a
``MasterChange`` and applied by ``apply_master_change`` — the ONLY apply path.

Layering:
  - ``validate_master_change`` / ``preview_master_change`` are PURE (no I/O):
    they read only the ``MasterChange`` and a ``MasterSnapshot``. They power
    the /master/preview confirm page, which doubles as the phase-2 review
    screen.
  - ``apply_master_change`` is a thin dispatch onto the airtable_io write
    primitives (``upsert_pricing_rules`` / ``end_pricing_rule``). It does not
    duplicate their close/guard logic. Callers must run
    ``validate_master_change`` first and refuse to apply on blocking errors
    (the /master/apply route re-validates; phase-2 Approve must too).

Load-bearing semantics preserved here (design §2.1):
  - half-open matching: valid_from <= d < valid_to; missing bounds are open.
  - overlaps are LEGAL and load-bearing (supports layer over standing rules);
    we never forbid them — the preview instead names which rule WINS on the
    effective date (newest valid_from first, mirroring reconcile._index_rules).
  - rule_key = "site|product|valid_from-iso" (or "open"); a key collision is
    an in-place PATCH in the upsert, so it is allowed ONLY for the explicit
    fix_in_place op and rejected otherwise.
  - retro_pct is a fraction stored to 10dp on purpose — NEVER rounded here.
  - fb_price is the LIST price (cost-file col 2), never Net.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import airtable_io
from airtable_io import MasterSnapshot
from reconcile import Rule, _to_str_code

MasterOp = Literal["price_change", "fix_in_place", "end_rule", "add_rule"]

VALID_OPS = ("price_change", "fix_in_place", "end_rule", "add_rule")

# Validate BEFORE write: _batch uses typecast=True, which would silently mint
# a new select option in Airtable from a typo (design §2.3 invariant 9).
VALID_STATUSES = ("tenanted", "managed", "supported")

# Warn — don't block — outside this £/keg band (design §2.3 invariant 2).
PRICE_SANITY_BAND = (20.0, 500.0)


# ---------------------------------------------------------------------------
# Margin (pure) — the analytics overlay for the editor
# ---------------------------------------------------------------------------
#
# fb_price = FB's list/buying price per keg; retro_pct = the supplier rebate as
# a fraction of fb_price, so FB's NET COST after the retro = fb_price*(1-retro)
# (this is master_export's "Net price"). The margin FB actually makes selling to
# the tenant is tenant_price - net_cost; the pre-retro (cash-at-purchase) margin
# is tenant_price - fb_price. pct is the retro-inclusive gross margin as a
# percentage of the selling (tenant) price. Any input None => that figure None,
# so partial rules (e.g. tenant-only) simply show blanks.

@dataclass(frozen=True)
class Margin:
    net_cost: float | None      # fb_price * (1 - retro_pct)
    gross_gbp: float | None     # tenant_price - fb_price (pre-retro £/keg)
    net_gbp: float | None       # tenant_price - net_cost  (true £/keg, retro-incl)
    pct: float | None           # net_gbp / tenant_price * 100 (of selling price)


def compute_margin(
    tenant_price: float | None,
    fb_price: float | None,
    retro_pct: float | None = 0.0,
) -> Margin:
    """FB's margin for one (site, product) rule. Pure; never raises."""
    retro = retro_pct or 0.0
    net_cost = fb_price * (1.0 - retro) if fb_price is not None else None
    gross = (tenant_price - fb_price) if (tenant_price is not None and fb_price is not None) else None
    net = (tenant_price - net_cost) if (tenant_price is not None and net_cost is not None) else None
    pct = (net / tenant_price * 100.0) if (net is not None and tenant_price) else None
    return Margin(net_cost=net_cost, gross_gbp=gross, net_gbp=net, pct=pct)


def margin_of(rule) -> Margin:
    """Convenience: compute_margin over a Rule's fields."""
    return compute_margin(rule.tenant_price, rule.fb_price, rule.retro_pct)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class MasterChange:
    """One proposed edit to the pricing master (design §3.1).

    ``valid_from`` is the effective date for price_change/add_rule and the
    identity (key) date of the target rule for fix_in_place/end_rule — it may
    be None for a legacy "site|product|open" key. ``valid_to`` is the end date
    for end_rule (and, if ever set on the other ops, a bounded window —
    validated valid_to > valid_from).

    ``create_missing_site`` / ``create_missing_product`` carry the add-rule
    form's explicit "create new site/product" opt-in (design §2.3 invariant 1);
    without them missing site/product is a blocking error.
    """
    op: MasterOp
    site_id: str                      # normalised via _to_str_code
    product_code: str
    product_desc: str | None = None   # add_rule only (Products.description)
    tenant_price: float | None = None
    fb_price: float | None = None
    retro_pct: float | None = None    # fraction, 10dp, never rounded
    status: str = "tenanted"
    valid_from: date | None = None
    valid_to: date | None = None
    reason: str = ""                  # required, non-empty, on EVERY mutation
    source_note: str = ""             # "editor" now, "email:<message_id>" in phase 2
    create_missing_site: bool = False   # add_rule only
    create_missing_product: bool = False


@dataclass
class ChangePreview:
    """Pure computation of what an apply WOULD do — renders the confirm page
    and the phase-2 review screen. ``errors`` non-empty => not applyable."""
    op: str
    rule_key: str                     # key of the rule written / targeted
    summary: str
    will_close: list[dict] = field(default_factory=list)   # {rule_key, valid_from, valid_to}
    will_create: list[dict] = field(default_factory=list)  # new-row field dicts
    will_update: list[dict] = field(default_factory=list)  # in-place PATCH descriptions
    winner_note: str = ""             # which rule wins on the effective date
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ChangeResult:
    """What apply_master_change actually did (counts from the primitives)."""
    created: int
    updated: int
    closed: int
    rule_keys_touched: list[str]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _norm(change: MasterChange) -> tuple[str, str]:
    return _to_str_code(change.site_id), _to_str_code(change.product_code)


def change_rule_key(change: MasterChange) -> str:
    """The rule_key this change writes/targets (airtable_io._rule_key semantics)."""
    site, code = _norm(change)
    return airtable_io._rule_key(site, code, change.valid_from)


def _key_of(rule: Rule) -> str:
    return airtable_io._rule_key(rule.site_id, rule.product_code, rule.valid_from)


def _rules_for_key(snap: MasterSnapshot, site: str, code: str) -> list[Rule]:
    """All rules for (site, product), newest valid_from first — the same order
    reconcile._index_rules uses, so 'first containing window wins' holds."""
    rules = [r for r in snap.rules if r.site_id == site and r.product_code == code]
    rules.sort(key=lambda r: r.valid_from or date.min, reverse=True)
    return rules


def _find_rule(snap: MasterSnapshot, site: str, code: str, valid_from: date | None) -> Rule | None:
    for r in _rules_for_key(snap, site, code):
        if r.valid_from == valid_from:
            return r
    return None


def _contains(vf: date | None, vt: date | None, d: date) -> bool:
    """Half-open interval membership, missing bounds open (reconcile.py:611-613)."""
    return (vf or date.min) <= d < (vt or date.max)


def _winner_on(candidates: list[tuple[str, date | None, date | None]], on: date) -> str | None:
    """First containing window, newest valid_from first. candidates: (label, vf, vt)."""
    ordered = sorted(candidates, key=lambda c: c[1] or date.min, reverse=True)
    for label, vf, vt in ordered:
        if _contains(vf, vt, on):
            return label
    return None


# ---------------------------------------------------------------------------
# validate_master_change — design §2.3 invariants, pure
# ---------------------------------------------------------------------------

def validate_master_change(
    change: MasterChange, snap: MasterSnapshot
) -> tuple[list[str], list[str]]:
    """Returns (blocking_errors, warnings). Warnings never block an apply —
    they are surfaced on the confirm page (sanity band, gap/delist, etc.)."""
    errors: list[str] = []
    warnings: list[str] = []

    if change.op not in VALID_OPS:
        return [f"unknown op {change.op!r} (expected one of {', '.join(VALID_OPS)})"], []

    site, code = _norm(change)
    if not site:
        errors.append("site_id is missing")
    if not code:
        errors.append("product_code is missing")

    # Invariant 6: reason non-empty on EVERY mutation.
    if not (change.reason or "").strip():
        errors.append("reason is required on every change")

    site_known = site in snap.sites
    prod_known = code in snap.product_ids

    # Invariant 1: site/product must exist — add_rule may explicitly opt into create.
    if change.op == "add_rule":
        if site and not site_known:
            if change.create_missing_site:
                warnings.append(
                    f"site {site!r} will be auto-created with defaults "
                    "status=tenanted, country=england"
                )
            else:
                errors.append(
                    f"site_id {site!r} is not in the master — tick 'create new site' to add it"
                )
        if code and not prod_known:
            if not change.create_missing_product:
                errors.append(
                    f"product_code {code!r} is not in the master — "
                    "tick 'create new product' to add it"
                )
            elif not (change.product_desc or "").strip():
                errors.append("product_desc is required when creating a new product")
            else:
                warnings.append(
                    f"product {code!r} will be auto-created with default supplier=LWC"
                )
    else:
        if site and not site_known:
            errors.append(f"site_id {site!r} is not in the master")
        if code and not prod_known:
            errors.append(f"product_code {code!r} is not in the master")

    key = change_rule_key(change)
    key_exists = key in snap.rule_ids
    target = _find_rule(snap, site, code, change.valid_from)

    if change.op == "end_rule":
        # Invariant 5 + §3.2 guards (re-checked at apply time by end_pricing_rule).
        if change.valid_to is None:
            errors.append("end date (valid_to) is required")
        if not key_exists and target is None:
            errors.append(f"no pricing rule with key {key!r}")
        elif target is None:
            errors.append(
                f"rule {key!r} exists but its site/product links cannot be resolved — "
                "fix the record in Airtable"
            )
        else:
            if target.valid_to is not None:
                errors.append(
                    f"rule {key!r} is already ended at {target.valid_to.isoformat()} — "
                    "only open rules can be ended"
                )
            if change.valid_to is not None and target.valid_from is not None \
                    and change.valid_to <= target.valid_from:
                errors.append(
                    f"end date ({change.valid_to.isoformat()}) must be after the rule's "
                    f"valid_from ({target.valid_from.isoformat()})"
                )
            if change.valid_to is not None and target.valid_to is None:
                others = [r for r in _rules_for_key(snap, site, code) if r is not target]
                covered = any(_contains(r.valid_from, r.valid_to, change.valid_to) for r in others)
                if not covered:
                    future_starts = [
                        r.valid_from for r in others
                        if r.valid_from is not None and r.valid_from > change.valid_to
                    ]
                    if future_starts:
                        warnings.append(
                            f"gap: no rule covers {change.valid_to.isoformat()} to "
                            f"{min(future_starts).isoformat()} — deliveries in that window "
                            "will flag as missing"
                        )
                    else:
                        warnings.append(
                            "no successor rule exists for this (site, product) — future "
                            "deliveries will flag as missing. Is this a delist?"
                        )
        return errors, warnings

    # ---- writing ops: price_change / fix_in_place / add_rule ----

    # Invariant 9: status vocabulary, BEFORE typecast=True can mint an option.
    if change.status not in VALID_STATUSES:
        errors.append(
            f"status {change.status!r} is not one of {', '.join(VALID_STATUSES)}"
        )
    elif change.status == "managed":
        warnings.append(
            "status=managed: managed rules skip price checks and assert zero margin "
            "(reconcile.py behaviour, not just a label)"
        )

    # Invariant 2: prices positive; sanity band warns only.
    if change.op in ("price_change", "add_rule") and change.tenant_price is None:
        errors.append("tenant_price is required")
    for label, value in (("tenant_price", change.tenant_price), ("fb_price", change.fb_price)):
        if value is None:
            continue
        if value <= 0:
            errors.append(f"{label} must be positive (got {value})")
        elif not (PRICE_SANITY_BAND[0] <= value <= PRICE_SANITY_BAND[1]):
            warnings.append(
                f"{label} £{value:.2f} is outside the £{PRICE_SANITY_BAND[0]:.0f}–"
                f"£{PRICE_SANITY_BAND[1]:.0f}/keg sanity band — double-check before confirming"
            )
    if change.retro_pct is not None:
        if change.retro_pct < 0:
            errors.append("retro must not be negative")
        elif change.retro_pct >= 1:
            # retro >= the full FB list price: a >=100% rebate is never valid and
            # would make the net price zero or negative. Almost always a mistyped
            # figure. Block it.
            if change.fb_price:
                errors.append(
                    f"retro £{change.retro_pct * change.fb_price:.2f}/keg is at or above the "
                    f"FB list price £{change.fb_price:.2f} — the net price would be zero or "
                    "negative. Check the retro figure."
                )
            else:
                errors.append(
                    "retro is at or above the FB list price (net price would be zero or "
                    "negative) — check the retro figure."
                )

    # Invariant 3: effective date required for the dated ops; intervals never
    # inverted/empty (dates themselves are parsed at the form layer).
    if change.op in ("price_change", "add_rule") and change.valid_from is None:
        errors.append("effective date (valid_from) is required")
    if change.valid_from is not None and change.valid_to is not None \
            and change.valid_to <= change.valid_from:
        errors.append(
            f"valid_to ({change.valid_to.isoformat()}) must be after valid_from "
            f"({change.valid_from.isoformat()})"
        )

    # Invariant 4: rule_key collision => in-place PATCH in the upsert, so it is
    # allowed ONLY for the explicit fix_in_place op.
    if change.op in ("price_change", "add_rule") and key_exists:
        errors.append(
            f"a rule already starts on that date ({key}) — edit it in place instead"
        )
    if change.op == "fix_in_place":
        if not key_exists and target is None:
            errors.append(f"no pricing rule with key {key!r} to fix")
        elif target is not None:
            if change.status != target.status:
                warnings.append(
                    f"this also changes status {target.status!r} → {change.status!r}"
                )
            if (target.retro_pct or 0) > 0 and change.retro_pct == 0.0:
                _keep = (
                    f"£{target.retro_pct * target.fb_price:.2f}/keg"
                    if target.fb_price else "the current retro"
                )
                warnings.append(
                    "a retro of £0 cannot clear the existing retro (the write path skips "
                    f"zero values) — leave it blank to keep {_keep}"
                )
            if change.tenant_price is None and change.fb_price is None \
                    and change.retro_pct is None and change.status == target.status:
                errors.append("nothing to change — set at least one field")
        # §2.2 caveat: history rewrite does not recompute persisted Mismatches.
        warnings.append(
            "fix-in-place rewrites history: already-persisted Mismatches rows are NOT "
            "recomputed, and re-uploading an affected weekly file will create duplicate "
            "mismatch rows"
        )

    # A newer OPEN rule survives the close pass (which only closes open rules
    # with valid_from < D) and — being the newest valid_from — keeps WINNING from
    # its own start onward. This is legitimate when a future-dated rule is
    # already scheduled, and a silent trap when the operator meant to change the
    # CURRENT price by backdating behind a standing rule. Warn loudly either way
    # (the preview states the exact window) rather than block a real workflow.
    if change.op in ("price_change", "add_rule") and change.valid_from is not None:
        newer_open = [
            r for r in _rules_for_key(snap, site, code)
            if r.valid_to is None and _key_of(r) != key
            and r.valid_from is not None and r.valid_from > change.valid_from
        ]
        if newer_open:
            vf = min(r.valid_from for r in newer_open)
            warnings.append(
                f"a newer open rule starts {vf.isoformat()} and will NOT be closed by this "
                f"change — it keeps winning from {vf.isoformat()} onward, so this only takes "
                f"effect {change.valid_from.isoformat()}..{vf.isoformat()}. To change the price "
                f"from now on, end or fix the {vf.isoformat()} rule instead."
            )

    # Invariant 8 (gaps warned): closing at D + opening at D is gapless, but if
    # nothing is open for this key and the latest ended rule stops before D,
    # the window in between matches nothing.
    if change.op in ("price_change", "add_rule") and change.valid_from is not None \
            and not key_exists:
        siblings = _rules_for_key(snap, site, code)
        if siblings and not any(
            r.valid_to is None and (r.valid_from is None or r.valid_from < change.valid_from)
            for r in siblings
        ):
            ended_before = [
                r.valid_to for r in siblings
                if r.valid_to is not None and r.valid_to < change.valid_from
            ]
            if ended_before and not any(
                _contains(r.valid_from, r.valid_to, change.valid_from) for r in siblings
            ):
                warnings.append(
                    f"gap: the previous rule ended {max(ended_before).isoformat()} and "
                    f"nothing covers up to {change.valid_from.isoformat()}"
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# preview_master_change — pure; the confirm page / phase-2 review screen
# ---------------------------------------------------------------------------

def preview_master_change(change: MasterChange, snap: MasterSnapshot) -> ChangePreview:
    errors, warnings = validate_master_change(change, snap)
    site, code = _norm(change)
    key = change_rule_key(change)
    rules = _rules_for_key(snap, site, code)

    will_close: list[dict] = []
    will_create: list[dict] = []
    will_update: list[dict] = []
    winner_note = ""
    summary = ""

    def _desc(r: Rule) -> dict:
        return {
            "rule_key": _key_of(r),
            "valid_from": r.valid_from.isoformat() if r.valid_from else None,
            "valid_to": r.valid_to.isoformat() if r.valid_to else None,
            "tenant_price": r.tenant_price,
            "fb_price": r.fb_price,
            "retro_pct": r.retro_pct,
            "status": r.status,
        }

    if change.op in ("price_change", "add_rule"):
        d = change.valid_from
        if d is not None:
            # Mirror the upsert close pass (airtable_io.py:496-525): open rules
            # for this (site, product) whose valid_from is BEFORE d, excluding
            # the same-key belt. Bounded overlaps (supports) are never closed.
            closed_keys: set[str] = set()
            for r in rules:
                if r.valid_to is None and _key_of(r) != key \
                        and (r.valid_from is None or r.valid_from < d):
                    closed_keys.add(_key_of(r))
                    will_close.append({**_desc(r), "valid_to": d.isoformat()})
            will_create.append({
                "rule_key": key,
                "valid_from": d.isoformat(),
                "valid_to": change.valid_to.isoformat() if change.valid_to else None,
                "tenant_price": change.tenant_price,
                "fb_price": change.fb_price,
                "retro_pct": change.retro_pct,
                "status": change.status,
            })
            candidates = [("new rule", d, change.valid_to)] + [
                (_key_of(r), r.valid_from, d if _key_of(r) in closed_keys else r.valid_to)
                for r in rules
            ]
            winner = _winner_on(candidates, d)
            # A newer OPEN sibling (valid_from > d) is not closed by the pass and
            # reclaims the win from its own start — the change only bites d..vf.
            newer_open = [
                r for r in rules
                if r.valid_to is None and _key_of(r) != key
                and r.valid_from is not None and r.valid_from > d
            ]
            if winner == "new rule":
                winner_note = f"On {d.isoformat()} the new rule wins."
            elif winner:
                winner_note = (
                    f"On {d.isoformat()} rule {winner} wins (newest valid_from first) — "
                    "the new rule takes over when that window ends."
                )
            if newer_open:
                vf = min(r.valid_from for r in newer_open)
                winner_note += (
                    f" ⚠ From {vf.isoformat()} the existing rule starting {vf.isoformat()} "
                    f"overrides this change — it only applies {d.isoformat()}..{vf.isoformat()}."
                )
            price = f"£{change.tenant_price:.2f}" if change.tenant_price is not None else "unset"
            if will_close:
                summary = (
                    f"Rule {will_close[0]['rule_key']} will be closed at {d.isoformat()} · "
                    f"new rule created from {d.isoformat()} at {price}"
                )
            else:
                summary = f"New rule {key} created from {d.isoformat()} at {price}"

    elif change.op == "fix_in_place":
        target = _find_rule(snap, site, code, change.valid_from)
        if target is not None:
            new_desc = dict(_desc(target))
            if change.tenant_price is not None:
                new_desc["tenant_price"] = change.tenant_price
            if change.fb_price is not None:
                new_desc["fb_price"] = change.fb_price
            if change.retro_pct:
                new_desc["retro_pct"] = change.retro_pct
            new_desc["status"] = change.status
            will_update.append({"old": _desc(target), "new": new_desc})
        on = date.today()
        winner = _winner_on([(_key_of(r), r.valid_from, r.valid_to) for r in rules], on)
        if winner == key:
            winner_note = f"On {on.isoformat()} this rule wins (newest valid_from first)."
        elif winner:
            winner_note = (
                f"On {on.isoformat()} rule {winner} wins (newest valid_from first) — "
                "the rule you are fixing is currently overridden on that date."
            )
        summary = f"Rule {key} will be rewritten IN PLACE (history rewrite — the old figure is treated as never true)"

    elif change.op == "end_rule":
        target = _find_rule(snap, site, code, change.valid_from)
        d = change.valid_to
        if target is not None and d is not None:
            will_update.append({"old": _desc(target), "new": {**_desc(target), "valid_to": d.isoformat()}})
            others = [
                (_key_of(r), r.valid_from, r.valid_to) for r in rules if r is not target
            ]
            winner = _winner_on(others, d)
            if winner:
                winner_note = f"On {d.isoformat()} rule {winner} takes over."
            else:
                winner_note = (
                    f"No rule covers {d.isoformat()} onwards — the (site, product) drops "
                    "off active membership immediately."
                )
            summary = f"Rule {key} will be ended at {d.isoformat()} (nothing created)"

    if change.reason:
        summary = f"{summary} · Reason: {change.reason!r}" if summary else f"Reason: {change.reason!r}"

    return ChangePreview(
        op=change.op,
        rule_key=key,
        summary=summary,
        will_close=will_close,
        will_create=will_create,
        will_update=will_update,
        winner_note=winner_note,
        warnings=warnings,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# apply_master_change — the ONLY apply path (thin dispatch, design §3.1)
# ---------------------------------------------------------------------------

def apply_master_change(change: MasterChange, actor_email: str) -> ChangeResult:
    """Stamps provenance and dispatches onto the airtable_io primitives.

    Callers MUST have run validate_master_change and refused on errors; this
    function only re-checks what the primitives re-check at apply time (the
    race-note re-reads: upsert_pricing_rules re-reads by_key internally, and
    end_pricing_rule re-resolves + re-checks openness). Both primitives end
    with invalidate_master_cache().
    """
    if change.op not in VALID_OPS:
        raise ValueError(f"unknown op {change.op!r}")

    site, code = _norm(change)
    key = airtable_io._rule_key(site, code, change.valid_from)
    source = f"{change.source_note or 'editor'}:{actor_email}"

    if change.op == "end_rule":
        if change.valid_to is None:
            raise ValueError("end_rule requires valid_to")
        airtable_io.end_pricing_rule(key, change.valid_to, change.reason, source)
        return ChangeResult(created=0, updated=1, closed=1, rule_keys_touched=[key])

    reason = change.reason
    if change.op == "fix_in_place":
        # Fresh read (not the cached snapshot) so the prepend uses the value
        # actually stored right now — and Airtable PATCH would otherwise
        # OVERWRITE the reason cell, losing history (design §2.2).
        recs = airtable_io._list_all(
            airtable_io.T["PricingRules"], fields=["rule_key", "tenant_price", "reason"]
        )
        old = next((r for r in recs if r["fields"].get("rule_key") == key), None)
        if old is None:
            raise ValueError(f"fix_in_place: no pricing rule with rule_key {key!r}")
        old_fields = old["fields"]
        old_price = old_fields.get("tenant_price")
        was = f"£{float(old_price):.2f}" if old_price is not None else "unset"
        stamp = (
            f"corrected by {actor_email} {date.today().isoformat()}: "
            f"was {was} — {change.reason}"
        )
        old_reason = (old_fields.get("reason") or "").strip()
        reason = f"{stamp}; {old_reason}" if old_reason else stamp

    rule = Rule(
        site_id=site,
        product_code=code,
        product_desc=change.product_desc or "",
        tenant_price=change.tenant_price,
        fb_price=change.fb_price,
        retro_pct=change.retro_pct or 0.0,  # fraction, never rounded
        valid_from=change.valid_from,
        # fix_in_place must not touch valid_to: None is stripped by the upsert,
        # so the stored value (open or ended) survives the PATCH.
        valid_to=None if change.op == "fix_in_place" else change.valid_to,
        status=change.status,
        reason=reason,
        source=source,
    )
    # price_change/add_rule close any prior open rule for this (site, product)
    # at the effective date; for a genuinely new key the close pass finds
    # nothing to close — the same call is safe (design §3.1 dispatch).
    close_at = change.valid_from if change.op in ("price_change", "add_rule") else None
    created, updated, closed = airtable_io.upsert_pricing_rules([rule], close_at)
    return ChangeResult(
        created=created, updated=updated, closed=closed, rule_keys_touched=[key]
    )


# ---------------------------------------------------------------------------
# patch_snapshot_for_change — in-memory mirror of an applied change
# ---------------------------------------------------------------------------

def patch_snapshot_for_change(
    snap: MasterSnapshot, change: MasterChange, actor_email: str
) -> MasterSnapshot:
    """A NEW MasterSnapshot reflecting what ``change`` just wrote to Airtable.

    Powers the grid cell editor's save -> redirect -> grid flow: publishing the
    patched snapshot (airtable_io.publish_patched_snapshot) lets the very next
    page read show the change instantly instead of leaving the cache at None —
    where the next read would do the ~30s inline sweep that times out the hub
    proxy. The background refresh replaces this patch with the authoritative
    Airtable state within ~a minute.

    Mirrors apply_master_change's semantics; call it only AFTER a successful
    apply. Never mutates ``snap`` (concurrent renders may hold it) — the rules
    list and any changed Rule are copied."""
    from dataclasses import replace as _dc_replace

    site, code = _norm(change)
    key = airtable_io._rule_key(site, code, change.valid_from)
    rules = list(snap.rules)

    def _is_target(r: Rule) -> bool:
        return (
            r.site_id == site and r.product_code == code
            and r.valid_from == change.valid_from
        )

    if change.op == "end_rule":
        rules = [
            _dc_replace(r, valid_to=change.valid_to) if _is_target(r) else r
            for r in rules
        ]
        return _dc_replace(snap, rules=rules)

    if change.op == "fix_in_place":
        def _fixed(r: Rule) -> Rule:
            return _dc_replace(
                r,
                tenant_price=(
                    change.tenant_price if change.tenant_price is not None else r.tenant_price
                ),
                fb_price=change.fb_price if change.fb_price is not None else r.fb_price,
                # zero/None retro keeps the stored value — the write path skips zeros
                retro_pct=change.retro_pct if change.retro_pct else r.retro_pct,
                status=change.status or r.status,
            )
        rules = [_fixed(r) if _is_target(r) else r for r in rules]
        return _dc_replace(snap, rules=rules)

    # price_change / add_rule: the close pass ends any OPEN rule for this
    # (site, product) starting before the effective date, then the new rule
    # is appended (upsert_pricing_rules semantics).
    d = change.valid_from
    def _closed(r: Rule) -> Rule:
        if (
            r.site_id == site and r.product_code == code
            and r.valid_to is None and d is not None
            and (r.valid_from or date.min) < d
        ):
            return _dc_replace(r, valid_to=d)
        return r
    rules = [_closed(r) for r in rules]
    desc = change.product_desc or next(
        (r.product_desc for r in snap.rules
         if r.product_code == code and r.product_desc), "",
    )
    rules.append(Rule(
        site_id=site,
        product_code=code,
        product_desc=desc,
        tenant_price=change.tenant_price,
        fb_price=change.fb_price,
        retro_pct=change.retro_pct or 0.0,
        valid_from=change.valid_from,
        valid_to=change.valid_to,
        status=change.status,
        reason=change.reason,
        source=f"{change.source_note or 'editor'}:{actor_email}",
    ))
    rule_ids = dict(snap.rule_ids)
    rule_ids.setdefault(key, "pending-refresh")
    return _dc_replace(snap, rules=rules, rule_ids=rule_ids)


# ---------------------------------------------------------------------------
# Universal (annual) price increase — pure builder + snapshot patch
# ---------------------------------------------------------------------------

# Warn outside this band; block outside ±50% (fat-finger guard).
INCREASE_SANITY_BAND_PCT = (-20.0, 20.0)
INCREASE_HARD_LIMIT_PCT = 50.0


def build_universal_increase(
    snap: MasterSnapshot, pct: float, effective: date, actor_email: str = ""
) -> tuple[list[Rule], dict]:
    """The successor rules for an across-the-board price increase of ``pct``%.

    Operator semantics (2026-07-02): the increase moves the TENANT price and
    the FB LIST price — the retro stays a FIXED £/keg. Because the stored
    retro_pct is a fraction of the list price, the successor's retro_pct is
    recomputed so retro £ is preserved exactly:
        retro£ = retro_pct x fb  ==  retro_pct' x fb'   =>   retro_pct' = retro_pct x fb / fb'
    The net price (list − retro) therefore rises with the list.

    Included: every OPEN rule (valid_to None) with status 'tenanted' whose
    valid_from is on/before ``effective``. A rule starting exactly ON the
    effective date shares the successor's rule_key, so the upsert updates it
    in place rather than closing it — same outcome, no duplicate.
    Skipped (reported in stats): supported/managed rules (temporary or
    zero-margin layers — an annual uplift doesn't apply), future-dated rules,
    and rules with no tenant price.
    """
    mult = 1.0 + pct / 100.0
    new_rules: list[Rule] = []
    skipped_support = 0
    skipped_future = 0
    skipped_no_price = 0
    skipped_already_at_date = 0
    examples: list[dict] = []
    sites_seen: set = set()
    products_seen: set = set()

    for r in snap.rules:
        if r.valid_to is not None:
            continue
        if (r.status or "tenanted") != "tenanted":
            skipped_support += 1
            continue
        if r.valid_from is not None and r.valid_from > effective:
            skipped_future += 1
            continue
        if r.valid_from is not None and r.valid_from == effective:
            # Already dated ON the effective date: either a successor this run
            # already created (a prior run that timed out part-way), or a
            # same-day manual edit. Re-increasing it would COMPOUND the uplift,
            # and its rule_key already equals the successor's — so skip it. This
            # is what makes the apply idempotent and safe to re-run after a
            # partial/timed-out apply (write order below is create-then-close,
            # so a crash never drops a price — the successor already shadows the
            # still-open original, and a re-run finishes the close pass).
            skipped_already_at_date += 1
            continue
        if r.tenant_price is None:
            skipped_no_price += 1
            continue
        new_tenant = round(r.tenant_price * mult, 2)
        if r.fb_price is not None:
            new_fb = round(r.fb_price * mult, 2)
            retro_gbp = (r.retro_pct or 0.0) * r.fb_price
            new_retro_pct = (retro_gbp / new_fb) if new_fb else 0.0
        else:
            new_fb = None
            new_retro_pct = r.retro_pct or 0.0
        new_rules.append(Rule(
            site_id=r.site_id,
            product_code=r.product_code,
            product_desc=r.product_desc,
            tenant_price=new_tenant,
            fb_price=new_fb,
            retro_pct=new_retro_pct,
            valid_from=effective,
            valid_to=None,
            status="tenanted",
            reason=f"annual increase {pct:+g}% (was {r.tenant_price})",
            source=f"increase:{actor_email}" if actor_email else "increase",
        ))
        sites_seen.add(r.site_id)
        products_seen.add(r.product_code)
        if len(examples) < 5:
            examples.append({
                "site_id": r.site_id,
                "product_code": r.product_code,
                "product_desc": r.product_desc,
                "old_tenant": r.tenant_price,
                "new_tenant": new_tenant,
                "old_fb": r.fb_price,
                "new_fb": new_fb,
                "retro_gbp": (r.retro_pct or 0.0) * (r.fb_price or 0.0),
            })

    # Fingerprint of exactly which prices this increase would rewrite (and
    # from what). The confirm page carries it and apply REFUSES on mismatch —
    # so a double submit (the first apply already moved the prices) or a
    # concurrent edit between preview and apply can never compound the
    # increase.
    import hashlib
    old_prices = "|".join(
        f"{r.site_id},{r.product_code},{_date_iso(r.valid_from)},{r.tenant_price}"
        for r in sorted(
            (r for r in snap.rules
             if r.valid_to is None and (r.status or "tenanted") == "tenanted"
             and r.tenant_price is not None
             # strictly BEFORE the effective date — the exact set that will be
             # rewritten (rules already at the effective date are skipped above)
             and (r.valid_from is None or r.valid_from < effective)),
            key=lambda r: (r.site_id, r.product_code, _date_iso(r.valid_from)),
        )
    )
    checksum = hashlib.sha256(
        f"{pct}|{effective.isoformat()}|{old_prices}".encode()
    ).hexdigest()[:16]

    stats = {
        "n_rules": len(new_rules),
        "n_sites": len(sites_seen),
        "n_products": len(products_seen),
        "skipped_support": skipped_support,
        "skipped_future": skipped_future,
        "skipped_no_price": skipped_no_price,
        "skipped_already_at_date": skipped_already_at_date,
        "examples": examples,
        "checksum": checksum,
    }
    return new_rules, stats


def _date_iso(d: date | None) -> str:
    return d.isoformat() if d else "open"


def patch_snapshot_for_bulk_upsert(
    snap: MasterSnapshot, new_rules: list[Rule], effective: date
) -> MasterSnapshot:
    """The in-memory mirror of applying ``new_rules`` via
    upsert_pricing_rules(close_keys_at_date=effective) — same shape as
    patch_snapshot_for_change but for bulk writes (the annual increase and the
    /upload-master whole-file replace both ride this)."""
    from dataclasses import replace as _dc_replace

    touched = {(r.site_id, r.product_code) for r in new_rules}
    new_keys = {(r.site_id, r.product_code, r.valid_from) for r in new_rules}
    rules: list[Rule] = []
    for r in snap.rules:
        k3 = (r.site_id, r.product_code, r.valid_from)
        if k3 in new_keys:
            continue  # same-key rule is REPLACED in place by the successor
        if (
            (r.site_id, r.product_code) in touched
            and r.valid_to is None
            and (r.valid_from or date.min) < effective
        ):
            rules.append(_dc_replace(r, valid_to=effective))
        else:
            rules.append(r)
    rules.extend(new_rules)
    rule_ids = dict(snap.rule_ids)
    for r in new_rules:
        rule_ids.setdefault(_key_of(r), "pending-refresh")
    return _dc_replace(snap, rules=rules, rule_ids=rule_ids)

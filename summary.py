"""
Per-file reconciliation summary, broken into the three sections that match the
operator's review workflow:

1. Tenant pricing mismatches by site → product
2. FB Taverns pricing mismatches by product (aggregated across sites)
3. Sites in the master that did not buy anything in this file
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape
from typing import Iterable

from reconcile import InvoiceLine, Mismatch  # type: ignore


# ---------- data shapes ----------

@dataclass
class TenantRow:
    product_code: str
    product_desc: str
    expected: float
    actual: float
    qty: float
    delta_per_unit: float
    delta_total: float
    support_note: str = ""  # populated when matched rule is status='supported'


@dataclass
class TenantSiteBlock:
    site_id: str
    site_name: str
    rows: list[TenantRow] = field(default_factory=list)
    total_delta: float = 0.0


@dataclass
class FBProductBlock:
    product_code: str
    product_desc: str
    expected: float
    actual: float
    delta_per_unit: float
    # site_id -> (site_name, total_qty_at_site, total_delta_at_site)
    site_totals: dict[str, tuple[str, float, float]] = field(default_factory=dict)
    total_qty: float = 0.0
    total_delta: float = 0.0

    @property
    def site_count(self) -> int:
        return len(self.site_totals)


@dataclass
class MissingSite:
    site_id: str
    site_name: str
    status: str


@dataclass
class OtherFindingRow:
    site_id: str
    site_name: str
    product_code: str
    product_desc: str
    qty: float
    notes: str = ""
    charged: float = 0.0   # qty-weighted invoiced unit price (LWC UNIT)
    cost: float = 0.0      # qty-weighted FB cost basis (LWC MASTER)
    mixed: bool = False    # True when the source lines had differing unit prices


@dataclass
class Summary:
    file_name: str
    line_count: int
    mismatch_count: int
    tenant_blocks: list[TenantSiteBlock]
    fb_blocks: list[FBProductBlock]
    missing_sites: list[MissingSite]
    products_not_on_master: list[OtherFindingRow]
    tenant_price_missing: list[OtherFindingRow]
    sites_in_sales_not_on_master: list[OtherFindingRow]
    other_counts: dict[str, int]  # everything else (arithmetic_error, etc.)
    total_tenant_delta: float = 0.0
    total_fb_delta: float = 0.0


# ---------- builder ----------

def build_summary(
    file_name: str,
    lines: list[InvoiceLine],
    mismatches: list[Mismatch],
    sites_master: dict[str, dict],
    active_site_ids: set[str] | None = None,
) -> Summary:
    """
    active_site_ids — sites currently on the master (have at least one open
    pricing rule). Section 3 ("didn't buy this week") is filtered to only
    these. If None, falls back to all sites in sites_master (legacy behaviour).
    """
    # 1. Tenant pricing mismatches grouped by site
    by_site: dict[str, TenantSiteBlock] = {}
    for m in mismatches:
        if m.type != "wrong_tenant_price":
            continue
        block = by_site.get(m.line.site_id)
        if block is None:
            block = TenantSiteBlock(
                site_id=m.line.site_id,
                site_name=m.line.site_name or sites_master.get(m.line.site_id, {}).get("name", ""),
            )
            by_site[m.line.site_id] = block
        is_support = bool(m.rule and m.rule.status == "supported")
        block.rows.append(
            TenantRow(
                product_code=m.line.product_code,
                product_desc=m.line.product_desc,
                expected=m.expected_tenant_price or 0.0,
                actual=m.actual_tenant_price or 0.0,
                qty=m.line.qty,
                delta_per_unit=m.delta_per_unit,
                delta_total=m.delta_total,
                support_note=m.notes if is_support else "",
            )
        )
        block.total_delta += m.delta_total

    tenant_blocks = sorted(by_site.values(), key=lambda b: -abs(b.total_delta))
    for b in tenant_blocks:
        b.rows.sort(key=lambda r: -abs(r.delta_total))

    # 2. FB pricing mismatches aggregated by product, then by distinct site.
    # Multiple delivery rows for the same site/product collapse to one site
    # entry whose qty is the sum across rows. site_count = distinct sites.
    fb_agg: dict[tuple[str, float, float], FBProductBlock] = {}
    for m in mismatches:
        if m.type != "wrong_fb_price":
            continue
        key = (
            m.line.product_code,
            round(m.expected_fb_price or 0.0, 4),
            round(m.actual_fb_price or 0.0, 4),
        )
        block = fb_agg.get(key)
        if block is None:
            block = FBProductBlock(
                product_code=m.line.product_code,
                product_desc=m.line.product_desc,
                expected=m.expected_fb_price or 0.0,
                actual=m.actual_fb_price or 0.0,
                delta_per_unit=m.delta_per_unit,
            )
            fb_agg[key] = block
        existing = block.site_totals.get(m.line.site_id)
        if existing:
            block.site_totals[m.line.site_id] = (
                existing[0],
                existing[1] + m.line.qty,
                existing[2] + m.delta_total,
            )
        else:
            block.site_totals[m.line.site_id] = (m.line.site_name, m.line.qty, m.delta_total)
        block.total_qty += m.line.qty
        block.total_delta += m.delta_total

    fb_blocks = sorted(fb_agg.values(), key=lambda b: -abs(b.total_delta))

    # 3. Sites in the master that didn't appear in this file's lines.
    # Per addendum Patch 2: only include sites that are CURRENTLY on the
    # master (have at least one active rule). YOF Ltd 840-style entries
    # that are in the Sites table but not on the cost file shouldn't appear.
    sites_in_file = {l.site_id for l in lines if l.site_id}
    expected_sites = active_site_ids if active_site_ids is not None else set(sites_master.keys())
    missing: list[MissingSite] = []
    for sid in sorted(expected_sites):
        if sid not in sites_in_file:
            info = sites_master.get(sid) or {}
            missing.append(
                MissingSite(
                    site_id=sid,
                    site_name=info.get("name", ""),
                    status=info.get("status", "tenanted"),
                )
            )

    # 4a-c. Split the former "Other findings" / no_rule_for_line into actionable
    # buckets. The two priced buckets (products_not_on_master / tenant_price_missing)
    # aggregate by (site, product) so each product is ONE actionable row carrying
    # the qty-weighted charged price (LWC UNIT) and cost basis (LWC MASTER) — the
    # basis for the margin shown and the "accept into master" button.
    sites_in_sales_not_on_master: list[OtherFindingRow] = []
    seen_unknown_site_keys: set[tuple[str, str]] = set()
    other_counts: dict[str, int] = {}
    _pnm: dict[tuple[str, str], dict] = {}
    _tpm: dict[tuple[str, str], dict] = {}

    def _fin(x) -> float:
        """Coerce to a finite float — a blank/NaN invoice cell would otherwise
        propagate NaN into the JSON blob and kill the whole findings script."""
        x = x or 0.0
        return x if isinstance(x, (int, float)) and math.isfinite(x) else 0.0

    def _accumulate(acc: dict, line: InvoiceLine) -> None:
        key = (line.site_id, line.product_code)
        a = acc.get(key)
        if a is None:
            a = {"site_id": line.site_id, "site_name": line.site_name,
                 "product_code": line.product_code, "product_desc": line.product_desc,
                 "qty": 0.0, "uq": 0.0, "mq": 0.0, "n": 0, "usum": 0.0, "msum": 0.0,
                 "umin": None, "umax": None}
            acc[key] = a
        q = _fin(line.qty)
        up = _fin(line.unit_price)
        mp = _fin(line.master_price)
        a["qty"] += q
        a["uq"] += up * q
        a["mq"] += mp * q
        a["usum"] += up
        a["msum"] += mp
        a["n"] += 1
        a["umin"] = up if a["umin"] is None else min(a["umin"], up)
        a["umax"] = up if a["umax"] is None else max(a["umax"], up)
        if line.site_name and not a["site_name"]:
            a["site_name"] = line.site_name
        if line.product_desc and not a["product_desc"]:
            a["product_desc"] = line.product_desc

    def _finalize(acc: dict) -> list[OtherFindingRow]:
        out: list[OtherFindingRow] = []
        for a in acc.values():
            qty = a["qty"]
            if qty > 0:
                charged, cost = a["uq"] / qty, a["mq"] / qty
            elif a["n"]:
                charged, cost = a["usum"] / a["n"], a["msum"] / a["n"]
            else:
                charged = cost = 0.0
            mixed = (
                a["umin"] is not None and a["umax"] is not None
                and (a["umax"] - a["umin"]) > 0.01
            )
            out.append(OtherFindingRow(
                site_id=a["site_id"], site_name=a["site_name"],
                product_code=a["product_code"], product_desc=a["product_desc"],
                qty=_fin(qty), charged=_fin(charged), cost=_fin(cost), mixed=mixed,
            ))
        return out

    for m in mismatches:
        t = m.type
        if t in ("wrong_tenant_price", "wrong_fb_price"):
            continue
        line = m.line
        if t == "product_not_on_master":
            _accumulate(_pnm, line)
        elif t == "tenant_price_missing":
            _accumulate(_tpm, line)
        elif t == "unknown_site":
            key = (line.site_id, line.site_name)
            if key not in seen_unknown_site_keys:
                seen_unknown_site_keys.add(key)
                sites_in_sales_not_on_master.append(OtherFindingRow(
                    site_id=line.site_id, site_name=line.site_name,
                    product_code=line.product_code, product_desc=line.product_desc,
                    qty=line.qty, notes=m.notes,
                ))
        else:
            other_counts[t] = other_counts.get(t, 0) + 1

    products_not_on_master = _finalize(_pnm)
    tenant_price_missing = _finalize(_tpm)
    products_not_on_master.sort(key=lambda r: (r.product_code, r.site_id))
    tenant_price_missing.sort(key=lambda r: (r.site_id, r.product_code))
    sites_in_sales_not_on_master.sort(key=lambda r: r.site_id)

    return Summary(
        file_name=file_name,
        line_count=len(lines),
        mismatch_count=len(mismatches),
        tenant_blocks=tenant_blocks,
        fb_blocks=fb_blocks,
        missing_sites=missing,
        products_not_on_master=products_not_on_master,
        tenant_price_missing=tenant_price_missing,
        sites_in_sales_not_on_master=sites_in_sales_not_on_master,
        other_counts=other_counts,
        total_tenant_delta=sum(b.total_delta for b in tenant_blocks),
        total_fb_delta=sum(b.total_delta for b in fb_blocks),
    )


# ---------- HTML renderer ----------

def _money(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}£{abs(v):,.2f}"


def _money_neutral(v: float) -> str:
    return f"£{v:,.2f}"


def _margin(charged: float, cost: float) -> tuple[float, float]:
    """(£/unit, %) FB margin of the invoiced price over the LWC cost basis.
    Pre-retro — the invoice line carries no retro, and a real retro only widens
    the margin, so a thin margin here is a reliable 'LWC mis-priced it' signal."""
    gbp = charged - cost
    pct = (gbp / charged * 100.0) if charged else 0.0
    return gbp, pct


def _margin_cls(gbp: float, pct: float) -> str:
    if gbp <= 0:
        return "mg-bad"
    if pct < 5:
        return "mg-warn"
    return "mg-ok"


# Pricing policy for the price we INSTRUCT LWC to set when there's no agreed price
# (operator, 2026): cask → a fixed £35/keg margin over FB cost (price = cost + 35);
# other draught → 40% gross margin of the selling price, pre-retro
# (price = cost / (1 − 0.40)). Cask is identified the same way as the master editor
# (master_pages._is_cask): "cask" in the product description.
# Fallback defaults. The LIVE values come from the editable Config record
# (airtable_io.load_pricing_policy) and are threaded through as `policy`; these
# apply only when no policy is passed (tests / cold config).
CASK_FIXED_MARGIN_GBP = 35.0
DRAUGHT_TARGET_GP = 0.40


def _policy_get(policy, key, default):
    if policy and policy.get(key) is not None:
        return policy[key]
    return default


def _policy_cask(policy) -> float:
    return float(_policy_get(policy, "cask_margin_gbp", CASK_FIXED_MARGIN_GBP))


def _policy_gp(policy) -> float:
    gp = float(_policy_get(policy, "draught_target_gp", DRAUGHT_TARGET_GP))
    return gp / 100.0 if gp >= 1 else gp   # tolerate 40 entered instead of 0.40


# A 9-gallon firkin (and a 4.5G pin) IS a cask container, so trade descriptions
# routinely mark cask by size — "SHARPS TWIN COAST PALE ALE 9G" — without ever
# writing the word "cask". Treat those as cask too, else they fall through to the
# 40%-GP draught rule and get over-priced. Safe for this estate: 42/42 nine-gallon
# products in the master are cask and none is a keg. Deliberately NOT 10G (Bass
# cask vs Stella keg — ambiguous) and NOT the bare word "pin" (hits pineapple/gin),
# either of which could tag a keg as cask and UNDER-price it.
_CASK_SIZE_RE = re.compile(r"\b(?:9|4\.5)\s*g(?:al(?:lon)?s?)?\b|\bfirkin\b", re.IGNORECASE)


def _is_cask(desc: str) -> bool:
    d = (desc or "").lower()
    return "cask" in d or bool(_CASK_SIZE_RE.search(d))


def _suggested_price(desc: str, cost: float, policy=None) -> float | None:
    """Tenant price to instruct LWC to set, per policy. None with no cost basis."""
    if not cost or cost <= 0:
        return None
    if _is_cask(desc):
        return cost + _policy_cask(policy)
    gp = _policy_gp(policy)
    return cost / (1.0 - gp) if 0 <= gp < 1 else None


def _sug_round(desc: str, cost: float, policy=None):
    v = _suggested_price(desc, cost, policy)
    return round(v, 2) if v is not None else None


def _suggest_basis(desc: str, policy=None) -> str:
    if _is_cask(desc):
        return f"cask: FB cost + £{_policy_cask(policy):.0f}/keg"
    gp = _policy_gp(policy)
    return f"{gp * 100:.0f}% gross margin (pre-retro): FB cost ÷ {1 - gp:.2f}"


def _policy_banner_html(policy, policy_url: str, can_accept: bool) -> str:
    """Footnote showing the current suggested-price policy; an amber reminder
    once the annual review date has passed (bump the cask margin + tell LWC)."""
    cask = _policy_cask(policy)
    gp = _policy_gp(policy)
    eff = _policy_get(policy, "effective_from", "")
    review = _policy_get(policy, "next_review_date", "")
    due = False
    try:
        if review:
            due = date.today() >= date.fromisoformat(str(review)[:10])
    except (ValueError, TypeError):
        due = False
    link = (f" <a href=\"{escape(policy_url, quote=True)}\">Review policy &rarr;</a>"
            if (policy_url and can_accept) else "")
    if due:
        return (
            "<div class='policy-banner due'>&#9888; <strong>Pricing policy due for its annual "
            f"RPI review</strong> (cask margin £{cask:.0f}/keg, set {escape(str(eff))}). Increase it "
            "and <strong>inform LWC that the cask margin rises across the board by the inflation "
            f"amount</strong>.{link}</div>"
        )
    return (
        f"<p class='sub policy-note'>Suggested prices use cask FB cost + £{cask:.0f}/keg and "
        f"{gp * 100:.0f}% gross margin (pre-retro) for other draught. Effective {escape(str(eff))}; "
        f"next review {escape(str(review))}.{link}</p>"
    )


def _acceptable_table(rows: list[OtherFindingRow], can_accept: bool, policy=None) -> str:
    """Table for the two actionable 'other' buckets: charged price, FB cost,
    margin £ and %, suggested price (policy), plus (admins only) an 'Add to
    master' button that writes the charged price for that (site, product)."""
    head_action = "<th></th>" if can_accept else ""
    _mtitle = "Pre-retro gross margin: (charged − FB cost) ÷ charged. A rebate only widens it."
    _cask = _policy_cask(policy)
    _gp = _policy_gp(policy)
    _stitle = (
        f"Price to instruct LWC to set — cask: FB cost + £{_cask:.0f}/keg; "
        f"other draught: {_gp * 100:.0f}% gross margin pre-retro (FB cost ÷ {1 - _gp:.2f})"
    )
    out = [
        "<table><thead><tr>"
        "<th>Site</th><th>Name</th><th>Code</th><th>Description</th>"
        "<th class='r'>Qty</th><th class='r'>Charged</th><th class='r'>FB cost</th>"
        f"<th class='r' title=\"{escape(_mtitle, quote=True)}\">Margin/unit *</th>"
        f"<th class='r' title=\"{escape(_mtitle, quote=True)}\">Margin % *</th>"
        f"<th class='r' title=\"{escape(_stitle, quote=True)}\">Suggested</th>"
        f"{head_action}</tr></thead><tbody>"
    ]
    for r in rows:
        gbp, pct = _margin(r.charged, r.cost)
        mcls = _margin_cls(gbp, pct)
        charged_cell = _money_neutral(r.charged)
        if r.mixed:
            charged_cell += " <span class='mixed-note'>(varied)</span>"
        sug = _suggested_price(r.product_desc, r.cost, policy)
        if sug is not None:
            sug_cell = (
                f"<td class='r' title=\"{escape(_suggest_basis(r.product_desc, policy), quote=True)}\">"
                f"<strong>{_money_neutral(sug)}</strong></td>"
            )
        else:
            sug_cell = "<td class='r'>&mdash;</td>"
        btn = ""
        if can_accept:
            if r.mixed:
                # Differing prices across this file's lines: the weighted figure
                # matches no single invoice, so don't offer a one-click write —
                # route to the editor to pick a specific price.
                btn = (
                    "<td><span class='mixed-note' title='This product was charged at more "
                    "than one price in this file — set it in the master editor'>use editor</span></td>"
                )
            else:
                btn = (
                    "<td><button type='button' class='accept-btn'"
                    f" data-site=\"{escape(r.site_id, quote=True)}\""
                    f" data-sitename=\"{escape(r.site_name, quote=True)}\""
                    f" data-product=\"{escape(r.product_code, quote=True)}\""
                    f" data-desc=\"{escape(r.product_desc, quote=True)}\""
                    f" data-charged=\"{r.charged:.2f}\" data-cost=\"{r.cost:.2f}\""
                    f" data-qty=\"{r.qty:g}\">Add to master</button></td>"
                )
        out.append(
            f"<tr data-key=\"{escape(r.site_id + '|' + r.product_code, quote=True)}\">"
            f"<td>{escape(r.site_id)}</td><td>{escape(r.site_name)}</td>"
            f"<td>{escape(r.product_code)}</td><td>{escape(r.product_desc)}</td>"
            f"<td class='r'>{r.qty:g}</td>"
            f"<td class='r'>{charged_cell}</td>"
            f"<td class='r'>{_money_neutral(r.cost)}</td>"
            f"<td class='r {mcls}'>{_money(gbp)}</td>"
            f"<td class='r {mcls}'>{pct:.1f}%</td>"
            f"{sug_cell}"
            f"{btn}</tr>"
        )
    out.append("</tbody></table>")
    out.append("<p class='sub' style='margin-top:-0.4em'>* Margin is pre-retro (gross) &mdash; a rebate only widens it.</p>")
    return "".join(out)


_FINDINGS_STYLE = """<style>
  .mg-bad { color:#b00020; font-weight:600; }
  .mg-warn { color:#8a6500; font-weight:600; }
  .mg-ok { color:#1f7a1f; }
  .accept-btn { background:#33691e; color:#fff; border:0; padding:0.3em 0.7em; border-radius:4px; font-size:0.82em; cursor:pointer; white-space:nowrap; }
  .accept-btn:hover { background:#274f16; }
  .accept-btn:disabled { opacity:0.6; cursor:default; }
  tr.accepted td { background:#eef7ea; color:#567; }
  .accepted-tag { color:#1f7a1f; font-weight:700; font-size:0.85em; }
  .email-draft { background:#f6f9ff; border:1px solid #c7d8f0; border-radius:6px; padding:1em; margin-top:1em; max-width:none; }
  .email-draft label { display:block; margin:0.6em 0 0.2em; font-weight:600; }
  .email-draft input[type=text] { width:100%; padding:0.45em; box-sizing:border-box; margin:0; }
  .email-draft textarea { width:100%; min-height:230px; box-sizing:border-box; font-family:ui-monospace,Menlo,Consolas,monospace; font-size:0.85em; }
  .email-actions { margin-top:0.6em; display:flex; gap:0.6em; align-items:center; flex-wrap:wrap; }
  .email-actions .ok { color:#1f7a1f; font-size:0.85em; }
  .eff-date-bar { max-width:none; }
  .eff-date-bar input[type=date] { display:inline; width:auto; margin:0 0 0 0.3em; }
  .mixed-note { color:#8a6500; font-size:0.85em; font-style:italic; }
  #email-dirty-note { color:#8a6500; font-size:0.85em; }
  .policy-banner.due { background:#fff4cf; border:1px solid #e6c34d; border-radius:6px; padding:0.7em 1em; margin:0.6em 0; color:#6b4e00; font-size:0.92em; }
  .policy-note { font-size:0.85em; color:#666; margin:0.3em 0 0.6em; }
</style>"""


def _findings_script(cfg: dict) -> str:
    # allow_nan=False: never emit bare NaN/Infinity — invalid JSON that would kill
    # JSON.parse and the entire findings script. Escape < > & as \\u00xx so no
    # product description / site name can break out of the <script> data context
    # (a lone '</' replacement misses e.g. '<!--<script'); these round-trip
    # through JSON.parse unchanged.
    try:
        cfg_json = json.dumps(cfg, allow_nan=False)
    except ValueError:
        cfg_json = json.dumps({
            "acceptUrl": cfg.get("acceptUrl", ""),
            "sourceFile": cfg.get("sourceFile", ""),
            "defaultEffDate": cfg.get("defaultEffDate", ""),
            "email": {"file": cfg.get("sourceFile", ""),
                      "tenant_mismatches": [], "missing_products": [], "missing_prices": []},
        })
    cfg_json = cfg_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        f'<script id="findings-config" type="application/json">{cfg_json}</script>'
        + _FINDINGS_JS
    )


def render_summary_html(
    s: Summary,
    accept_url: str = "/accept-master-rule",
    can_accept: bool = False,
    policy: dict | None = None,
    policy_url: str = "",
) -> str:
    parts: list[str] = [_FINDINGS_STYLE]

    parts.append(
        f"""<div class="result">
  <div class="summary-row"><span>File</span><code>{escape(s.file_name)}</code></div>
  <div class="summary-row"><span>Lines processed</span><strong>{s.line_count}</strong></div>
  <div class="summary-row"><span>Mismatches</span><strong>{s.mismatch_count}</strong></div>
  <div class="summary-row"><span>Tenant pricing exposure (sum)</span><strong>{_money(s.total_tenant_delta)}</strong></div>
  <div class="summary-row"><span>FB pricing exposure (sum)</span><strong>{_money(s.total_fb_delta)}</strong></div>
</div>"""
    )

    parts.append("<h2>1. Tenant pricing mismatches — by site &amp; product</h2>")
    if not s.tenant_blocks:
        parts.append("<p><em>No tenant pricing mismatches.</em></p>")
    else:
        for b in s.tenant_blocks:
            parts.append(
                f"""<details open class="block">
  <summary><strong>{escape(b.site_id)} — {escape(b.site_name)}</strong>
  <span class="pill">{len(b.rows)} item{'s' if len(b.rows) != 1 else ''}</span>
  <span class="pill">net {_money(b.total_delta)}</span></summary>
  <table>
    <thead><tr>
      <th>Product</th><th>Description</th>
      <th class="r">Expected</th><th class="r">Charged</th>
      <th class="r">Qty</th><th class="r">Δ / unit</th><th class="r">Δ total</th>
    </tr></thead>
    <tbody>"""
            )
            for r in b.rows:
                cls = "neg" if r.delta_total < 0 else "pos"
                desc_cell = escape(r.product_desc)
                if r.support_note:
                    desc_cell += " <span class='support-tag'>SUPPORT</span>"
                parts.append(
                    f"<tr class='{cls}'>"
                    f"<td>{escape(r.product_code)}</td>"
                    f"<td>{desc_cell}</td>"
                    f"<td class='r'>{_money_neutral(r.expected)}</td>"
                    f"<td class='r'>{_money_neutral(r.actual)}</td>"
                    f"<td class='r'>{r.qty:g}</td>"
                    f"<td class='r'>{_money(r.delta_per_unit)}</td>"
                    f"<td class='r'><strong>{_money(r.delta_total)}</strong></td>"
                    f"</tr>"
                )
                if r.support_note:
                    parts.append(
                        f"<tr class='support-note'>"
                        f"<td colspan='7'><em>{escape(r.support_note)}</em></td>"
                        f"</tr>"
                    )
            parts.append("</tbody></table></details>")

    parts.append("<h2>2. FB Taverns pricing mismatches — by product</h2>")
    if not s.fb_blocks:
        parts.append("<p><em>No FB pricing mismatches above the £0.05 / unit threshold.</em></p>")
    else:
        parts.append(
            """<table>
  <thead><tr>
    <th>Product</th><th>Description</th>
    <th class="r">Expected FB</th><th class="r">Charged FB</th>
    <th class="r">Δ / unit</th><th class="r">Sites</th>
    <th class="r">Total qty</th><th class="r">Δ total</th>
  </tr></thead>
  <tbody>"""
        )
        for b in s.fb_blocks:
            cls = "neg" if b.total_delta < 0 else "pos"
            sites_attr = "; ".join(
                f"{sid} {name} (qty {q:g})"
                for sid, (name, q, _d) in sorted(b.site_totals.items())
            )
            parts.append(
                f"<tr class='{cls}'>"
                f"<td>{escape(b.product_code)}</td>"
                f"<td>{escape(b.product_desc)}</td>"
                f"<td class='r'>{_money_neutral(b.expected)}</td>"
                f"<td class='r'>{_money_neutral(b.actual)}</td>"
                f"<td class='r'>{_money(b.delta_per_unit)}</td>"
                f"<td class='r' title='{escape(sites_attr)}'>{b.site_count}</td>"
                f"<td class='r'>{b.total_qty:g}</td>"
                f"<td class='r'><strong>{_money(b.total_delta)}</strong></td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>3. Sites that didn't buy this week</h2>")
    if not s.missing_sites:
        parts.append("<p><em>Every site in the master had at least one invoice line.</em></p>")
    else:
        parts.append("<table><thead><tr><th>Site</th><th>Name</th><th>Status</th></tr></thead><tbody>")
        for ms in s.missing_sites:
            parts.append(
                f"<tr><td>{escape(ms.site_id)}</td>"
                f"<td>{escape(ms.site_name)}</td>"
                f"<td>{escape(ms.status)}</td></tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>4. Other findings</h2>")

    if s.products_not_on_master or s.tenant_price_missing:
        parts.append(_policy_banner_html(policy, policy_url, can_accept))

    if can_accept and (s.products_not_on_master or s.tenant_price_missing):
        parts.append(
            "<div class='result eff-date-bar'>"
            "<label for='accept-eff-date' style='display:inline; font-weight:600'>Effective date for accepted prices</label>"
            f"<input type='date' id='accept-eff-date' value='{(date.today() - timedelta(days=14)).isoformat()}'>"
            " <span class='sub' style='margin:0'>— applies to every price you accept below.</span>"
            "</div>"
        )

    parts.append(
        f"<h3>Products not on master <span class='pill'>{len(s.products_not_on_master)}</span></h3>"
    )
    if not s.products_not_on_master:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append(
            "<p class='sub'>Charged price, margin over FB cost, and our <strong>suggested</strong> "
            "price to set (per the policy shown above). Healthy charged margin &rarr; add it at the "
            "charged price; otherwise the email below instructs LWC to set our suggested price.</p>"
        )
        parts.append(_acceptable_table(s.products_not_on_master, can_accept, policy))

    parts.append(
        f"<h3>Tenant price missing for site <span class='pill'>{len(s.tenant_price_missing)}</span></h3>"
    )
    if not s.tenant_price_missing:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append(
            "<p class='sub'>Product is on the master but this site has no tenant price. Charged price, "
            "margin, and our suggested price are shown &mdash; accept the charged price into the master, "
            "or let the email below instruct LWC to set our suggested price.</p>"
        )
        parts.append(_acceptable_table(s.tenant_price_missing, can_accept, policy))

    if s.sites_in_sales_not_on_master:
        parts.append(
            f"<h3>Sites in sales but not on master <span class='pill'>{len(s.sites_in_sales_not_on_master)}</span></h3>"
        )
        parts.append("<table><thead><tr><th>Site</th><th>Site name</th></tr></thead><tbody>")
        for r in s.sites_in_sales_not_on_master:
            parts.append(f"<tr><td>{escape(r.site_id)}</td><td>{escape(r.site_name)}</td></tr>")
        parts.append("</tbody></table>")

    if s.other_counts:
        parts.append("<h3>Other</h3><div class='result'>")
        for t, c in sorted(s.other_counts.items(), key=lambda kv: -kv[1]):
            parts.append(f"<div class='summary-row'><span>{escape(t)}</span><strong>{c}</strong></div>")
        parts.append("</div>")

    # 5. Draft email to LWC — built client-side from the mismatches + missing
    # items; accepting a missing item into the master (buttons above) drops it
    # from the draft live. The accept buttons' JS also lives in this block, so it
    # is emitted whenever there is anything actionable on the page.
    if s.tenant_blocks or s.products_not_on_master or s.tenant_price_missing:
        default_subject = f"FB Taverns pricing — {s.file_name}"
        email_data = {
            "file": s.file_name,
            "tenant_mismatches": [
                {"site": b.site_id, "site_name": b.site_name, "product": r.product_code,
                 "desc": r.product_desc, "expected": round(r.expected, 2),
                 "charged": round(r.actual, 2), "delta_total": round(r.delta_total, 2),
                 "qty": r.qty}
                for b in s.tenant_blocks for r in b.rows
            ],
            "missing_products": [
                {"site": r.site_id, "site_name": r.site_name, "product": r.product_code,
                 "desc": r.product_desc, "charged": round(r.charged, 2),
                 "cost": round(r.cost, 2), "qty": r.qty,
                 "suggested": _sug_round(r.product_desc, r.cost, policy), "is_cask": _is_cask(r.product_desc)}
                for r in s.products_not_on_master
            ],
            "missing_prices": [
                {"site": r.site_id, "site_name": r.site_name, "product": r.product_code,
                 "desc": r.product_desc, "charged": round(r.charged, 2),
                 "cost": round(r.cost, 2), "qty": r.qty,
                 "suggested": _sug_round(r.product_desc, r.cost, policy), "is_cask": _is_cask(r.product_desc)}
                for r in s.tenant_price_missing
            ],
        }
        parts.append("<h2>5. Draft email to LWC</h2>")
        parts.append(
            "<p class='sub'>Auto-drafted from the price mismatches and missing items above &mdash; "
            "the tenant prices for LWC to correct, plus our <strong>required prices</strong> for "
            "anything missing, to instruct rather than ask. Accepting an item into the master removes "
            "it from this draft. Edit freely, then copy or open in your mail app.</p>"
        )
        parts.append(
            "<div class='email-draft'>"
            "<label for='email-subject'>Subject</label>"
            f"<input type='text' id='email-subject' value=\"{escape(default_subject, quote=True)}\">"
            "<label for='email-body'>Body</label>"
            "<textarea id='email-body'></textarea>"
            "<div class='email-actions'>"
            "<button type='button' id='email-copy'>Copy email</button>"
            "<a class='button' id='email-mailto' href='#' style='margin-top:0'>Open in mail app</a>"
            "<span class='ok' id='email-copied' style='display:none'>Copied &check;</span>"
            "<span id='email-dirty-note' style='display:none'>&#9888; Edited &mdash; accepted items are no longer auto-removed; delete them by hand.</span>"
            "</div></div>"
        )
        parts.append(_findings_script({
            "acceptUrl": accept_url,
            "sourceFile": s.file_name,
            "defaultEffDate": (date.today() - timedelta(days=14)).isoformat(),
            "email": email_data,
        }))

    return "\n".join(parts)


# Client-side logic for the findings page: the "Add to master" accept buttons
# (POST to accept_url, confirm dialog, mark row done) and the live-drafted LWC
# email. Reads config from the <script id="findings-config"> JSON blob so no
# server values are interpolated into JS. Plain string (NOT an f-string) — the
# JS uses braces heavily.
_FINDINGS_JS = """<script>
(function () {
  var el = document.getElementById('findings-config');
  if (!el) return;
  var CFG;
  try { CFG = JSON.parse(el.textContent); } catch (e) { return; }
  var accepted = new Set();
  var bodyDirty = false;
  var subject = document.getElementById('email-subject');
  var body = document.getElementById('email-body');
  var mailto = document.getElementById('email-mailto');

  function fmtQty(q) {
    q = Number(q) || 0;
    var n = (q % 1 === 0) ? q.toFixed(0) : q.toFixed(2);
    return n + ' keg' + (q === 1 ? '' : 's');
  }
  function money(v) { return '\\u00a3' + (Number(v) || 0).toFixed(2); }

  function effDate() {
    var d = document.getElementById('accept-eff-date');
    return (d && d.value) || CFG.defaultEffDate;
  }

  function buildBody() {
    var e = CFG.email, L = [];
    L.push('Hi,');
    L.push('');
    L.push('Reviewing ' + e.file + ', the following need your attention:');
    L.push('');
    if (e.tenant_mismatches && e.tenant_mismatches.length) {
      L.push('1) Tenant prices charged that differ from the agreed price - please correct these on your system:');
      var total1 = 0;
      e.tenant_mismatches.forEach(function (m) {
        total1 += Number(m.delta_total) || 0;
        L.push('   - ' + m.site + ' ' + m.site_name + ' / ' + m.product + ' ' + m.desc +
               ': agreed ' + money(m.expected) + ', charged ' + money(m.charged) +
               ' (diff ' + money(m.delta_total) + ' over ' + fmtQty(m.qty) + ')');
      });
      L.push('   Total tenant-price discrepancy across the above: ' + money(Math.abs(total1)) + ' ' + (total1 >= 0 ? 'overcharged' : 'undercharged') + '.');
      L.push('');
    }
    var missing = [];
    (e.missing_products || []).forEach(function (x) { var y = {}; for (var k in x) y[k] = x[k]; y.kind = 'not on our price list'; missing.push(y); });
    (e.missing_prices || []).forEach(function (x) { var y = {}; for (var k in x) y[k] = x[k]; y.kind = 'no agreed price for this site'; missing.push(y); });
    missing = missing.filter(function (x) { return !accepted.has(x.site + '|' + x.product); });
    if (missing.length) {
      L.push('2) Please set the following tenant prices on your system:');
      missing.forEach(function (x) {
        var line = '   - ' + x.site + ' ' + x.site_name + ' / ' + x.product + ' ' + x.desc + ': ';
        if (x.suggested != null) {
          line += 'set to ' + money(x.suggested) + ' (currently charged ' + money(x.charged) + ')';
        } else {
          line += 'please confirm the agreed tenant price (currently charged ' + money(x.charged) + ')';
        }
        L.push(line);
      });
      L.push('');
    }
    L.push('Thanks,');
    return L.join('\\n');
  }

  function updateMailto() {
    if (!mailto) return;
    var s = subject ? subject.value : '';
    var b = body ? body.value : '';
    var href = 'mailto:?subject=' + encodeURIComponent(s) + '&body=' + encodeURIComponent(b);
    // Mail apps / ShellExecute cap the URL near 2000 chars; a full weekly draft
    // can exceed that and silently no-op. Past the cap, drop the body and steer
    // the user to Copy.
    if (href.length > 1900) {
      mailto.setAttribute('href', 'mailto:?subject=' + encodeURIComponent(s));
      mailto.textContent = 'Open in mail app (too long \\u2014 use Copy for the body)';
    } else {
      mailto.setAttribute('href', href);
      mailto.textContent = 'Open in mail app';
    }
  }

  function rebuild() {
    if (body && !bodyDirty) body.value = buildBody();
    updateMailto();
  }

  function acceptRule(btn) {
    var d = btn.dataset;
    var msg = 'Add to the live pricing master?\\n\\n' +
      'Site ' + d.site + ' ' + d.sitename + '\\n' +
      'Product ' + d.product + ' ' + d.desc + '\\n' +
      'Tenant price: ' + money(d.charged) + '\\n' +
      'FB cost: ' + money(d.cost) + '\\n' +
      'Effective from: ' + effDate();
    if (!window.confirm(msg)) return;
    var orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving\\u2026';
    var params = new URLSearchParams();
    params.set('site_id', d.site);
    params.set('product_code', d.product);
    params.set('product_desc', d.desc);
    params.set('tenant_price', d.charged);
    params.set('fb_price', d.cost);
    params.set('valid_from', effDate());
    params.set('source_file', CFG.sourceFile);
    fetch(CFG.acceptUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      credentials: 'same-origin',
      body: params.toString()
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; })
        .then(function (j) { return { ok: r.ok && j && j.ok, j: j }; });
    }).then(function (res) {
      if (res.ok) {
        var row = btn.closest('tr');
        if (row) row.classList.add('accepted');
        var td = btn.parentNode;
        td.textContent = '';
        var tag = document.createElement('span');
        tag.className = 'accepted-tag';
        tag.textContent = '\\u2713 added';
        td.appendChild(tag);
        accepted.add(d.site + '|' + d.product);
        if (bodyDirty) {
          var note = document.getElementById('email-dirty-note');
          if (note) note.style.display = 'inline';
        }
        rebuild();
      } else {
        btn.disabled = false;
        btn.textContent = orig;
        window.alert('Could not add to master: ' + ((res.j && res.j.error) || 'unknown error'));
      }
    }).catch(function (err) {
      btn.disabled = false;
      btn.textContent = orig;
      window.alert('Network error: ' + err);
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('.accept-btn'), function (b) {
    b.addEventListener('click', function () { acceptRule(b); });
  });
  if (body) body.addEventListener('input', function () { bodyDirty = true; updateMailto(); });
  if (subject) subject.addEventListener('input', updateMailto);
  var copyBtn = document.getElementById('email-copy');
  if (copyBtn) copyBtn.addEventListener('click', function () {
    var text = (subject ? 'Subject: ' + subject.value + '\\n\\n' : '') + (body ? body.value : '');
    var done = document.getElementById('email-copied');
    function shown() { if (done) { done.style.display = 'inline'; setTimeout(function () { done.style.display = 'none'; }, 2000); } }
    function fallbackCopy() {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.focus(); ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      if (ok) { shown(); }
      else if (body) { body.focus(); body.select(); window.alert('Press Ctrl-C / Cmd-C to copy the draft.'); }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(shown, fallbackCopy);
    } else {
      fallbackCopy();
    }
  });
  rebuild();
})();
</script>"""

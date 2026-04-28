"""
Per-file reconciliation summary, broken into the three sections that match the
operator's review workflow:

1. Tenant pricing mismatches by site → product
2. FB Taverns pricing mismatches by product (aggregated across sites)
3. Sites in the master that did not buy anything in this file
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    # 4a-c. Split the former "Other findings" / no_rule_for_line into actionable buckets.
    products_not_on_master: list[OtherFindingRow] = []
    tenant_price_missing: list[OtherFindingRow] = []
    sites_in_sales_not_on_master: list[OtherFindingRow] = []
    seen_unknown_site_keys: set[tuple[str, str]] = set()
    other_counts: dict[str, int] = {}

    for m in mismatches:
        t = m.type
        if t in ("wrong_tenant_price", "wrong_fb_price"):
            continue
        line = m.line
        row = OtherFindingRow(
            site_id=line.site_id,
            site_name=line.site_name,
            product_code=line.product_code,
            product_desc=line.product_desc,
            qty=line.qty,
            notes=m.notes,
        )
        if t == "product_not_on_master":
            products_not_on_master.append(row)
        elif t == "tenant_price_missing":
            tenant_price_missing.append(row)
        elif t == "unknown_site":
            key = (line.site_id, line.site_name)
            if key not in seen_unknown_site_keys:
                seen_unknown_site_keys.add(key)
                sites_in_sales_not_on_master.append(row)
        else:
            other_counts[t] = other_counts.get(t, 0) + 1

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


def render_summary_html(s: Summary) -> str:
    parts: list[str] = []

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

    parts.append(
        f"<h3>Products not on master <span class='pill'>{len(s.products_not_on_master)}</span></h3>"
    )
    if not s.products_not_on_master:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append("<p class='sub'>Add a product row to the master, or treat as a one-off guest line.</p>")
        parts.append(
            "<table><thead><tr><th>Code</th><th>Description</th><th>Site</th><th>Site name</th><th class='r'>Qty</th></tr></thead><tbody>"
        )
        for r in s.products_not_on_master:
            parts.append(
                f"<tr><td>{escape(r.product_code)}</td><td>{escape(r.product_desc)}</td>"
                f"<td>{escape(r.site_id)}</td><td>{escape(r.site_name)}</td>"
                f"<td class='r'>{r.qty:g}</td></tr>"
            )
        parts.append("</tbody></table>")

    parts.append(
        f"<h3>Tenant price missing for site <span class='pill'>{len(s.tenant_price_missing)}</span></h3>"
    )
    if not s.tenant_price_missing:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append("<p class='sub'>Product is on the master but the tenant-price cell for this site is blank — populate it.</p>")
        parts.append(
            "<table><thead><tr><th>Site</th><th>Site name</th><th>Code</th><th>Description</th><th class='r'>Qty</th></tr></thead><tbody>"
        )
        for r in s.tenant_price_missing:
            parts.append(
                f"<tr><td>{escape(r.site_id)}</td><td>{escape(r.site_name)}</td>"
                f"<td>{escape(r.product_code)}</td><td>{escape(r.product_desc)}</td>"
                f"<td class='r'>{r.qty:g}</td></tr>"
            )
        parts.append("</tbody></table>")

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

    return "\n".join(parts)

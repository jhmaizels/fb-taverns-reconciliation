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
    sites: list[tuple[str, str, float]] = field(default_factory=list)  # (site_id, site_name, qty)
    total_qty: float = 0.0
    total_delta: float = 0.0


@dataclass
class MissingSite:
    site_id: str
    site_name: str
    status: str


@dataclass
class Summary:
    file_name: str
    line_count: int
    mismatch_count: int
    tenant_blocks: list[TenantSiteBlock]
    fb_blocks: list[FBProductBlock]
    missing_sites: list[MissingSite]
    other_counts: dict[str, int]  # mismatch types not in the three sections
    total_tenant_delta: float = 0.0
    total_fb_delta: float = 0.0


# ---------- builder ----------

def build_summary(
    file_name: str,
    lines: list[InvoiceLine],
    mismatches: list[Mismatch],
    sites_master: dict[str, dict],
) -> Summary:
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
        block.rows.append(
            TenantRow(
                product_code=m.line.product_code,
                product_desc=m.line.product_desc,
                expected=m.expected_tenant_price or 0.0,
                actual=m.actual_tenant_price or 0.0,
                qty=m.line.qty,
                delta_per_unit=m.delta_per_unit,
                delta_total=m.delta_total,
            )
        )
        block.total_delta += m.delta_total

    tenant_blocks = sorted(by_site.values(), key=lambda b: -abs(b.total_delta))
    for b in tenant_blocks:
        b.rows.sort(key=lambda r: -abs(r.delta_total))

    # 2. FB pricing mismatches aggregated by product
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
        block.sites.append((m.line.site_id, m.line.site_name, m.line.qty))
        block.total_qty += m.line.qty
        block.total_delta += m.delta_total

    fb_blocks = sorted(fb_agg.values(), key=lambda b: -abs(b.total_delta))
    for b in fb_blocks:
        b.sites.sort(key=lambda s: s[0])

    # 3. Sites in the master that didn't appear in this file's lines
    sites_in_file = {l.site_id for l in lines if l.site_id}
    missing: list[MissingSite] = []
    for sid, info in sorted(sites_master.items()):
        if sid not in sites_in_file:
            missing.append(
                MissingSite(
                    site_id=sid,
                    site_name=(info or {}).get("name", ""),
                    status=(info or {}).get("status", "tenanted"),
                )
            )

    other_counts: dict[str, int] = {}
    for m in mismatches:
        if m.type in ("wrong_tenant_price", "wrong_fb_price"):
            continue
        other_counts[m.type] = other_counts.get(m.type, 0) + 1

    return Summary(
        file_name=file_name,
        line_count=len(lines),
        mismatch_count=len(mismatches),
        tenant_blocks=tenant_blocks,
        fb_blocks=fb_blocks,
        missing_sites=missing,
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
                parts.append(
                    f"<tr class='{cls}'>"
                    f"<td>{escape(r.product_code)}</td>"
                    f"<td>{escape(r.product_desc)}</td>"
                    f"<td class='r'>{_money_neutral(r.expected)}</td>"
                    f"<td class='r'>{_money_neutral(r.actual)}</td>"
                    f"<td class='r'>{r.qty:g}</td>"
                    f"<td class='r'>{_money(r.delta_per_unit)}</td>"
                    f"<td class='r'><strong>{_money(r.delta_total)}</strong></td>"
                    f"</tr>"
                )
            parts.append("</tbody></table></details>")

    parts.append("<h2>2. FB Taverns pricing mismatches — by product</h2>")
    if not s.fb_blocks:
        parts.append("<p><em>No FB pricing mismatches.</em></p>")
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
            sites_attr = "; ".join(f"{sid} {name} (qty {q:g})" for sid, name, q in b.sites)
            parts.append(
                f"<tr class='{cls}'>"
                f"<td>{escape(b.product_code)}</td>"
                f"<td>{escape(b.product_desc)}</td>"
                f"<td class='r'>{_money_neutral(b.expected)}</td>"
                f"<td class='r'>{_money_neutral(b.actual)}</td>"
                f"<td class='r'>{_money(b.delta_per_unit)}</td>"
                f"<td class='r' title='{escape(sites_attr)}'>{len(b.sites)}</td>"
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

    if s.other_counts:
        parts.append("<h2>Other findings</h2>")
        parts.append("<div class='result'>")
        for t, c in sorted(s.other_counts.items(), key=lambda kv: -kv[1]):
            parts.append(f"<div class='summary-row'><span>{escape(t)}</span><strong>{c}</strong></div>")
        parts.append("</div>")

    return "\n".join(parts)

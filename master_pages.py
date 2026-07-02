"""
master_pages.py — server-rendered HTML bodies + form codec for the /master*
pricing-master editor routes (design docs/master-editor-design.md §4, §6 Phase 2).

Split out of webapp.py so the route handlers there stay thin (auth, cache,
error handling); everything here is presentation + form parsing over the
master_changes seam. NO Airtable I/O in this module — every function works
from a MasterSnapshot / MasterChange / ChangePreview / ChangeResult already
in hand, so the /master/apply result page can render without touching
load_master_snapshot() (the 30s-inline-rebuild trap, design §3.4).

House rules honoured here:
  - every emitted href/action goes through ext_url() (the app is proxied
    under EXTERNAL_BASE_PATH=/drinks — bare paths break behind the hub);
  - fragments only: callers wrap with render_head()/PAGE_FOOT;
  - all user/Airtable-derived text is escape()d;
  - existing HEAD_STYLE classes only (.result, .summary-row, .pill,
    .master-banner, .grid2, .help, table td.r) plus tr.ended added there.
"""

from __future__ import annotations

from datetime import date
from html import escape
from urllib.parse import quote, urlencode

import airtable_io
from airtable_io import BASE_ID, MasterSnapshot
from auth_supabase import ext_url
from master_changes import (
    VALID_OPS,
    VALID_STATUSES,
    ChangePreview,
    ChangeResult,
    MasterChange,
    _contains,
    compute_margin,
    margin_of,
)
from reconcile import Rule, _parse_date

AIRTABLE_BASE_URL = f"https://airtable.com/{BASE_ID}"

PAGE_SIZE = 200  # §4.1: "Next 200 →" links over the in-memory snapshot

# §2.1: "open" (valid_to empty = on the master) and "effective on D" (half-open
# containment) are DIFFERENT views and must be labelled distinctly.
VIEW_LABELS = {
    "active": "Open (on the master)",
    "effective_on": "Effective on date…",
    "future": "Future-dated",
    "ended": "Ended",
    "all": "All",
    "recent": "Recent changes",
}

OP_LABELS = {
    "price_change": "Change price from a date",
    "fix_in_place": "Fix a mistake in place (rewrites history)",
    "end_rule": "End rule (delist)",
    "add_rule": "Add a rule",
}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def rule_key_of(rule: Rule) -> str:
    return airtable_io._rule_key(rule.site_id, rule.product_code, rule.valid_from)


def find_rule(snap: MasterSnapshot, rule_key: str) -> Rule | None:
    """Resolve a rule_key against the snapshot's Rule objects (keys are derived,
    not parsed — product codes could in principle contain anything)."""
    for r in snap.rules:
        if rule_key_of(r) == rule_key:
            return r
    return None


def _money(v: float | None) -> str:
    return "—" if v is None else f"£{v:,.2f}"


def _frac_str(v: float | None) -> str:
    """Retro fraction for display/prefill. 10dp is the CANONICAL storage
    precision (Rule.to_row uses :.10f) — trimming trailing zeros is not
    rounding. Never convert to a percentage on a write path."""
    if not v:
        return ""
    return f"{v:.10f}".rstrip("0").rstrip(".")


def _retro_disp(v: float | None) -> str:
    """Fraction shown as a % (0.125 -> 12.5%). Kept for the retro-fraction hint;
    the grid/forms show the £/keg figure via _retro_gbp instead."""
    if not v:
        return ""
    return f"{v * 100:.8g}%"


def retro_gbp(rule) -> float | None:
    """The retro as £ per keg — the figure the Excel carries (col 3) and the
    team thinks in. Stored on the Rule as a fraction of the FB list price
    (retro_pct = retro_per_keg / fb_price), so £ = retro_pct × fb_price."""
    if not rule.retro_pct or rule.fb_price is None:
        return None
    return rule.retro_pct * rule.fb_price


def net_price(rule) -> float | None:
    """FB net-net price per keg = list price − retro (Excel col 4)."""
    if rule.fb_price is None:
        return None
    return rule.fb_price * (1.0 - (rule.retro_pct or 0.0))


def _date_str(d: date | None, empty: str = "") -> str:
    return d.isoformat() if d else empty


def _margin_cell(rule) -> str:
    """Grid Margin cell: net £/keg + gross-margin %, retro-inclusive. Negative
    margin (selling under cost) is flagged red; the hover carries the full
    breakdown (pre-retro £ and net cost). Blank when it can't be computed."""
    m = margin_of(rule)
    if m.net_gbp is None:
        return '<td class="r">—</td>'
    pct = f" ({m.pct:.1f}%)" if m.pct is not None else ""
    colour = "#b00020" if m.net_gbp < 0 else ("#7a6a00" if (m.pct is not None and m.pct < 10) else "#1f7a1f")
    parts = []
    if m.gross_gbp is not None and rule.retro_pct:
        parts.append(f"vs list £{m.gross_gbp:,.2f}")
    if m.net_cost is not None:
        parts.append(f"net price £{m.net_cost:,.2f}")
    title = " · ".join(parts)
    return (
        f'<td class="r" title="{escape(title)}">'
        f'<strong style="color:{colour}">£{m.net_gbp:,.2f}</strong>'
        f'<span style="color:#666;font-size:0.85em">{pct}</span></td>'
    )


def _hidden(fields: dict[str, str]) -> str:
    return "".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">'
        for k, v in fields.items()
    )


def _status_select(current: str) -> str:
    opts = "".join(
        f'<option value="{s}"{" selected" if s == current else ""}>{s}</option>'
        for s in VALID_STATUSES
    )
    return f'<select name="status">{opts}</select>'


def _warnings_html(warnings: list[str]) -> str:
    if not warnings:
        return ""
    items = "".join(f"<li>{escape(w)}</li>" for w in warnings)
    return (
        '<div class="master-banner"><strong>Warnings</strong> (do not block):'
        f'<ul style="margin:0.4em 0 0">{items}</ul></div>'
    )


def errors_html(errors: list[str]) -> str:
    """Blocking-error block for the preview/apply refusal pages. Content is
    escaped here; safe to hand to webapp._error_page."""
    items = "".join(f"<li>{escape(e)}</li>" for e in errors)
    return (
        "<p>This change cannot be applied:</p>"
        f"<ul>{items}</ul>"
        "<p>Use your browser's Back button to correct the form.</p>"
    )


def _product_descs(snap: MasterSnapshot) -> dict[str, str]:
    """product_code -> best-known description (from rules; codes with no rule
    yet still appear via snap.product_ids)."""
    out: dict[str, str] = {code: "" for code in snap.product_ids}
    for r in snap.rules:
        if r.product_code and r.product_desc and not out.get(r.product_code):
            out[r.product_code] = r.product_desc
    return out


# ---------------------------------------------------------------------------
# Form <-> MasterChange codec (design §3.3: /master/apply re-parses hidden
# inputs and re-validates — the SAME parser serves both POSTs)
# ---------------------------------------------------------------------------

def parse_master_change_form(form) -> tuple[MasterChange | None, list[str]]:
    """Parse a /master/preview or /master/apply form into a MasterChange.

    Returns (change, parse_errors). Parse errors are the pre-semantic layer
    (unparseable numbers/dates, missing op) — semantic invariants live in
    validate_master_change. ``site_id_new`` / ``product_code_new`` (the add
    form's free-entry alternative) override the selects when non-blank.
    Floats go through float() untouched — retro_pct is NEVER rounded.
    """
    errors: list[str] = []

    def _get(name: str) -> str:
        return (form.get(name) or "").strip()

    op = _get("op")
    if op not in VALID_OPS:
        return None, [f"unknown or missing operation {op!r}"]

    def _float(name: str) -> float | None:
        s = _get(name)
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            errors.append(f"{name}: could not parse number {s!r}")
            return None

    def _date_field(name: str) -> date | None:
        s = _get(name)
        if not s:
            return None
        d = _parse_date(s)
        if d is None:
            errors.append(f"{name}: could not parse date {s!r}")
        return d

    def _flag(name: str) -> bool:
        return _get(name).lower() in ("1", "on", "true", "yes")

    tenant_price = _float("tenant_price")
    fb_price = _float("fb_price")

    # Retro: the edit/add forms collect a fixed £/keg figure (retro_gbp — the
    # Excel's "Retro P/Keg"); the confirm page's hidden fields carry the exact
    # stored fraction (retro_pct) so confirm→apply is bit-exact. Prefer the
    # fraction when present; otherwise convert £ → fraction of the FB list price.
    retro_pct = _float("retro_pct")
    if retro_pct is None:
        retro_gbp_in = _float("retro_gbp")
        if retro_gbp_in is not None:
            if fb_price and fb_price > 0:
                retro_pct = retro_gbp_in / fb_price
            else:
                errors.append(
                    "retro (£/keg) needs the FB list price to store it — enter the "
                    "FB list price too, or leave retro blank"
                )

    change = MasterChange(
        op=op,  # type: ignore[arg-type]
        site_id=_get("site_id_new") or _get("site_id"),
        product_code=_get("product_code_new") or _get("product_code"),
        product_desc=_get("product_desc") or None,
        tenant_price=tenant_price,
        fb_price=fb_price,
        retro_pct=retro_pct,
        status=_get("status") or "tenanted",
        valid_from=_date_field("valid_from"),
        valid_to=_date_field("valid_to"),
        reason=(form.get("reason") or "").strip(),
        create_missing_site=_flag("create_missing_site"),
        create_missing_product=_flag("create_missing_product"),
    )
    return change, errors


def change_to_hidden_fields(change: MasterChange) -> dict[str, str]:
    """Serialise a MasterChange for the confirm page's hidden inputs. Floats
    use repr(), which round-trips float64 exactly — retro_pct survives to
    10dp+ unrounded."""
    return {
        "op": change.op,
        "site_id": str(change.site_id),
        "product_code": str(change.product_code),
        "product_desc": change.product_desc or "",
        "tenant_price": "" if change.tenant_price is None else repr(change.tenant_price),
        "fb_price": "" if change.fb_price is None else repr(change.fb_price),
        "retro_pct": "" if change.retro_pct is None else repr(change.retro_pct),
        "status": change.status,
        "valid_from": _date_str(change.valid_from),
        "valid_to": _date_str(change.valid_to),
        "reason": change.reason,
        "create_missing_site": "1" if change.create_missing_site else "",
        "create_missing_product": "1" if change.create_missing_product else "",
    }


# ---------------------------------------------------------------------------
# /master — the Excel-style pivot (products x sites), the default view
# ---------------------------------------------------------------------------

def _pivot_winners(snap: MasterSnapshot, on: date) -> dict[tuple, Rule]:
    """Current winning rule per (site_id, product_code) on ``on`` — the newest
    valid_from among rules whose half-open window contains ``on``. This is the
    SAME selection the reconciler and the list grid use, so the pivot shows the
    price that would actually bill today."""
    per_key: dict[tuple, list[Rule]] = {}
    for r in snap.rules:
        if _contains(r.valid_from, r.valid_to, on):
            per_key.setdefault((r.site_id, r.product_code), []).append(r)
    winners: dict[tuple, Rule] = {}
    for k, lst in per_key.items():
        lst.sort(key=lambda r: r.valid_from or date.min, reverse=True)
        winners[k] = lst[0]
    return winners


def _pivot_cell(winner: Rule | None) -> str:
    """One site×product cell. Renders BOTH the tenant price and the £ margin
    (with % subline); the toggle swaps which is visible via a table class, so
    no round-trip. Blank when the product isn't priced at that site."""
    if winner is None or winner.tenant_price is None:
        return '<td class="num"><span class="pivot-empty">·</span></td>'
    price = f'<span class="cell-price">{escape(_money(winner.tenant_price))}</span>'
    m = margin_of(winner)
    if m.net_gbp is None:
        margin = '<span class="cell-margin pivot-empty">n/a</span>'
    else:
        cls = "cell-neg" if m.net_gbp < 0 else (
            "cell-warn" if (m.pct is not None and m.pct < 10) else "cell-pos"
        )
        pct = (
            f'<span class="pct">{m.pct:.1f}%</span>' if m.pct is not None
            else '<span class="pct">n/a</span>'
        )
        margin = f'<span class="cell-margin {cls}">{escape(_money(m.net_gbp))}{pct}</span>'
    return f'<td class="num">{price}{margin}</td>'


def render_master_pivot(
    snap: MasterSnapshot, params: dict, is_admin: bool, banner_html: str = ""
) -> str:
    """Excel-style master: products down the rows, sites across the columns.
    Each cell = the CURRENT (today-winning) tenant price; the toggle flips every
    cell to its £ margin (tenant − net-of-retro FB cost) with the % underneath.
    Read-only — history and per-rule edits live on the list view (?view=list)."""
    q = (params.get("q") or "").strip()
    q_low = q.lower()
    today = date.today()
    winners = _pivot_winners(snap, today)
    products = getattr(snap, "products", {}) or {}

    def _site_name(sid: str) -> str:
        return (snap.sites.get(sid) or {}).get("name") or ""

    # Columns = sites carrying ≥1 current rule, ascending by site id — the same
    # inclusion + order as /export-master (master_export.py), i.e. the Excel's.
    site_ids = sorted({s for (s, _p) in winners})

    # Per-product left columns (Code / Name / Price / Retro P/Keg / Net price —
    # the Excel's cols 0-4). Retro P/Keg is the PRODUCT-level fixed £ from the
    # Products table, exactly as the export writes it. fb_price lives on rules
    # (per site×product); collect the DISTINCT values so we show a single figure
    # only when consistent and flag "varies" otherwise — never an arbitrary,
    # order-dependent pick. Deterministic iteration via sorted().
    prod_agg: dict[str, dict] = {}
    for (s, p), w in sorted(winners.items()):
        agg = prod_agg.setdefault(p, {"desc": "", "fbs": set()})
        if not agg["desc"] and w.product_desc:
            agg["desc"] = w.product_desc
        if w.fb_price is not None:
            agg["fbs"].add(round(w.fb_price, 4))
    # The Excel also lists products that carry a retro but no current tenant
    # price anywhere (export: codes_active | codes_retro) — keep those rows.
    for code, info in products.items():
        if (info.get("retro_per_keg") or 0) > 0 and code not in prod_agg:
            prod_agg[code] = {"desc": info.get("desc") or "", "fbs": set()}
    for code, agg in prod_agg.items():
        if not agg["desc"]:
            agg["desc"] = (products.get(code) or {}).get("desc") or ""

    def _row_match(p: str) -> bool:
        if not q_low:
            return True
        return q_low in f"{p} {prod_agg.get(p, {}).get('desc', '')}".lower()

    # Row order = the export's (and the cost file's): alphabetical by product
    # NAME, case-insensitive, code as tiebreaker.
    prod_codes = sorted(
        (p for p in prod_agg if _row_match(p)),
        key=lambda c: ((prod_agg[c]["desc"] or "").upper(), c),
    )

    n_sites, n_prods = len(site_ids), len(prod_codes)

    # ---- toolbar (toggle + product search + link to the list/history view) ----
    q_attr = escape(q)
    clear = (
        f' <a href="{ext_url("/master")}" style="font-size:0.85em">clear</a>' if q else ""
    )
    toolbar = f"""
<div class="pivot-toolbar">
  <button type="button" id="pivot-toggle" class="toggle">Show margins</button>
  <form method="get" action="{ext_url('/master')}">
    <input type="search" name="q" value="{q_attr}" placeholder="Filter products…">
    <button type="submit">Search</button>{clear}
  </form>
  <span class="grow"></span>
  <span class="help" style="margin:0">{n_prods} products × {n_sites} sites · prices current as of {today.isoformat()}</span>
  <a href="{ext_url('/master')}?view=list">Detailed list / history →</a>
</div>"""

    if not prod_codes or not site_ids:
        empty = (
            "No products match this filter." if q else
            "No current prices in the master yet."
        )
        return (
            f'<div class="pivot-wide"><h1>Pricing master</h1>{banner_html}'
            f"{toolbar}<p class=\"help\">{escape(empty)}</p></div>"
        )

    # ---- header row (the Excel's: Code | Name | Price | Retro P/Keg | Net price | sites…) ----
    head_sites = "".join(
        f'<th class="num site" title="{escape(sid)} — {escape(_site_name(sid))}">'
        f'{escape(_site_name(sid) or sid)}<span class="sid">{escape(sid)}</span></th>'
        for sid in site_ids
    )
    thead = (
        '<thead><tr>'
        '<th class="sticky-col c1">Product Code</th>'
        '<th class="sticky-col c2">Product Name</th>'
        '<th class="num">Price</th><th class="num">Retro P/Keg</th><th class="num">Net price</th>'
        f'{head_sites}</tr></thead>'
    )

    # ---- body rows ----
    varies = '<span class="pivot-empty" title="differs across sites — see each cell">varies</span>'

    def _left_cells(p: str, agg: dict) -> tuple[str, str, str]:
        """Price / Retro P/Keg / Net price — the Excel's cols 2-4. Retro is the
        product-level £/keg (Products.retro_per_keg), matching the export. Price
        shows a single figure only when consistent across sites ('varies'
        otherwise); Net = Price − Retro, as in the cost file."""
        fbs = agg["fbs"]
        fb = next(iter(fbs)) if len(fbs) == 1 else None
        retro = (products.get(p) or {}).get("retro_per_keg") or 0.0
        retro_c = escape(_money(retro)) if retro else "—"
        if len(fbs) > 1:
            return varies, retro_c, varies
        if fb is None:
            return "—", retro_c, "—"
        return escape(_money(fb)), retro_c, escape(_money(fb - retro))

    body: list[str] = []
    for p in prod_codes:
        agg = prod_agg[p]
        price_c, retro_c, net_c = _left_cells(p, agg)
        prod_cells = (
            f'<td class="sticky-col c1 pcode">{escape(p)}</td>'
            f'<td class="sticky-col c2">{escape(agg["desc"] or "")}</td>'
        )
        pinfo = (
            f'<td class="num pinfo">{price_c}</td>'
            f'<td class="num pinfo">{retro_c}</td>'
            f'<td class="num pinfo">{net_c}</td>'
        )
        cells = "".join(_pivot_cell(winners.get((sid, p))) for sid in site_ids)
        body.append(f"<tr>{prod_cells}{pinfo}{cells}</tr>")

    table = (
        f'<div class="pivot-wrap"><table class="pivot" id="pivot-tbl">'
        f'{thead}<tbody>{"".join(body)}</tbody></table></div>'
    )

    legend = (
        '<p class="help" style="margin-top:0">Margin view: '
        '<span class="cell-pos">green</span> ≥10% · '
        '<span class="cell-warn">amber</span> &lt;10% · '
        '<span class="cell-neg">red</span> loss (selling under the net-of-retro FB cost). '
        'Blank cell = product not priced at that site.</p>'
    )

    toggle_js = (
        "<script>(function(){var t=document.getElementById('pivot-tbl'),"
        "b=document.getElementById('pivot-toggle');if(!t||!b)return;"
        "b.addEventListener('click',function(){var on=t.classList.toggle('show-margin');"
        "b.textContent=on?'Show prices':'Show margins';});})();</script>"
    )

    return (
        f'<div class="pivot-wide"><h1>Pricing master</h1>{banner_html}'
        f'{toolbar}{table}{legend}{toggle_js}</div>'
    )


# ---------------------------------------------------------------------------
# /master?view=list — the detailed rule grid (§4.1)
# ---------------------------------------------------------------------------

def render_master_grid(
    snap: MasterSnapshot, params: dict, is_admin: bool, banner_html: str = ""
) -> str:
    q = (params.get("q") or "").strip()
    site_f = (params.get("site") or "").strip()
    status_f = (params.get("status") or "").strip()
    show = (params.get("show") or "active").strip()
    if show not in VIEW_LABELS:
        show = "active"
    on_raw = (params.get("on") or "").strip()
    on_d = _parse_date(on_raw) if on_raw else None
    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    today = date.today()
    # Reference date for the "which rule wins" indicator (§2.1: overlaps are
    # legal and load-bearing — never forbidden, only explained).
    if show == "effective_on":
        ref_date: date | None = on_d or today
    elif show == "active":
        ref_date = today
    else:
        ref_date = None

    q_low = q.lower()

    def _match(r: Rule) -> bool:
        if site_f and r.site_id != site_f:
            return False
        if status_f and (r.status or "tenanted") != status_f:
            return False
        if q_low:
            name = (snap.sites.get(r.site_id) or {}).get("name", "")
            hay = f"{r.site_id} {name} {r.product_code} {r.product_desc}".lower()
            if q_low not in hay:
                return False
        if show == "active":
            return r.valid_to is None
        if show == "ended":
            return r.valid_to is not None
        if show == "future":
            return r.valid_from is not None and r.valid_from > today
        if show == "effective_on":
            return _contains(r.valid_from, r.valid_to, ref_date)  # type: ignore[arg-type]
        return True  # all / recent

    shown = [r for r in snap.rules if _match(r)]

    # Winner per (site, product) on the reference date — computed over the WHOLE
    # snapshot (an overlapping support may be filtered out of view but still
    # wins). Newest valid_from first, mirroring reconcile._index_rules.
    winners: dict[tuple, Rule] = {}
    if ref_date is not None:
        per_key: dict[tuple, list[Rule]] = {}
        for r in snap.rules:
            if _contains(r.valid_from, r.valid_to, ref_date):
                per_key.setdefault((r.site_id, r.product_code), []).append(r)
        for key, lst in per_key.items():
            if len(lst) > 1:
                lst.sort(key=lambda r: r.valid_from or date.min, reverse=True)
                winners[key] = lst[0]

    rule_created = getattr(snap, "rule_created", {}) or {}
    if show == "recent":
        shown.sort(
            key=lambda r: (
                rule_created.get(rule_key_of(r)) or "",
                _date_str(r.valid_from),
            ),
            reverse=True,
        )
    else:
        shown.sort(key=lambda r: r.valid_from or date.min, reverse=True)
        shown.sort(key=lambda r: (r.site_id, r.product_code))

    total = len(shown)
    page = shown[offset : offset + PAGE_SIZE]

    # ---- rows ----
    body_rows: list[str] = []
    for r in page:
        key = rule_key_of(r)
        kq = quote(key, safe="")
        site_name = (snap.sites.get(r.site_id) or {}).get("name", "")
        ended = r.valid_to is not None
        pills = ""
        if r.status == "supported":
            pills += ' <span class="pill">supported</span>'
        if r.valid_from is not None and r.valid_from > today:
            pills += ' <span class="pill">future</span>'
        if ref_date is not None and winners.get((r.site_id, r.product_code)) is r:
            pills += f' <span class="pill">wins on {ref_date.isoformat()}</span>'
        src = r.source or ""
        src_disp = escape(src[:24] + "…") if len(src) > 24 else escape(src)
        src_cell = f'<span title="{escape(src)}">{src_disp}</span>'
        action_cell = ""
        if is_admin:
            edit_url = ext_url("/master/edit") + "?rule_key=" + kq
            links = f'<a href="{edit_url}">Edit</a>'
            if not ended:
                end_url = ext_url("/master/end") + "?rule_key=" + kq
                links += f' · <a href="{end_url}">End</a>'
            action_cell = f"<td>{links}</td>"
        status_disp = escape(r.status or "tenanted")
        body_rows.append(
            f'<tr class="{"ended" if ended else ""}">'
            f"<td>{escape(r.site_id)} {escape(site_name)}</td>"
            f"<td>{escape(r.product_code)} <span style=\"color:#666\">{escape(r.product_desc or '')}</span></td>"
            f'<td class="r">{_money(r.fb_price)}</td>'
            f'<td class="r">{_money(retro_gbp(r))}</td>'
            f'<td class="r">{_money(net_price(r))}</td>'
            f'<td class="r">{_money(r.tenant_price)}</td>'
            f"{_margin_cell(r)}"
            f"<td>{_date_str(r.valid_from, 'open')}</td>"
            f"<td>{_date_str(r.valid_to, '—')}</td>"
            f"<td>{status_disp}{pills}</td>"
            f"<td>{src_cell}</td>"
            f"{action_cell}</tr>"
        )
    action_head = "<th>Actions</th>" if is_admin else ""
    table = (
        "<table><thead><tr><th>Site</th><th>Product</th>"
        '<th class="r" title="FB list price per keg">Price £</th>'
        '<th class="r" title="Retro rebate per keg (fixed £)">Retro £/keg</th>'
        '<th class="r" title="Net price = list − retro (Excel col 4)">Net price £</th>'
        '<th class="r">Tenant £</th>'
        '<th class="r" title="FB margin per keg = tenant − net price, and % of the tenant price">Margin</th>'
        f"<th>Valid from</th><th>Valid to</th><th>Status</th><th>Source</th>{action_head}"
        "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table>"
        if body_rows
        else '<p class="help">No rules match this view/filter.</p>'
    )

    # ---- filter form ----
    site_opts = ['<option value="">All sites</option>']
    for sid in sorted(snap.sites):
        name = (snap.sites.get(sid) or {}).get("name", "")
        sel = " selected" if sid == site_f else ""
        site_opts.append(
            f'<option value="{escape(sid)}"{sel}>{escape(sid)} — {escape(name)}</option>'
        )
    status_opts = ['<option value="">All statuses</option>'] + [
        f'<option value="{s}"{" selected" if s == status_f else ""}>{s}</option>'
        for s in VALID_STATUSES
    ]
    show_opts = [
        f'<option value="{k}"{" selected" if k == show else ""}>{escape(v)}</option>'
        for k, v in VIEW_LABELS.items()
    ]
    filter_form = f"""
<form method="get" action="{ext_url('/master')}" style="max-width:none">
  <div style="display:flex; gap:1em; flex-wrap:wrap; align-items:flex-end">
    <div><label for="mq">Search</label>
      <input type="text" name="q" id="mq" value="{escape(q)}" placeholder="site / product / text" style="padding:0.45em"></div>
    <div><label for="msite">Site</label><select name="site" id="msite" style="margin-bottom:0">{''.join(site_opts)}</select></div>
    <div><label for="mstatus">Status</label><select name="status" id="mstatus" style="margin-bottom:0">{''.join(status_opts)}</select></div>
    <div><label for="mshow">View</label><select name="show" id="mshow" style="margin-bottom:0">{''.join(show_opts)}</select></div>
    <div><label for="mon">On date</label>
      <input type="date" name="on" id="mon" value="{escape(on_raw)}" style="padding:0.45em"></div>
    <div><button type="submit">Filter</button></div>
  </div>
  <p class="help" style="margin:0.6em 0 0"><strong>Open</strong> = valid_to empty (counts toward active membership) —
  distinct from <strong>Effective on date</strong> = the half-open window contains that date
  (valid_from ≤ D &lt; valid_to). “On date” applies to the Effective-on view only.</p>
</form>"""

    # ---- view-specific notes ----
    notes = ""
    if show == "recent":
        notes = (
            '<p class="help">Recent = record <em>creation</em> order (Airtable createdTime). '
            "In-place fixes don’t bump a row’s creation time — for field-level history use "
            f'<a href="{AIRTABLE_BASE_URL}" target="_blank">Airtable’s revision history</a>.</p>'
        )
    elif ref_date is not None and winners:
        notes = (
            f'<p class="help">Overlapping rules exist for some (site, product) pairs — the rule marked '
            f'<span class="pill">wins on {ref_date.isoformat()}</span> is the one reconciliation uses on that date '
            "(newest valid_from first). Overlaps are how temporary supports work; they are deliberate.</p>"
        )

    admin_buttons = ""
    if is_admin:
        # /add-support is a POST endpoint — its form lives on /lwc, so the
        # support button links there (design said "link to /add-support";
        # verified against webapp.py: there is no GET form at that path).
        admin_buttons = (
            f'<p><a class="button" href="{ext_url("/master/add")}">Add a rule</a> '
            f'<a class="button" href="{ext_url("/lwc")}" style="background:#666">Add a temporary support →</a></p>'
        )

    # ---- pagination ----
    base_qs = {k: v for k, v in (("q", q), ("site", site_f), ("status", status_f), ("show", show), ("on", on_raw)) if v}

    def _page_url(off: int) -> str:
        qs = dict(base_qs)
        if off:
            qs["offset"] = str(off)
        tail = "?" + urlencode(qs) if qs else ""
        return ext_url("/master") + tail

    nav = []
    if offset > 0:
        nav.append(f'<a href="{_page_url(max(0, offset - PAGE_SIZE))}">← Previous {PAGE_SIZE}</a>')
    if offset + PAGE_SIZE < total:
        nav.append(f'<a href="{_page_url(offset + PAGE_SIZE)}">Next {PAGE_SIZE} →</a>')
    nav_html = f"<p>{' · '.join(nav)}</p>" if nav else ""

    first = 0 if total == 0 else offset + 1
    last = min(offset + PAGE_SIZE, total)
    count_line = (
        f'<p class="help">Showing {first}–{last} of {total} rules · view: '
        f"<strong>{escape(VIEW_LABELS[show])}</strong></p>"
    )

    back = f'<p class="sub" style="margin-top:0"><a href="{ext_url("/lwc")}">← Back to LWC</a></p>'
    return f"""{back}
<h1>Pricing master — rules</h1>
{banner_html}
<p class="help">Served from the cached master snapshot — fresh edits can take up to a minute to appear here.
Exports and reconciliations always read fresh.</p>
{admin_buttons}
{filter_form}
{count_line}
{notes}
<div style="overflow-x:auto">{table}</div>
{nav_html}
<p style="margin-top:1.5em"><a class="button" href="{AIRTABLE_BASE_URL}" target="_blank">Open Airtable base</a></p>
"""


# ---------------------------------------------------------------------------
# Rule summary block (edit/end pages)
# ---------------------------------------------------------------------------

def _rule_current_block(snap: MasterSnapshot, rule: Rule) -> str:
    site_name = (snap.sites.get(rule.site_id) or {}).get("name", "")
    _rg = retro_gbp(rule)
    retro_line = _money(_rg) if _rg is not None else "—"
    m = margin_of(rule)
    if m.net_gbp is None:
        margin_line = "—"
    else:
        pct = f" ({m.pct:.1f}%)" if m.pct is not None else ""
        extra = f" · net cost £{m.net_cost:,.2f}" if m.net_cost is not None else ""
        loss = " — loss-making" if m.net_gbp < 0 else ""
        margin_line = f"£{m.net_gbp:,.2f}{pct}{extra}{loss}"
    rows = [
        ("Rule key", f"<code>{escape(rule_key_of(rule))}</code>"),
        ("Site", escape(f"{rule.site_id} {site_name}".strip())),
        ("Product", escape(f"{rule.product_code} {rule.product_desc or ''}".strip())),
        ("FB (list) price", escape(_money(rule.fb_price))),
        ("Retro (£/keg)", escape(retro_line)),
        ("Net price (list − retro)", escape(_money(net_price(rule)))),
        ("Tenant price", escape(_money(rule.tenant_price))),
        ("Margin (on net price)", escape(margin_line)),
        ("Valid from", escape(_date_str(rule.valid_from, "open (no start date)"))),
        ("Valid to", escape(_date_str(rule.valid_to, "open — on the master"))),
        ("Status", escape(rule.status or "tenanted")),
        ("Reason", escape(rule.reason or "—")),
        ("Source", escape(rule.source or "—")),
    ]
    rows_html = "".join(
        f'<div class="summary-row"><span>{k}</span><span>{v}</span></div>' for k, v in rows
    )
    return f'<div class="result" style="max-width:none">{rows_html}</div>'


# ---------------------------------------------------------------------------
# /master/edit — two forms (§4.2a)
# ---------------------------------------------------------------------------

def render_edit_page(snap: MasterSnapshot, rule: Rule) -> str:
    key = rule_key_of(rule)
    common_hidden = {
        "site_id": rule.site_id,
        "product_code": rule.product_code,
        "product_desc": rule.product_desc or "",
    }
    tenant_val = "" if rule.tenant_price is None else f"{rule.tenant_price:.2f}"
    fb_val = "" if rule.fb_price is None else f"{rule.fb_price:.2f}"
    _rg = retro_gbp(rule)
    retro_val = "" if _rg is None else f"{_rg:.2f}"
    today_iso = date.today().isoformat()
    cur_status = rule.status if rule.status in VALID_STATUSES else "tenanted"
    preview_url = ext_url("/master/preview")

    # (a) Change price from a date — the standard forward change: closes the
    # current open rule at D, creates the successor from D. Default = TODAY
    # (deliberately NOT /lwc's today-14d default, which exists for whole-file
    # re-uploads only — design §2.2).
    form_a = f"""
<form method="post" action="{preview_url}">
  <h3>Change price from a date</h3>
  <p class="help">The normal case: the price genuinely changed. The current rule is closed at the
  effective date and a new rule takes over from it (an invoice dated exactly that day gets the <strong>new</strong> price).
  History is preserved.</p>
  {_hidden({**common_hidden, "op": "price_change"})}
  <label for="pc-tp">New tenant price (£)</label>
  <input type="number" step="0.01" min="0" name="tenant_price" id="pc-tp" value="{escape(tenant_val)}" required style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="pc-fb">FB list price (£, optional)</label>
  <input type="number" step="0.01" min="0" name="fb_price" id="pc-fb" value="{escape(fb_val)}" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="pc-retro">Retro (£ per keg — optional)</label>
  <input type="number" step="0.01" min="0" name="retro_gbp" id="pc-retro" value="{escape(retro_val)}" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <p class="help">The fixed £ rebate per keg (the Excel's "Retro P/Keg"). Net price = FB list − retro. Blank = no retro on the new rule.</p>
  <label for="pc-status">Status</label>
  {_status_select(cur_status)}
  <label for="pc-vf" style="margin-top:1em">Effective from</label>
  <input type="date" name="valid_from" id="pc-vf" value="{today_iso}" required style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="pc-reason">Reason (required)</label>
  <textarea name="reason" id="pc-reason" required placeholder="e.g. LWC list increase Jul-26"></textarea>
  <button type="submit" style="margin-top:1em">Preview change</button>
</form>"""

    # (b) Fix a mistake — same (site,product,valid_from) key => in-place PATCH.
    # Status MUST be prefilled with the rule's current status: Rule.status is
    # always written, so a price-only fix would otherwise silently reset a
    # supported/managed rule to "tenanted".
    form_b = f"""
<form method="post" action="{preview_url}">
  <h3>Fix a mistake <span class="support-tag">REWRITES HISTORY</span></h3>
  <p class="help"><strong>The old figure is treated as never true.</strong> The rule keeps its key and dates —
  only the figures are rewritten. Already-recorded mismatches are NOT recomputed, and re-uploading an
  affected weekly file will create duplicate mismatch rows.</p>
  {_hidden({**common_hidden, "op": "fix_in_place", "valid_from": _date_str(rule.valid_from)})}
  <label for="fx-tp">Corrected tenant price (£)</label>
  <input type="number" step="0.01" min="0" name="tenant_price" id="fx-tp" value="{escape(tenant_val)}" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="fx-fb">Corrected FB list price (£, optional)</label>
  <input type="number" step="0.01" min="0" name="fb_price" id="fx-fb" value="{escape(fb_val)}" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="fx-retro">Corrected retro (£ per keg — leave blank to keep current{f", currently £{escape(retro_val)}" if retro_val else ""})</label>
  <input type="number" step="0.01" min="0" name="retro_gbp" id="fx-retro" value="" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <p class="help">The fixed £ rebate per keg. Blank keeps the stored retro. (Entering 0 cannot clear a stored retro — the write path skips zeros.)</p>
  <label for="fx-status">Status</label>
  {_status_select(cur_status)}
  <label for="fx-reason" style="margin-top:1em">Reason (required)</label>
  <textarea name="reason" id="fx-reason" required placeholder="e.g. typo in the June upload — was £180, should be £182"></textarea>
  <button type="submit" style="margin-top:1em">Preview fix</button>
</form>"""

    back = f'<p class="sub" style="margin-top:0"><a href="{ext_url("/master")}?view=list">← Back to master (list)</a></p>'
    return f"""{back}
<h1>Edit rule</h1>
<h2 style="margin-top:0.6em">Current values</h2>
{_rule_current_block(snap, rule)}
<p class="help" style="margin-top:1em">Pick the operation that matches what happened. <strong>Change price from a date</strong> when the
price genuinely changed; <strong>Fix a mistake</strong> when the stored figure was never right. To move a rule's start date,
end the rule and add a new one (changing valid_from would change its identity).</p>
<div class="grid2" style="margin-top:1em">
{form_a}
{form_b}
</div>
<p style="margin-top:1.5em"><a href="{ext_url('/master/end')}?rule_key={quote(key, safe='')}">End (delist) this rule instead →</a></p>
"""


# ---------------------------------------------------------------------------
# /master/end — end-rule form (§4.2b)
# ---------------------------------------------------------------------------

def render_end_page(snap: MasterSnapshot, rule: Rule) -> str:
    hidden = {
        "op": "end_rule",
        "site_id": rule.site_id,
        "product_code": rule.product_code,
        "valid_from": _date_str(rule.valid_from),
    }
    today_iso = date.today().isoformat()
    back = f'<p class="sub" style="margin-top:0"><a href="{ext_url("/master")}?view=list">← Back to master (list)</a></p>'
    return f"""{back}
<h1>End rule</h1>
<h2 style="margin-top:0.6em">Current values</h2>
{_rule_current_block(snap, rule)}
<form method="post" action="{ext_url('/master/preview')}" style="margin-top:1em">
  <h3>End (delist) this rule</h3>
  <p class="help">This removes the product from active membership <strong>immediately</strong> — future deliveries at this
  site will flag as missing. Use for genuine delists or to repair an open-ended support rule. Nothing new is created.</p>
  <label for="er-vt">End date (valid_to)</label>
  <input type="date" name="valid_to" id="er-vt" value="{today_iso}" required style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <p class="help">Half-open: a delivery dated exactly the end date is <strong>not</strong> covered by this rule.</p>
  {_hidden(hidden)}
  <label for="er-reason">Reason (required)</label>
  <textarea name="reason" id="er-reason" required placeholder="e.g. delisted — site no longer stocks this line"></textarea>
  <button type="submit" style="margin-top:1em">Preview end</button>
</form>
"""


# ---------------------------------------------------------------------------
# /master/add — add-rule form (§4.2c)
# ---------------------------------------------------------------------------

def render_add_page(snap: MasterSnapshot) -> str:
    today_iso = date.today().isoformat()
    site_opts = ['<option value="">— pick a site —</option>'] + [
        f'<option value="{escape(sid)}">{escape(sid)} — {escape((snap.sites.get(sid) or {}).get("name", ""))}</option>'
        for sid in sorted(snap.sites)
    ]
    descs = _product_descs(snap)
    prod_opts = ['<option value="">— pick a product —</option>'] + [
        f'<option value="{escape(code)}">{escape(code)} — {escape(descs.get(code) or "")}</option>'
        for code in sorted(descs)
    ]
    back = f'<p class="sub" style="margin-top:0"><a href="{ext_url("/master")}?view=list">← Back to master (list)</a></p>'
    return f"""{back}
<h1>Add a rule</h1>
<p class="help">A new (site, product) pricing rule. For a <em>temporary</em> price that layers over the standard rule,
use the tenant-support form on the <a href="{ext_url('/lwc')}">LWC page</a> instead.</p>
<form method="post" action="{ext_url('/master/preview')}" style="max-width:640px">
  {_hidden({"op": "add_rule"})}
  <label for="ar-site">Site</label>
  <select name="site_id" id="ar-site">{''.join(site_opts)}</select>
  <label for="ar-site-new">…or a new site id (e.g. 812)</label>
  <input type="text" name="site_id_new" id="ar-site-new" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:0.4em">
  <p class="help"><label style="display:inline; font-weight:400"><input type="checkbox" name="create_missing_site" value="1">
  Create this site (defaults: status=tenanted, country=england)</label></p>
  <label for="ar-prod">Product</label>
  <select name="product_code" id="ar-prod">{''.join(prod_opts)}</select>
  <label for="ar-prod-new">…or a new product code</label>
  <input type="text" name="product_code_new" id="ar-prod-new" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:0.4em">
  <label for="ar-desc">Product description (required for a new product)</label>
  <input type="text" name="product_desc" id="ar-desc" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:0.4em">
  <p class="help"><label style="display:inline; font-weight:400"><input type="checkbox" name="create_missing_product" value="1">
  Create this product (default: supplier=LWC)</label></p>
  <label for="ar-tp">Tenant price (£)</label>
  <input type="number" step="0.01" min="0" name="tenant_price" id="ar-tp" required style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="ar-fb">FB list price (£, optional)</label>
  <input type="number" step="0.01" min="0" name="fb_price" id="ar-fb" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="ar-retro">Retro (£ per keg — optional)</label>
  <input type="number" step="0.01" min="0" name="retro_gbp" id="ar-retro" style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="ar-status">Status</label>
  {_status_select("tenanted")}
  <label for="ar-vf" style="margin-top:1em">Valid from</label>
  <input type="date" name="valid_from" id="ar-vf" value="{today_iso}" required style="padding:0.45em; width:100%; box-sizing:border-box; margin-bottom:1em">
  <label for="ar-reason">Reason (required)</label>
  <textarea name="reason" id="ar-reason" required placeholder="e.g. new line for Bell 804 from July"></textarea>
  <button type="submit" style="margin-top:1em">Preview rule</button>
</form>
"""


# ---------------------------------------------------------------------------
# /master/preview — POST-echo confirm page (§4.3; doubles as phase-2 review)
# ---------------------------------------------------------------------------

def _preview_detail_rows(preview: ChangePreview) -> str:
    rows: list[str] = []

    def _retro_money(d: dict) -> str:
        """Retro as £/keg (fraction × FB list). Falls back to the fraction if the
        FB list price is absent so the figure is never silently dropped."""
        r = d.get("retro_pct")
        if not r:
            return "—"
        fb = d.get("fb_price")
        return f"£{r * fb:,.2f}" if fb else _frac_str(r)

    def _fields_desc(d: dict) -> str:
        bits = []
        if d.get("tenant_price") is not None:
            bits.append(f"tenant {_money(d['tenant_price'])}")
        if d.get("fb_price") is not None:
            bits.append(f"FB {_money(d['fb_price'])}")
        if d.get("retro_pct"):
            bits.append(f"retro {_retro_money(d)}/keg")
        if d.get("status"):
            bits.append(str(d["status"]))
        return escape(" · ".join(bits))

    for c in preview.will_close:
        rows.append(
            f'<div class="summary-row"><span>Close <code>{escape(str(c.get("rule_key")))}</code></span>'
            f'<span>valid_to → <strong>{escape(str(c.get("valid_to")))}</strong></span></div>'
        )
    for c in preview.will_create:
        vf = c.get("valid_from") or "open"
        vt = c.get("valid_to")
        window = f"from {vf}" + (f" to {vt}" if vt else " (open-ended)")
        rows.append(
            f'<div class="summary-row"><span>Create <code>{escape(str(c.get("rule_key")))}</code> {escape(window)}</span>'
            f"<span>{_fields_desc(c)}</span></div>"
        )
    for u in preview.will_update:
        old, new = u.get("old") or {}, u.get("new") or {}
        diffs = []
        for k in ("tenant_price", "fb_price", "retro_pct", "status", "valid_to", "valid_from"):
            if old.get(k) != new.get(k):
                if k in ("tenant_price", "fb_price"):
                    diffs.append(f"{k}: {_money(old.get(k))} → {_money(new.get(k))}")
                elif k == "retro_pct":
                    diffs.append(f"retro: {_retro_money(old)} → {_retro_money(new)}")
                else:
                    diffs.append(f"{k}: {old.get(k) or '—'} → {new.get(k) or '—'}")
        key = (old.get("rule_key") or new.get("rule_key") or "")
        rows.append(
            f'<div class="summary-row"><span>Update <code>{escape(str(key))}</code> in place</span>'
            f"<span>{escape(' · '.join(diffs)) or 'no field changes'}</span></div>"
        )
    margin_row = _preview_margin_row(preview)
    if margin_row:
        rows.append(margin_row)
    if preview.winner_note:
        rows.append(
            f'<div class="summary-row"><span>Who wins</span><span>{escape(preview.winner_note)}</span></div>'
        )
    return "".join(rows)


def _preview_margin_row(preview: ChangePreview) -> str:
    """Resulting FB margin (net of retro) of the change — old → new for a
    fix-in-place, the new figure for a create/price change. Flags a loss."""
    def _disp(fields: dict | None):
        if not fields:
            return None, None
        m = compute_margin(
            fields.get("tenant_price"), fields.get("fb_price"), fields.get("retro_pct")
        )
        if m.net_gbp is None:
            return None, m
        pct = f" ({m.pct:.1f}%)" if m.pct is not None else ""
        return f"£{m.net_gbp:,.2f}{pct}", m

    new_fields = preview.will_create[0] if preview.will_create else (
        preview.will_update[0].get("new") if preview.will_update else None
    )
    old_fields = preview.will_update[0].get("old") if preview.will_update else None
    new_disp, new_m = _disp(new_fields)
    if new_disp is None:
        return ""
    old_disp, _ = _disp(old_fields)
    val = f"{old_disp} → {new_disp}" if old_disp else new_disp
    warn = ' <span style="color:#b00020">— loss-making</span>' if new_m.net_gbp < 0 else ""
    return f'<div class="summary-row"><span>Margin (net of retro)</span><span>{escape(val)}{warn}</span></div>'


def render_preview_page(change: MasterChange, preview: ChangePreview) -> str:
    op_label = OP_LABELS.get(preview.op, preview.op)
    back = f'<p class="sub" style="margin-top:0"><a href="{ext_url("/master")}?view=list">← Back to master (list)</a></p>'
    head = f"""{back}
<h1>Confirm change</h1>
<p class="sub">{escape(op_label)} — review below. <strong>Nothing has been written yet.</strong></p>"""

    if preview.errors:
        return f"""{head}
<div class="result err">{errors_html(preview.errors)}</div>
{_warnings_html(preview.warnings)}
<p><a href="{ext_url('/master')}">Cancel and go back to the master</a></p>
"""

    summary = f'<div class="summary-row"><span>Summary</span><span>{escape(preview.summary)}</span></div>'
    confirm_form = f"""
<form method="post" action="{ext_url('/master/apply')}" style="background:none; border:0; padding:0; max-width:none">
  {_hidden(change_to_hidden_fields(change))}
  <button type="submit">Confirm change</button>
  <a href="{ext_url('/master')}" style="margin-left:1em">Cancel</a>
</form>"""
    return f"""{head}
<div class="result" style="max-width:none">
{summary}
{_preview_detail_rows(preview)}
<div class="summary-row"><span>Reason</span><span>{escape(change.reason)}</span></div>
</div>
{_warnings_html(preview.warnings)}
{confirm_form}
"""


# ---------------------------------------------------------------------------
# /master/apply — result page (§3.4/§4.3: rendered from in-hand data ONLY,
# never load_master_snapshot) and the partial-failure page (§3.5)
# ---------------------------------------------------------------------------

def render_result_page(
    change: MasterChange, preview: ChangePreview, result: ChangeResult
) -> str:
    op_label = OP_LABELS.get(change.op, change.op)
    keys = "".join(
        f"<div class='summary-row'><span>Rule key</span><code>{escape(k)}</code></div>"
        for k in result.rule_keys_touched
    )
    return f"""
<h1>Change applied</h1>
<p class="sub">{escape(op_label)} · {escape(preview.summary)}</p>
<div class="result">
  <div class="summary-row"><span>Rules created</span><strong>{result.created}</strong></div>
  <div class="summary-row"><span>Rules updated</span><strong>{result.updated}</strong></div>
  <div class="summary-row"><span>Rules closed</span><strong>{result.closed}</strong></div>
  {keys}
</div>
<p class="help" style="margin-top:1em">The master list is served from a cached snapshot — this change may take up to a
minute to appear there (a background refresh has been kicked off). Exports and new reconciliations pick it up immediately.</p>
<p style="margin-top:1.5em">
  <a class="button" href="{ext_url('/master')}?view=list">Back to master (list)</a>
  <a class="button" href="{AIRTABLE_BASE_URL}" target="_blank" style="background:#666">Open Airtable</a>
</p>
"""


def render_apply_failure(change: MasterChange, preview: ChangePreview) -> str:
    """§3.5: the upsert closes BEFORE it creates, with no transaction — say
    exactly which rule may have been closed without a successor and link to
    repair. Returned as an (escaped) fragment for webapp._error_page."""
    lines = ["<p>The write failed partway through and has been logged.</p>"]
    if preview.will_close:
        items = []
        for c in preview.will_close:
            key = str(c.get("rule_key") or "")
            url = ext_url("/master/edit") + "?rule_key=" + quote(key, safe="")
            items.append(
                f'<li><code>{escape(key)}</code> may have been CLOSED at '
                f'{escape(str(c.get("valid_to")))} without its successor being created — '
                f'<a href="{url}">check &amp; repair</a></li>'
            )
        lines.append(
            "<p>Because rules are closed before the replacement is created, the following "
            f"may now be closed with no successor:</p><ul>{''.join(items)}</ul>"
        )
    else:
        lines.append("<p>No prior rule was due to be closed by this change, so no gap can have been left.</p>")
    lines.append(
        f'<p>Verify in <a href="{AIRTABLE_BASE_URL}" target="_blank">Airtable</a> before retrying.</p>'
    )
    return "".join(lines)

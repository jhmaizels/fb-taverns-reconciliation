"""
Price history for both masters — what changed, when, by whom, and what it
was before — with undo and roll-back to an earlier date.

There is no separate history table. Every single-price write already leaves
its trail on the row it changes, and this module reads that trail:

LWC (PricingRules). A price change never overwrites: it creates a new rule
dated the day of the change and closes the old one, so the old price is the
closed predecessor's. A correction on the same day IS an overwrite, and
master_changes.apply_master_change stamps it into the reason as
"corrected by <email> <date>: was £X". Clearing a cell ends the rule with no
successor. So, per (site, product):
  - a rule created with a different tenant price from its predecessor → set
  - each "corrected by" stamp → set (was £X → the next value in the chain)
  - a rule ended with no rule starting where it ends → removed
Cost-only successors (same tenant price, new FB price/retro) are not tenant
price events and are left out. Temporary supports are left out too: they are
changed on the support page, and undoing one from a cell would edit the
support rather than the standing price.

Tennent's. TennentsSitePrices and SKU_Master rows carry their changes in
`notes`, newest first, " | "-separated:
  "Bar plan change <date> by <who>: off-invoice £a → £b/brl."   (a pub's split)
  "Price grid <date> by <who>: total £a → £b/brl, WSP £c → £d/brl."  (product)
A workbook re-upload re-creates rows from the workbook, so history before a
re-upload survives only on rows the upload kept (bar plan and price grid
rows are kept by design).

Undo puts the cell back to the figure before its LATEST change, through the
same planners and writers the grids use, so an undo is itself a recorded
change and can be undone. Roll-back takes a date and, for every cell changed
on or after it, puts back the figure it had at the start of that day.

Pure: derivation and HTML only. The routes in webapp.py load the masters and
apply the changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape
from urllib.parse import urlencode

from auth_supabase import ext_url
from reconcile import select_winners

# kind of cell an event belongs to
LWC_PRICE = "price"      # LWC tenant price for (site, product)
TN_OFF = "off"           # Tennent's off-invoice £/brl for (account, sku)
TN_TOTAL = "total"       # Tennent's agreed total discount for a sku (estate-wide)
TN_WSP = "wsp"           # Tennent's WSP for a sku (estate-wide)

TOL = 0.005


@dataclass
class PriceEvent:
    kind: str
    site_id: str              # LWC site id / Tennent's account; "" for a product-level event
    site_name: str
    product_code: str
    product_desc: str
    on: date
    who: str
    before: float | None      # None = no price (not on the master / RATE TBC / none)
    after: float | None       # None = removed
    order: tuple = ()         # chronological sort key within the cell
    latest: bool = False      # newest event of its cell
    current: float | None = None   # what the cell shows now
    undoable: bool = False

    @property
    def cell(self) -> tuple:
        return (self.kind, self.site_id, self.product_code)


@dataclass
class RollbackItem:
    kind: str
    site_id: str
    site_name: str
    product_code: str
    product_desc: str
    current: float | None
    target: float | None
    changes: int              # how many changes on/after the date this reverses
    blocked: str = ""         # why it can't be put back automatically ("" = it can)


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < TOL


def _gbp(s: str) -> float | None:
    s = (s or "").strip()
    if not s.startswith("£"):
        return None
    try:
        return float(s[1:].replace(",", ""))
    except ValueError:
        return None


def _who(source: str) -> str:
    """The person in a 'note:email' source stamp, else the source as written
    (a cost file name, a findings stamp)."""
    s = (source or "").strip()
    for part in reversed(s.replace(" ", ":").split(":")):
        if "@" in part:
            return part
    return s


def _finish(events: list[PriceEvent], current_of) -> list[PriceEvent]:
    """Order each cell's events, mark the newest, and decide which can be undone:
    only the newest change of a cell, and only while the cell still shows the
    figure that change set (anything else means it has moved since)."""
    by_cell: dict[tuple, list[PriceEvent]] = {}
    for e in events:
        by_cell.setdefault(e.cell, []).append(e)
    for cell, lst in by_cell.items():
        lst.sort(key=lambda e: e.order)
        cur = current_of(cell)
        for e in lst:
            e.current = cur
        last = lst[-1]
        last.latest = True
        last.undoable = _same(cur, last.after) and not _same(last.before, last.after)
    return events


# ---------------------------------------------------------------------------
# LWC
# ---------------------------------------------------------------------------

_FIX_RE = re.compile(
    r"corrected by (\S+) (\d{4}-\d{2}-\d{2}): was (£[\d,]+(?:\.\d+)?|unset)"
)


def _parse_iso(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def lwc_events(snap, today: date | None = None) -> list[PriceEvent]:
    """Every tenant-price change recorded in the LWC master, oldest first per
    cell. See the module docstring for how each kind is recognised."""
    today = today or date.today()
    created = getattr(snap, "rule_created", {}) or {}
    rules = [r for r in snap.rules if (r.status or "tenanted") != "supported"]
    by_cell: dict[tuple, list] = {}
    for r in rules:
        by_cell.setdefault((r.site_id, r.product_code), []).append(r)

    def _name(sid: str) -> str:
        return (snap.sites.get(sid) or {}).get("name", "")

    def _key(r) -> str:
        vf = r.valid_from.isoformat() if r.valid_from else "open"
        return f"{r.site_id}|{r.product_code}|{vf}"

    events: list[PriceEvent] = []
    for (sid, code), lst in by_cell.items():
        starts = {r.valid_from for r in lst}
        for r in lst:
            ct = created.get(_key(r)) or ""
            rank = ct or (r.valid_from.isoformat() if r.valid_from else "")
            stamps = list(reversed(_FIX_RE.findall(r.reason or "")))   # oldest first
            values = [None if w == "unset" else _gbp(w) for _, _, w in stamps] + [r.tenant_price]
            initial = values[0]
            base = dict(kind=LWC_PRICE, site_id=sid, site_name=_name(sid),
                        product_code=code, product_desc=r.product_desc or "")

            # the rule's creation, against the rule it took over from
            pred = None
            if r.valid_from is not None:
                preds = [p for p in lst if p is not r and p.valid_to == r.valid_from]
                if preds:
                    pred = max(preds, key=lambda p: created.get(_key(p)) or "")
            before = pred.tenant_price if pred is not None else None
            if not _same(before, initial):
                on = _parse_iso(ct) or r.valid_from or today
                events.append(PriceEvent(
                    **base, on=on, who=_who(r.source) if not stamps else "",
                    before=before, after=initial, order=(rank, 0)))

            # same-day corrections
            for i, (email, d, _was) in enumerate(stamps):
                if _same(values[i], values[i + 1]):
                    continue
                events.append(PriceEvent(
                    **base, on=_parse_iso(d) or today, who=email,
                    before=values[i], after=values[i + 1], order=(rank, 1 + i)))

            # removed: ended with nothing starting where it ends
            if r.valid_to is not None and r.valid_to not in starts and r.tenant_price is not None:
                on = r.valid_to
                if r.valid_from is not None and r.valid_to == r.valid_from + timedelta(days=1):
                    on = r.valid_from       # a rule removed on the day it started
                events.append(PriceEvent(
                    **base, on=on, who=_who(r.source),
                    before=r.tenant_price, after=None, order=(rank, 999)))

    winners = select_winners(snap.rules, today)

    def _current(cell):
        w = winners.get((cell[1], cell[2]))
        if w is None:
            return None
        if (w.status or "tenanted") == "supported":
            return float("nan")     # a support is showing: never "the same as" anything
        return w.tenant_price

    return _finish(events, _current)


# ---------------------------------------------------------------------------
# Tennent's
# ---------------------------------------------------------------------------

_BP_RE = re.compile(
    r"Bar plan change (\d{4}-\d{2}-\d{2}) by (.*?): off-invoice (none|£[\d,]+\.\d{2}) → (£[\d,]+\.\d{2})/brl"
)
_PG_RE = re.compile(r"Price grid (\d{4}-\d{2}-\d{2}) by (.*?): (.*)")
_PG_TOTAL = re.compile(r"total (RATE TBC|£[\d,]+\.\d{2}) → (£[\d,]+\.\d{2})/brl")
_PG_WSP = re.compile(r"WSP (none|£[\d,]+\.\d{2}) → (£[\d,]+\.\d{2})/brl")


def tennents_events(master) -> list[PriceEvent]:
    events: list[PriceEvent] = []

    def _sku_name(code: str) -> str:
        sku = master.find_sku(code)
        if sku is None:
            return ""
        return " ".join(x for x in (sku.product, sku.container) if x)

    for sp in master.site_prices:
        canon = master.canonical_sku(sp.sku_code).strip().upper()
        site = master.site_for_account(sp.account)
        name = (site.site_name if site else "") or sp.site_name
        segs = [s.strip() for s in (sp.notes or "").split(" | ")]
        for i, seg in enumerate(reversed(segs)):
            m = _BP_RE.search(seg)
            if not m:
                continue
            d, who, was, now = m.groups()
            before = None if was == "none" else _gbp(was)
            events.append(PriceEvent(
                kind=TN_OFF, site_id=sp.account, site_name=name, product_code=canon,
                product_desc=sp.product or _sku_name(canon), on=_parse_iso(d) or date.min,
                who=who.strip(), before=before, after=_gbp(now), order=(d, i)))

    for sku in master.skus:
        segs = [s.strip() for s in (sku.notes or "").split(" | ")]
        for i, seg in enumerate(reversed(segs)):
            m = _PG_RE.search(seg)
            if not m:
                continue
            d, who, rest = m.groups()
            for kind, rx in ((TN_TOTAL, _PG_TOTAL), (TN_WSP, _PG_WSP)):
                mm = rx.search(rest)
                if not mm:
                    continue
                was, now = mm.groups()
                events.append(PriceEvent(
                    kind=kind, site_id="", site_name="", product_code=sku.sku_code.strip().upper(),
                    product_desc=_sku_name(sku.sku_code), on=_parse_iso(d) or date.min,
                    who=who.strip(), before=_gbp(was), after=_gbp(now), order=(d, i)))

    def _current(cell):
        kind, acct, code = cell
        if kind == TN_OFF:
            sp = master.site_price(acct, code)
            return None if sp is None else float(sp.off_invoice_per_brl or 0.0)
        sku = master.find_sku(code)
        if sku is None:
            return None
        v = sku.correct_total_per_brl if kind == TN_TOTAL else sku.wsp_per_brl
        return None if v is None else round(float(v), 2)

    events = _finish(events, _current)
    for e in events:
        # a pub's figure with no row reads "none", which is the same as £0 on
        # the price file; a product figure can never go back to blank
        if e.kind == TN_OFF and e.before is None:
            e.before = 0.0
            e.undoable = e.latest and _same(e.current, e.after) and not _same(e.after, 0.0)
        if e.kind in (TN_TOTAL, TN_WSP) and e.before is None:
            e.undoable = False
    return events


# ---------------------------------------------------------------------------
# filtering and roll-back
# ---------------------------------------------------------------------------

def filter_events(events: list[PriceEvent], since: date | None = None, site: str = "",
                  q: str = "") -> list[PriceEvent]:
    ql = q.strip().lower()
    out = []
    for e in events:
        if since is not None and e.on < since:
            continue
        if site and e.site_id != site:
            continue
        if ql and ql not in f"{e.site_id} {e.site_name} {e.product_code} {e.product_desc} {e.who}".lower():
            continue
        out.append(e)
    out.sort(key=lambda e: (e.on, e.order), reverse=True)
    return out


def plan_rollback(events: list[PriceEvent], since: date, site: str = "") -> list[RollbackItem]:
    """Every cell changed on or after ``since`` (at ``site``, when given) and
    what it showed before the first of those changes."""
    by_cell: dict[tuple, list[PriceEvent]] = {}
    for e in events:
        if e.on >= since and (not site or e.site_id == site):
            by_cell.setdefault(e.cell, []).append(e)
    items: list[RollbackItem] = []
    for cell, lst in by_cell.items():
        lst.sort(key=lambda e: e.order)
        first = lst[0]
        target = first.before
        cur = first.current
        if _same(cur, target):
            continue
        blocked = ""
        if cur is not None and cur != cur:          # NaN: a support is showing
            blocked = "a temporary support is showing here; change it on the support page"
        elif first.kind in (TN_TOTAL, TN_WSP) and target is None:
            blocked = "it had no figure before, and a product figure can't be set back to blank"
        items.append(RollbackItem(
            kind=first.kind, site_id=first.site_id, site_name=first.site_name,
            product_code=first.product_code, product_desc=first.product_desc,
            current=cur, target=target, changes=len(lst), blocked=blocked))
    items.sort(key=lambda i: (i.site_name, i.site_id, i.product_desc, i.kind))
    return items


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _money(v: float | None, none: str = "—") -> str:
    if v is None:
        return none
    if v != v:
        return "support"
    return f"£{v:,.2f}"


_KIND_LABEL = {LWC_PRICE: "Tenant price", TN_OFF: "Off-invoice", TN_TOTAL: "Total discount", TN_WSP: "WSP"}

_CSS = """<style>
table.hist{border-collapse:collapse;width:100%;font-size:0.92em}
table.hist th,table.hist td{border-bottom:1px solid #e3e3e3;padding:0.35em 0.5em;text-align:left;vertical-align:top}
table.hist td.r,table.hist th.r{text-align:right;white-space:nowrap}
table.hist form{margin:0;max-width:none}
table.hist button{margin:0;padding:0.25em 0.7em;font-size:0.9em}
.hist-note{color:#666;font-size:0.9em}
.hist-flash{background:#e8f5e9;border:1px solid #a5d6a7;padding:0.6em 0.8em;border-radius:4px}
.hist-err{background:#fdecea;border:1px solid #f5c2c0;padding:0.6em 0.8em;border-radius:4px}
</style>"""


def _hidden(fields: dict) -> str:
    return "".join(f'<input type="hidden" name="{escape(str(k))}" value="{escape(str(v))}">'
                   for k, v in fields.items())


def _value_field(v: float | None) -> str:
    return "" if v is None else f"{v:.2f}"


def render_history_page(*, master: str, events: list[PriceEvent], sites: list[tuple[str, str]],
                        params: dict, is_admin: bool, flash: str = "", error: str = "") -> str:
    """The history page body for one master ('lwc' or 'tennents')."""
    base = "/master/history" if master == "lwc" else "/tennents/history"
    grid = "/master" if master == "lwc" else "/tennents/master"
    title = "LWC price history" if master == "lwc" else "Tennent's price history"
    site_f = (params.get("site") or "").strip()
    q = (params.get("q") or "").strip()
    try:
        days = max(1, min(3650, int(params.get("days") or 30)))
    except ValueError:
        days = 30
    since = date.today() - timedelta(days=days - 1)
    shown = filter_events(events, since=since, site=site_f, q=q)

    site_opts = ['<option value="">All pubs</option>'] + [
        f'<option value="{escape(sid)}"{" selected" if sid == site_f else ""}>{escape(label)}</option>'
        for sid, label in sites
    ]
    day_opts = "".join(
        f'<option value="{d}"{" selected" if d == days else ""}>{label}</option>'
        for d, label in ((1, "Today"), (7, "Last 7 days"), (30, "Last 30 days"),
                         (90, "Last 90 days"), (365, "Last year"), (3650, "Everything"))
    )
    keep = {"site": site_f, "q": q, "days": str(days)}

    rows = []
    for e in shown:
        where = escape(e.site_name or e.site_id) if e.site_id else "<em>all pubs</em>"
        prod = f'{escape(e.product_code)} <span class="hist-note">{escape(e.product_desc)}</span>'
        if e.after is None:
            what = "removed"
        elif e.before is None or (e.kind == TN_OFF and _same(e.before, 0.0)):
            what = "added"
        else:
            what = "changed"
        act = ""
        if is_admin and e.undoable:
            label = "Undo: remove" if e.before is None or (e.kind == TN_OFF and _same(e.before, 0.0)) \
                else f"Undo: put back {_money(e.before)}"
            act = (
                f'<form method="post" action="{ext_url(base + "/undo")}">'
                + _hidden({"kind": e.kind, "site_id": e.site_id, "product_code": e.product_code,
                           "to": _value_field(e.before), "expect": _value_field(e.after), **keep})
                + f'<button type="submit">{escape(label)}</button></form>'
            )
        elif e.latest and not _same(e.current, e.after):
            act = f'<span class="hist-note">now {_money(e.current, "no price")}</span>'
        rows.append(
            f"<tr><td>{e.on.strftime('%d %b %Y')}</td><td>{where}</td><td>{prod}</td>"
            f"<td>{escape(_KIND_LABEL.get(e.kind, e.kind))}</td>"
            f'<td class="r">{_money(e.before, "none")}</td><td class="r">{_money(e.after, "removed")}</td>'
            f"<td>{escape(what)}</td><td>{escape(e.who)}</td><td>{act}</td></tr>"
        )
    table = (
        '<table class="hist"><thead><tr><th>Date</th><th>Pub</th><th>Product</th><th>Figure</th>'
        '<th class="r">Before</th><th class="r">After</th><th></th><th>By</th><th></th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table>"
    ) if rows else '<p class="help">No price changes recorded for this filter.</p>'

    rollback = ""
    if is_admin:
        rollback = f"""
<h2 style="margin-top:1.6em">Roll back to an earlier date</h2>
<form method="post" action="{ext_url(base + '/rollback')}" style="max-width:none">
  <div style="display:flex; gap:1em; flex-wrap:wrap; align-items:flex-end">
    <div><label>Put prices back as they stood at the start of</label>
      <input type="date" name="since" required value="{date.today().isoformat()}" style="padding:0.45em"></div>
    <div><label>Pub</label><select name="site" style="margin-bottom:0">{''.join(site_opts)}</select></div>
    <div><button type="submit">Show what would change</button></div>
  </div>
  <p class="help" style="margin:0.6em 0 0">Nothing is changed until you confirm on the next page.
  Every change made on or after that date is reversed, cell by cell.</p>
</form>"""

    note = (
        "History is read from the price rows themselves: each change keeps the price it replaced. "
        "Undo is offered on the newest change of a price while the grid still shows it."
    )
    if master == "lwc":
        note += " Temporary supports and cost-price (FB price / retro) changes are not listed here."
    else:
        note += (" Only changes made in the app are listed (the price grid, the bar plan page, the July "
                 "restore); figures loaded by uploading the master workbook are not.")

    banner = ""
    if flash:
        banner += f'<p class="hist-flash">{escape(flash)}</p>'
    if error:
        banner += f'<p class="hist-err">{escape(error)}</p>'

    return f"""{_CSS}
<p class="sub" style="margin-top:0"><a href="{ext_url(grid)}">← Back to the price grid</a></p>
<h1>{escape(title)}</h1>
{banner}
<form method="get" action="{ext_url(base)}" style="max-width:none">
  <div style="display:flex; gap:1em; flex-wrap:wrap; align-items:flex-end">
    <div><label>Pub</label><select name="site" onchange="this.form.submit()" style="margin-bottom:0">{''.join(site_opts)}</select></div>
    <div><label>Period</label><select name="days" onchange="this.form.submit()" style="margin-bottom:0">{day_opts}</select></div>
    <div><label>Search</label><input type="text" name="q" value="{escape(q)}" placeholder="product / person" style="padding:0.45em"></div>
    <div><button type="submit">Filter</button></div>
  </div>
</form>
<p class="help">{escape(note)}</p>
<p class="help">{len(shown)} change{'s' if len(shown) != 1 else ''}.</p>
<div style="overflow-x:auto">{table}</div>
{rollback}
"""


def render_rollback_page(*, master: str, items: list[RollbackItem], since: date, site: str,
                         site_label: str) -> str:
    base = "/master/history" if master == "lwc" else "/tennents/history"
    ok = [i for i in items if not i.blocked]
    rows = []
    for i in items:
        where = escape(i.site_name or i.site_id) if i.site_id else "<em>all pubs</em>"
        status = escape(i.blocked) if i.blocked else ""
        rows.append(
            f"<tr><td>{where}</td><td>{escape(i.product_code)} <span class=\"hist-note\">{escape(i.product_desc)}</span></td>"
            f"<td>{escape(_KIND_LABEL.get(i.kind, i.kind))}</td>"
            f'<td class="r">{_money(i.current, "no price")}</td><td class="r">{_money(i.target, "no price")}</td>'
            f"<td class=\"r\">{i.changes}</td><td>{status}</td></tr>"
        )
    scope = escape(site_label) if site else "every pub"
    back = ext_url(base) + "?" + urlencode({"site": site, "days": "30"})
    if not items:
        body = '<p class="help">Nothing to put back: no price has changed since then.</p>'
    else:
        body = (
            '<table class="hist"><thead><tr><th>Pub</th><th>Product</th><th>Figure</th>'
            '<th class="r">Now</th><th class="r">Put back to</th><th class="r">Changes reversed</th><th></th>'
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
        if ok:
            body += (
                f'<form method="post" action="{ext_url(base + "/rollback/apply")}" style="max-width:none; margin-top:1em">'
                + _hidden({"since": since.isoformat(), "site": site})
                + f'<button type="submit">Put back {len(ok)} price{"s" if len(ok) != 1 else ""}</button> '
                f'<a href="{back}">Cancel</a></form>'
                '<p class="help">Each price put back is recorded as a new change, so this can itself be undone '
                "from the history page.</p>"
            )
    return f"""{_CSS}
<p class="sub" style="margin-top:0"><a href="{back}">← Back to the history</a></p>
<h1>Roll back to the start of {since.strftime('%d %b %Y')}</h1>
<p>For {scope}: every price changed on or after that date, and the figure it had before.</p>
{body}
"""

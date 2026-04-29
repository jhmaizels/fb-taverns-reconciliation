"""
Tennents Direct (Scottish estate) reconciliation.

Inputs:
  Master  : FB_Taverns_-_Commercial_Data.xlsx, sheet "FB Taverns Discount"
            98 rows of (Customer, SKU) discount agreements.
  Monthly : FB_Taverns_Draught_Pricing_Report_-_<MONTH>.xlsx, sheet "Data"
            Per-delivery line items.

Both files use compound identifiers — names with the ID in brackets:
  Customer "BELLS BAR (17591759)" → account 17591759
  SKU      "CALEDONIA BEST 3.2% 11G KEG (400076)" → SKU code 400076
The reconciliation joins on (account, sku_code).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from html import escape

import pandas as pd

# Per-keg unit-price tolerance (rounding artefacts on Net price)
TENNENTS_PRICE_TOLERANCE = 0.05
# Per-Brl discount tolerance (rounding compounds when spread over a barrel)
TENNENTS_DISCOUNT_TOLERANCE = 0.50


# ---------- compound-identifier extraction ----------

_ACCT_PAT = re.compile(r"\((\d+)\)")
_SKU_PAT = re.compile(r"\(([^)]+)\)\s*$")


def extract_account(s) -> str:
    if pd.isna(s):
        return ""
    m = _ACCT_PAT.search(str(s))
    return m.group(1) if m else ""


def extract_sku(s) -> str:
    if pd.isna(s):
        return ""
    m = _SKU_PAT.search(str(s))
    return m.group(1) if m else ""


def strip_account_suffix(s) -> str:
    """'BELLS BAR (17591759)' → 'BELLS BAR'."""
    if pd.isna(s):
        return ""
    return _ACCT_PAT.sub("", str(s)).strip()


def strip_sku_suffix(s) -> str:
    if pd.isna(s):
        return ""
    return _SKU_PAT.sub("", str(s)).strip()


# ---------- master parsing ----------

@dataclass
class Agreement:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    tenant_invoice: float
    fb_net_price: float
    off_invoice_per_brl: float
    retro_per_brl: float
    total_per_brl: float
    source: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.account, self.sku_code)

    @property
    def implied_total(self) -> float:
        return float(self.off_invoice_per_brl or 0) + float(self.retro_per_brl or 0)


def parse_master(path: str) -> list[Agreement]:
    df = pd.read_excel(path, sheet_name="FB Taverns Discount")
    df.columns = [str(c).strip() for c in df.columns]
    required = {
        "Customer Name",
        "SKU",
        "Tenant invoice",
        "FB net price",
        "Off invoice Discount per Brl",
        "Retro discount per Brl",
        "Total Discount per Brl",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Master file missing columns: {missing}")

    df = df.dropna(subset=["Customer Name", "SKU"])
    out: list[Agreement] = []
    for _, row in df.iterrows():
        account = extract_account(row["Customer Name"])
        sku_code = extract_sku(row["SKU"])
        if not account or not sku_code:
            continue
        out.append(
            Agreement(
                account=account,
                customer_name=strip_account_suffix(row["Customer Name"]),
                sku_code=sku_code,
                sku_desc=strip_sku_suffix(row["SKU"]),
                tenant_invoice=float(row["Tenant invoice"] or 0),
                fb_net_price=float(row["FB net price"] or 0),
                off_invoice_per_brl=float(row["Off invoice Discount per Brl"] or 0),
                retro_per_brl=float(row["Retro discount per Brl"] or 0),
                total_per_brl=float(row["Total Discount per Brl"] or 0),
            )
        )
    return out


# ---------- monthly parsing ----------

@dataclass
class DeliveryLine:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    kegs: float
    barrels: float
    invoice_price: float          # per case/keg
    net_price: float              # per keg
    off_invoice_per_brl: float
    retro_per_brl: float
    total_discount_per_brl: float


def parse_monthly(path: str) -> list[DeliveryLine]:
    df = pd.read_excel(path, sheet_name="Data")
    df.columns = [str(c).strip() for c in df.columns]
    required = {
        "Customer Name",
        "SKU",
        "Kegs",
        "Barrels",
        "Invoice Price (per case/keg)",
        "Net Price per keg",
        "Off invoice Discount per Brl",
        "Retro discount per Brl",
        "Total Discount per Brl",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Monthly file missing columns: {missing}")

    df = df.dropna(subset=["Customer Name", "SKU"])
    df = df[df["Kegs"] > 0]

    out: list[DeliveryLine] = []
    for _, row in df.iterrows():
        account = extract_account(row["Customer Name"])
        sku_code = extract_sku(row["SKU"])
        if not account or not sku_code:
            continue
        try:
            out.append(
                DeliveryLine(
                    account=account,
                    customer_name=strip_account_suffix(row["Customer Name"]),
                    sku_code=sku_code,
                    sku_desc=strip_sku_suffix(row["SKU"]),
                    kegs=float(row["Kegs"] or 0),
                    barrels=float(row["Barrels"] or 0),
                    invoice_price=float(row["Invoice Price (per case/keg)"] or 0),
                    net_price=float(row["Net Price per keg"] or 0),
                    off_invoice_per_brl=float(row["Off invoice Discount per Brl"] or 0),
                    retro_per_brl=float(row["Retro discount per Brl"] or 0),
                    total_discount_per_brl=float(row["Total Discount per Brl"] or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    if not out:
        raise ValueError("Monthly file produced zero delivery lines after parsing")
    return out


# ---------- summary shapes ----------

@dataclass
class TenantInvoiceMismatch:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    expected: float
    actual: float
    delta_per_unit: float
    kegs: float


@dataclass
class FBPriceMismatch:
    sku_code: str
    sku_desc: str
    expected: float
    actual: float
    delta_per_unit: float
    sites_affected: list[tuple[str, str, float]]  # (account, customer, kegs)
    total_kegs: float


@dataclass
class DiscountMismatch:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    expected: float
    actual: float
    delta_per_brl: float
    barrels: float
    delta_total: float


@dataclass
class NotOnMasterRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    kegs: float
    avg_invoice: float
    avg_discount_per_brl: float


@dataclass
class MasterArithmeticRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    off_plus_retro: float
    total: float
    delta: float


@dataclass
class TennentsSummary:
    file_name: str
    line_count: int
    invoice_mismatches: list[TenantInvoiceMismatch]
    fb_price_mismatches: list[FBPriceMismatch]
    discount_mismatches: list[DiscountMismatch]
    sites_did_not_buy: list[tuple[str, str]]    # (account, customer)
    not_on_master: list[NotOnMasterRow]
    master_arithmetic_errors: list[MasterArithmeticRow]
    customers_not_on_master: list[tuple[str, str]]  # (account, customer)
    total_discount_delta: float = 0.0


# ---------- reconciliation ----------

def _aggregate_by_key(lines: list[DeliveryLine]):
    """Per-(account,sku) aggregation: sum Kegs/Barrels, mean of rates."""
    by_key: dict[tuple[str, str], dict] = {}
    for l in lines:
        k = (l.account, l.sku_code)
        b = by_key.setdefault(k, {
            "customer_name": l.customer_name,
            "sku_desc": l.sku_desc,
            "kegs": 0.0, "barrels": 0.0,
            "invoice_sum": 0.0, "net_sum": 0.0,
            "off_sum": 0.0, "retro_sum": 0.0, "total_disc_sum": 0.0,
            "n": 0,
        })
        b["kegs"] += l.kegs
        b["barrels"] += l.barrels
        b["invoice_sum"] += l.invoice_price
        b["net_sum"] += l.net_price
        b["off_sum"] += l.off_invoice_per_brl
        b["retro_sum"] += l.retro_per_brl
        b["total_disc_sum"] += l.total_discount_per_brl
        b["n"] += 1
    out = {}
    for k, b in by_key.items():
        n = b["n"]
        out[k] = {
            "customer_name": b["customer_name"],
            "sku_desc": b["sku_desc"],
            "kegs": b["kegs"],
            "barrels": b["barrels"],
            "invoice_avg": b["invoice_sum"] / n,
            "net_avg": b["net_sum"] / n,
            "off_avg": b["off_sum"] / n,
            "retro_avg": b["retro_sum"] / n,
            "total_disc_avg": b["total_disc_sum"] / n,
        }
    return out


def reconcile(
    file_name: str,
    agreements: list[Agreement],
    delivery_lines: list[DeliveryLine],
    price_tolerance: float = TENNENTS_PRICE_TOLERANCE,
    discount_tolerance: float = TENNENTS_DISCOUNT_TOLERANCE,
) -> TennentsSummary:
    by_key = {a.key: a for a in agreements}
    agg = _aggregate_by_key(delivery_lines)

    invoice_mm: list[TenantInvoiceMismatch] = []
    fb_buckets: dict[tuple[str, float, float], FBPriceMismatch] = {}
    discount_mm: list[DiscountMismatch] = []
    not_on_master: list[NotOnMasterRow] = []

    for k, a in agg.items():
        master = by_key.get(k)
        if master is None:
            not_on_master.append(NotOnMasterRow(
                account=k[0],
                customer_name=a["customer_name"],
                sku_code=k[1],
                sku_desc=a["sku_desc"],
                kegs=a["kegs"],
                avg_invoice=a["invoice_avg"],
                avg_discount_per_brl=a["total_disc_avg"],
            ))
            continue

        # 1. Tenant invoice (per keg)
        d = a["invoice_avg"] - master.tenant_invoice
        if abs(d) > price_tolerance:
            invoice_mm.append(TenantInvoiceMismatch(
                account=k[0],
                customer_name=a["customer_name"],
                sku_code=k[1],
                sku_desc=a["sku_desc"],
                expected=master.tenant_invoice,
                actual=a["invoice_avg"],
                delta_per_unit=d,
                kegs=a["kegs"],
            ))

        # 2. FB net price (per keg) — aggregate by (sku, expected, actual)
        d = a["net_avg"] - master.fb_net_price
        if abs(d) > price_tolerance:
            bk = (k[1], round(master.fb_net_price, 4), round(a["net_avg"], 4))
            blk = fb_buckets.get(bk)
            if blk is None:
                blk = FBPriceMismatch(
                    sku_code=k[1],
                    sku_desc=a["sku_desc"],
                    expected=master.fb_net_price,
                    actual=a["net_avg"],
                    delta_per_unit=d,
                    sites_affected=[],
                    total_kegs=0.0,
                )
                fb_buckets[bk] = blk
            blk.sites_affected.append((k[0], a["customer_name"], a["kegs"]))
            blk.total_kegs += a["kegs"]

        # 3. Discount per Brl
        d = a["total_disc_avg"] - master.total_per_brl
        if abs(d) > discount_tolerance:
            discount_mm.append(DiscountMismatch(
                account=k[0],
                customer_name=a["customer_name"],
                sku_code=k[1],
                sku_desc=a["sku_desc"],
                expected=master.total_per_brl,
                actual=a["total_disc_avg"],
                delta_per_brl=d,
                barrels=a["barrels"],
                delta_total=d * a["barrels"],
            ))

    # 4. Sites in master that didn't buy this month
    delivered_accounts = {k[0] for k in agg}
    master_accounts = {a.account: a.customer_name for a in agreements}
    sites_did_not_buy = [
        (acct, name) for acct, name in sorted(master_accounts.items())
        if acct not in delivered_accounts
    ]

    # 6. Customers in monthly but not on master at all
    delivered_customers: dict[str, str] = {}
    for k, a in agg.items():
        delivered_customers.setdefault(k[0], a["customer_name"])
    customers_not_on_master = sorted(
        ((acct, name) for acct, name in delivered_customers.items()
         if acct not in master_accounts),
        key=lambda x: x[0],
    )

    # 5b. Master arithmetic errors (independent of monthly file).
    # Threshold £0.05 — anything tighter picks up floating-point artefacts
    # in the source spreadsheet rather than real bookkeeping errors.
    master_arith: list[MasterArithmeticRow] = []
    for ag in agreements:
        if abs(ag.total_per_brl - ag.implied_total) > 0.05:
            master_arith.append(MasterArithmeticRow(
                account=ag.account,
                customer_name=ag.customer_name,
                sku_code=ag.sku_code,
                sku_desc=ag.sku_desc,
                off_plus_retro=ag.implied_total,
                total=ag.total_per_brl,
                delta=ag.total_per_brl - ag.implied_total,
            ))

    invoice_mm.sort(key=lambda r: -abs(r.delta_per_unit * r.kegs))
    fb_blocks = sorted(fb_buckets.values(), key=lambda b: -abs(b.delta_per_unit * b.total_kegs))
    discount_mm.sort(key=lambda r: -abs(r.delta_total))
    not_on_master.sort(key=lambda r: -r.kegs)
    master_arith.sort(key=lambda r: -abs(r.delta))

    return TennentsSummary(
        file_name=file_name,
        line_count=len(delivery_lines),
        invoice_mismatches=invoice_mm,
        fb_price_mismatches=fb_blocks,
        discount_mismatches=discount_mm,
        sites_did_not_buy=sites_did_not_buy,
        not_on_master=not_on_master,
        master_arithmetic_errors=master_arith,
        customers_not_on_master=customers_not_on_master,
        total_discount_delta=sum(r.delta_total for r in discount_mm),
    )


# ---------- HTML rendering ----------

def _money(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}£{abs(v):,.2f}"


def _money_neutral(v: float) -> str:
    return f"£{v:,.2f}"


def render_summary_html(s: TennentsSummary) -> str:
    parts: list[str] = []

    parts.append(
        f"""<div class="result">
  <div class="summary-row"><span>File</span><code>{escape(s.file_name)}</code></div>
  <div class="summary-row"><span>Lines processed</span><strong>{s.line_count}</strong></div>
  <div class="summary-row"><span>Tenant invoice mismatches</span><strong>{len(s.invoice_mismatches)}</strong></div>
  <div class="summary-row"><span>FB net price mismatches</span><strong>{len(s.fb_price_mismatches)}</strong></div>
  <div class="summary-row"><span>Discount per Brl mismatches</span><strong>{len(s.discount_mismatches)}</strong></div>
  <div class="summary-row"><span>Total discount Δ</span><strong>{_money(s.total_discount_delta)}</strong></div>
</div>"""
    )

    parts.append("<h2>1. Tenant invoice mismatches — by site &amp; product</h2>")
    if not s.invoice_mismatches:
        parts.append("<p><em>None — every tenant invoice matched the master.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th><th>Description</th>"
            "<th class='r'>Master £</th><th class='r'>Charged £</th>"
            "<th class='r'>Δ / unit</th><th class='r'>Kegs</th>"
            "</tr></thead><tbody>"
        )
        for r in s.invoice_mismatches:
            cls = "neg" if r.delta_per_unit < 0 else "pos"
            parts.append(
                f"<tr class='{cls}'>"
                f"<td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)}</td><td>{escape(r.sku_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.expected)}</td>"
                f"<td class='r'>{_money_neutral(r.actual)}</td>"
                f"<td class='r'>{_money(r.delta_per_unit)}</td>"
                f"<td class='r'>{r.kegs:g}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>2. FB net price mismatches — by product</h2>")
    if not s.fb_price_mismatches:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Code</th><th>Description</th>"
            "<th class='r'>Expected FB</th><th class='r'>Charged FB</th>"
            "<th class='r'>Δ / unit</th><th class='r'>Sites</th><th class='r'>Total kegs</th>"
            "</tr></thead><tbody>"
        )
        for b in s.fb_price_mismatches:
            cls = "neg" if b.delta_per_unit < 0 else "pos"
            sites_attr = "; ".join(f"{a} {n} ({k:g} kegs)" for a, n, k in b.sites_affected)
            parts.append(
                f"<tr class='{cls}'>"
                f"<td>{escape(b.sku_code)}</td><td>{escape(b.sku_desc)}</td>"
                f"<td class='r'>{_money_neutral(b.expected)}</td>"
                f"<td class='r'>{_money_neutral(b.actual)}</td>"
                f"<td class='r'>{_money(b.delta_per_unit)}</td>"
                f"<td class='r' title='{escape(sites_attr)}'>{len(b.sites_affected)}</td>"
                f"<td class='r'>{b.total_kegs:g}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>3. Discount per Brl mismatches — by site &amp; product</h2>")
    parts.append(
        "<p class='sub'>The headline section for Tennents — most issues live here. "
        "Positive Δ = tenant got LESS discount than master expected; negative Δ = tenant got MORE. "
        "Tenant invoice and FB net are unchanged either way — these are master bookkeeping errors.</p>"
    )
    if not s.discount_mismatches:
        parts.append("<p><em>None — every discount matched the master.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Master £/Brl</th><th class='r'>Actual £/Brl</th>"
            "<th class='r'>Δ / Brl</th><th class='r'>Brl</th><th class='r'>Δ total</th>"
            "</tr></thead><tbody>"
        )
        for r in s.discount_mismatches:
            cls = "neg" if r.delta_total < 0 else "pos"
            parts.append(
                f"<tr class='{cls}'>"
                f"<td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{_money(r.expected)}</td>"
                f"<td class='r'>{_money(r.actual)}</td>"
                f"<td class='r'>{_money(r.delta_per_brl)}</td>"
                f"<td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'><strong>{_money(r.delta_total)}</strong></td>"
                f"</tr>"
            )
        parts.append(
            f"<tr><td colspan='7' class='r'><strong>Net total</strong></td>"
            f"<td class='r'><strong>{_money(s.total_discount_delta)}</strong></td></tr>"
        )
        parts.append("</tbody></table>")

    parts.append("<h2>4. Sites that didn't buy this month</h2>")
    if not s.sites_did_not_buy:
        parts.append("<p><em>None — every site on the master had at least one delivery.</em></p>")
    else:
        parts.append("<table><thead><tr><th>Account</th><th>Customer</th></tr></thead><tbody>")
        for acct, name in s.sites_did_not_buy:
            parts.append(f"<tr><td>{escape(acct)}</td><td>{escape(name)}</td></tr>")
        parts.append("</tbody></table>")

    parts.append("<h2>5. Other findings</h2>")
    parts.append(
        f"<h3>(Customer, SKU) not on master <span class='pill'>{len(s.not_on_master)}</span></h3>"
    )
    if not s.not_on_master:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append("<p class='sub'>Deliveries to a known customer for an SKU the master doesn't cover. Either extend the master to add the agreement, or treat as unauthorised.</p>")
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th><th>Description</th>"
            "<th class='r'>Kegs</th><th class='r'>Avg invoice</th><th class='r'>Avg discount £/Brl</th>"
            "</tr></thead><tbody>"
        )
        for r in s.not_on_master:
            parts.append(
                f"<tr><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)}</td><td>{escape(r.sku_desc)}</td>"
                f"<td class='r'>{r.kegs:g}</td>"
                f"<td class='r'>{_money_neutral(r.avg_invoice)}</td>"
                f"<td class='r'>{_money(r.avg_discount_per_brl)}</td></tr>"
            )
        parts.append("</tbody></table>")

    parts.append(
        f"<h3>Master Total ≠ Off + Retro <span class='pill'>{len(s.master_arithmetic_errors)}</span></h3>"
    )
    if not s.master_arithmetic_errors:
        parts.append("<p><em>None — every row's Total = Off + Retro.</em></p>")
    else:
        parts.append("<p class='sub'>Data-quality issue inside the master — Total Discount per Brl doesn't equal Off + Retro for these rows. The corrected value is Off + Retro.</p>")
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Off + Retro</th><th class='r'>Master Total</th><th class='r'>Δ</th>"
            "</tr></thead><tbody>"
        )
        for r in s.master_arithmetic_errors:
            parts.append(
                f"<tr><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{_money(r.off_plus_retro)}</td>"
                f"<td class='r'>{_money(r.total)}</td>"
                f"<td class='r'><strong>{_money(r.delta)}</strong></td></tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>6. Customers in monthly file but not on master</h2>")
    if not s.customers_not_on_master:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append("<p class='sub'>New customers receiving deliveries that don't have any master agreements yet. Onboard via Commercial Data master.</p>")
        parts.append("<table><thead><tr><th>Account</th><th>Customer</th></tr></thead><tbody>")
        for acct, name in s.customers_not_on_master:
            parts.append(f"<tr><td>{escape(acct)}</td><td>{escape(name)}</td></tr>")
        parts.append("</tbody></table>")

    return "\n".join(parts)

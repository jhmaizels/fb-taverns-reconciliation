"""
Monthly LWC Rate-Per-Keg (retro) reconciliation.

Reads a monthly Rate Per Keg .xlsx, compares the per-keg rate paid against the
agreed retro on the master cost file (column D), and produces a five-section
report:

  1. Under-payments — by product (the headline)
  2. Over-payments — by product
  3. Retros paid on products not on master (or master retro = 0)
  4. Multiple rates within the month (diagnostic)
  5. Agreed but not delivered (informational; collapsed by default)

Threshold for under/over: £0.005 per keg (retros are small to begin with;
applying the £0.05 weekly threshold here would mask real issues).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html import escape
from pathlib import Path

import pandas as pd

from reconcile import _to_str_code, _parse_date  # type: ignore

RETRO_THRESHOLD = 0.005  # £0.005 per keg = half a penny


# ---------- column detection ----------

def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).replace("\n", " ").strip() for c in df.columns]
    return df


def _find_col(df: pd.DataFrame, *candidates: str) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    raise ValueError(
        f"None of {candidates} found in columns: {list(df.columns)}"
    )


# ---------- parser ----------

@dataclass
class RetroLine:
    product_code: str
    product_desc: str
    qty: float
    rate_per_keg: float
    invoice_date: date | None = None
    site_id: str = ""


def parse_lwc_retro(path: str) -> list[RetroLine]:
    """Parses a monthly Rate Per Keg file. Robust to LWC's column-name churn."""
    with pd.ExcelFile(path) as xl:
        sheet = "FB_Taverns_Del_Date" if "FB_Taverns_Del_Date" in xl.sheet_names else xl.sheet_names[0]
        df = pd.read_excel(xl, sheet_name=sheet)
    df = _norm_cols(df)

    pc_col = _find_col(df, "PRODUCT CODE", "CODE.1")
    desc_col = _find_col(df, "PRODUCT DESC", "PRODUCT")
    qty_col = _find_col(df, "QTY")
    rate_col = _find_col(df, "RATE PER KEG")
    date_col = next((c for c in ("DATE",) if c in df.columns), None)
    site_col = next((c for c in ("SITE ID",) if c in df.columns), None)

    lines: list[RetroLine] = []
    for _, row in df.iterrows():
        pc = _to_str_code(row.get(pc_col))
        if not pc:
            continue
        try:
            qty = float(row.get(qty_col) or 0)
            rate = float(row.get(rate_col) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        lines.append(
            RetroLine(
                product_code=pc,
                product_desc=str(row.get(desc_col) or "").strip(),
                qty=qty,
                rate_per_keg=rate,
                invoice_date=_parse_date(row.get(date_col)) if date_col else None,
                site_id=_to_str_code(row.get(site_col)) if site_col else "",
            )
        )
    return lines


# ---------- agreed-retro lookup ----------

def load_agreed_retros() -> dict[str, dict]:
    """Loads {product_code: {description, agreed_retro}} from Airtable Products."""
    from airtable_io import load_agreed_retros as _load
    return _load()


# ---------- summary shapes ----------

@dataclass
class UnderOverRow:
    product_code: str
    product_desc: str
    agreed: float
    rates_paid: list[float]
    kegs: float
    total_delta: float  # negative for under, positive for over


@dataclass
class NoMasterRow:
    product_code: str
    product_desc: str
    rates_paid: list[float]
    kegs: float
    total_received: float


@dataclass
class MultiRateRow:
    product_code: str
    product_desc: str
    rates_with_kegs: list[tuple[float, float]]  # (rate, kegs)


@dataclass
class AgreedNotDeliveredRow:
    product_code: str
    product_desc: str
    agreed: float


@dataclass
class RetroSummary:
    file_name: str
    line_count: int
    under_payments: list[UnderOverRow]
    over_payments: list[UnderOverRow]
    paid_not_on_master: list[NoMasterRow]
    multi_rate: list[MultiRateRow]
    agreed_not_delivered: list[AgreedNotDeliveredRow]
    total_under: float = 0.0
    total_over: float = 0.0
    total_no_master: float = 0.0


# ---------- builder ----------

def build_retro_summary(
    file_name: str,
    lines: list[RetroLine],
    master: dict[str, dict],
    threshold: float = RETRO_THRESHOLD,
) -> RetroSummary:
    if not lines:
        raise ValueError("No retro lines parsed — refusing to produce empty summary")

    by_product: dict[str, list[RetroLine]] = {}
    for ln in lines:
        by_product.setdefault(ln.product_code, []).append(ln)

    under_payments: list[UnderOverRow] = []
    over_payments: list[UnderOverRow] = []
    paid_not_on_master: list[NoMasterRow] = []
    multi_rate: list[MultiRateRow] = []

    def _distinct_rates(group: list[RetroLine]) -> list[float]:
        return sorted({round(float(g.rate_per_keg), 4) for g in group})

    for pc, group in by_product.items():
        master_entry = master.get(pc) or {}
        agreed = float(master_entry.get("agreed_retro") or 0.0)
        desc = group[0].product_desc or master_entry.get("description", "") or ""

        distinct_rates_all = _distinct_rates(group)
        non_zero_distinct = [r for r in distinct_rates_all if r > 0]
        if len(non_zero_distinct) > 1:
            kegs_per_rate: dict[float, float] = {}
            for ln in group:
                if ln.rate_per_keg > 0:
                    rk = round(float(ln.rate_per_keg), 4)
                    kegs_per_rate[rk] = kegs_per_rate.get(rk, 0.0) + ln.qty
            multi_rate.append(
                MultiRateRow(
                    product_code=pc,
                    product_desc=desc,
                    rates_with_kegs=sorted(kegs_per_rate.items()),
                )
            )

        if agreed <= 0:
            paid_lines = [ln for ln in group if ln.rate_per_keg > 0]
            if paid_lines:
                kegs = sum(ln.qty for ln in paid_lines)
                total_received = sum(ln.qty * ln.rate_per_keg for ln in paid_lines)
                paid_not_on_master.append(
                    NoMasterRow(
                        product_code=pc,
                        product_desc=desc,
                        rates_paid=_distinct_rates(paid_lines),
                        kegs=kegs,
                        total_received=total_received,
                    )
                )
            continue

        under_lines = [ln for ln in group if (agreed - ln.rate_per_keg) > threshold]
        over_lines = [ln for ln in group if (ln.rate_per_keg - agreed) > threshold]

        if under_lines:
            under_kegs = sum(ln.qty for ln in under_lines)
            under_loss = sum(ln.qty * (agreed - ln.rate_per_keg) for ln in under_lines)
            under_payments.append(
                UnderOverRow(
                    product_code=pc,
                    product_desc=desc,
                    agreed=agreed,
                    rates_paid=_distinct_rates(under_lines),
                    kegs=under_kegs,
                    total_delta=-under_loss,
                )
            )

        if over_lines:
            over_kegs = sum(ln.qty for ln in over_lines)
            over_gain = sum(ln.qty * (ln.rate_per_keg - agreed) for ln in over_lines)
            over_payments.append(
                UnderOverRow(
                    product_code=pc,
                    product_desc=desc,
                    agreed=agreed,
                    rates_paid=_distinct_rates(over_lines),
                    kegs=over_kegs,
                    total_delta=+over_gain,
                )
            )

    delivered = set(by_product.keys())
    agreed_not_delivered: list[AgreedNotDeliveredRow] = []
    for pc, info in master.items():
        agreed = float(info.get("agreed_retro") or 0.0)
        if agreed > 0 and pc not in delivered:
            agreed_not_delivered.append(
                AgreedNotDeliveredRow(
                    product_code=pc,
                    product_desc=info.get("description", "") or "",
                    agreed=agreed,
                )
            )

    under_payments.sort(key=lambda r: r.total_delta)  # most negative first
    over_payments.sort(key=lambda r: -r.total_delta)
    paid_not_on_master.sort(key=lambda r: -r.total_received)
    multi_rate.sort(key=lambda r: r.product_code)
    agreed_not_delivered.sort(key=lambda r: r.product_code)

    return RetroSummary(
        file_name=file_name,
        line_count=len(lines),
        under_payments=under_payments,
        over_payments=over_payments,
        paid_not_on_master=paid_not_on_master,
        multi_rate=multi_rate,
        agreed_not_delivered=agreed_not_delivered,
        total_under=sum(r.total_delta for r in under_payments),
        total_over=sum(r.total_delta for r in over_payments),
        total_no_master=sum(r.total_received for r in paid_not_on_master),
    )


# ---------- HTML renderer ----------

def _money(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}£{abs(v):,.2f}"


def _money_neutral(v: float) -> str:
    return f"£{v:,.2f}"


def _rates_str(rates: list[float]) -> str:
    return ", ".join(f"£{r:.2f}" for r in rates)


def render_retro_summary_html(s: RetroSummary) -> str:
    parts: list[str] = []

    parts.append(
        f"""<div class="result">
  <div class="summary-row"><span>File</span><code>{escape(s.file_name)}</code></div>
  <div class="summary-row"><span>Lines</span><strong>{s.line_count}</strong></div>
  <div class="summary-row"><span>Total under-credit</span><strong>{_money(s.total_under)}</strong></div>
  <div class="summary-row"><span>Total over-credit</span><strong>{_money(s.total_over)}</strong></div>
  <div class="summary-row"><span>Total received on products not on master</span><strong>{_money_neutral(s.total_no_master)}</strong></div>
</div>"""
    )

    parts.append("<h2>1. Under-payments — by product</h2>")
    if not s.under_payments:
        parts.append("<p><em>None — every product was paid at the agreed rate or higher.</em></p>")
    else:
        parts.append(
            """<table>
  <thead><tr>
    <th>Code</th><th>Description</th>
    <th class="r">Agreed</th><th class="r">Paid</th>
    <th class="r">Kegs</th><th class="r">Δ total</th>
  </tr></thead><tbody>"""
        )
        for r in s.under_payments:
            parts.append(
                f"<tr class='neg'>"
                f"<td>{escape(r.product_code)}</td><td>{escape(r.product_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.agreed)}</td>"
                f"<td class='r'>{escape(_rates_str(r.rates_paid))}</td>"
                f"<td class='r'>{r.kegs:g}</td>"
                f"<td class='r'><strong>{_money(r.total_delta)}</strong></td>"
                f"</tr>"
            )
        parts.append(
            f"<tr><td colspan='5' class='r'><strong>Total under-credit</strong></td>"
            f"<td class='r'><strong>{_money(s.total_under)}</strong></td></tr>"
        )
        parts.append("</tbody></table>")

    parts.append("<h2>2. Over-payments — by product</h2>")
    if not s.over_payments:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append(
            """<table>
  <thead><tr>
    <th>Code</th><th>Description</th>
    <th class="r">Agreed</th><th class="r">Paid</th>
    <th class="r">Kegs</th><th class="r">Δ total</th>
  </tr></thead><tbody>"""
        )
        for r in s.over_payments:
            parts.append(
                f"<tr class='pos'>"
                f"<td>{escape(r.product_code)}</td><td>{escape(r.product_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.agreed)}</td>"
                f"<td class='r'>{escape(_rates_str(r.rates_paid))}</td>"
                f"<td class='r'>{r.kegs:g}</td>"
                f"<td class='r'><strong>{_money(r.total_delta)}</strong></td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>3. Retros paid on products not on master</h2>")
    if not s.paid_not_on_master:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append("<p class='sub'>Add a master row (with the right retro), or fill in the retro on an existing row that has it blank.</p>")
        parts.append(
            """<table>
  <thead><tr>
    <th>Code</th><th>Description</th>
    <th class="r">Rates paid</th><th class="r">Kegs</th><th class="r">Total received</th>
  </tr></thead><tbody>"""
        )
        for r in s.paid_not_on_master:
            parts.append(
                f"<tr>"
                f"<td>{escape(r.product_code)}</td><td>{escape(r.product_desc)}</td>"
                f"<td class='r'>{escape(_rates_str(r.rates_paid))}</td>"
                f"<td class='r'>{r.kegs:g}</td>"
                f"<td class='r'>{_money_neutral(r.total_received)}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("<h2>4. Multiple rates within the month</h2>")
    if not s.multi_rate:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append("<table><thead><tr><th>Code</th><th>Description</th><th class='r'>Rates &amp; kegs</th></tr></thead><tbody>")
        for r in s.multi_rate:
            rates = "; ".join(f"£{rate:.2f} × {kegs:g}" for rate, kegs in r.rates_with_kegs)
            parts.append(
                f"<tr><td>{escape(r.product_code)}</td><td>{escape(r.product_desc)}</td>"
                f"<td class='r'>{escape(rates)}</td></tr>"
            )
        parts.append("</tbody></table>")

    parts.append(
        f"""<details class="block">
  <summary><strong>5. Agreed but not delivered</strong>
  <span class="pill">{len(s.agreed_not_delivered)}</span></summary>"""
    )
    if not s.agreed_not_delivered:
        parts.append("<p><em>None — every agreed-retro product appeared on the file.</em></p>")
    else:
        parts.append("<p class='sub'>Informational — products with a master retro that had no deliveries this month. Watch next month.</p>")
        parts.append("<table><thead><tr><th>Code</th><th>Description</th><th class='r'>Agreed retro</th></tr></thead><tbody>")
        for r in s.agreed_not_delivered:
            parts.append(
                f"<tr><td>{escape(r.product_code)}</td>"
                f"<td>{escape(r.product_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.agreed)}</td></tr>"
            )
        parts.append("</tbody></table>")
    parts.append("</details>")

    return "\n".join(parts)

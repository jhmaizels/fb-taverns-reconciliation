"""
Tennents Direct (Scottish estate) reconciliation.

Inputs:
  Master  : FB_Taverns_Tennents_Master.xlsx — the PRIMARY price file
            (parsed by tennents_master.py: estate-wide SKU rates +
            site constructs + per-(site, SKU) exceptions).
  Monthly : FB Taverns Draught Pricing Report - <MONTH>.xlsx, sheet "Data"
            Per-delivery line items.

The workbook's own README §4 is the reconciliation spec:
  - expected total discount = SKU_Master "CURRENT CORRECT Total Discount"
    unless a Site_SKU_Exceptions row overrides it (the "Loaded" value is
    expected-current until the exception is resolved);
  - tolerance ±£0.50/brl;
  - retro due must equal retro £/brl × barrels EXACTLY;
  - managed sites: zero retro + full discount off-invoice is CORRECT;
  - Gartocher (flat £200/brl retro construct): validate total discount,
    not the split.

Monthly-file conventions:
  - Discounts and Retro Due are NEGATIVE in the report; the master holds
    positive rates. parse_monthly normalises to positive (sign detected
    file-wide, so a future positive-convention export also parses).
  - Compound identifiers: Customer "BELLS BAR (17591759)" → account
    17591759; SKU "T.LAGER 22G KEG (09000X)" → code 09000X. The report can
    quote either a SKU's code or its alt code — resolution is via the
    master's alt-code index. Join is (account, canonical sku).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from html import escape

import pandas as pd

from tennents_master import SkuException, TennentsMaster

# Per-Brl total-discount tolerance (README §4: ±£0.50/brl, rounding)
TENNENTS_DISCOUNT_TOLERANCE = 0.50
# §4: "retro due must equal retro £/brl × barrels exactly" — exact to the penny
RETRO_EXACT_TOLERANCE = 0.005
# Off + Retro + AOD must equal Total on every line (internal consistency)
LINE_ARITH_TOLERANCE = 0.005
# Implied WSP vs master WSP — monitoring only, not persisted as findings
WSP_VARIANCE_TOLERANCE = 1.00

# Volume commitment (Agreement_Terms): minimum draught barrels per agreement year
ANNUAL_BARREL_COMMITMENT = 2700
# T.Lager annual retro (£/brl), claimable at year end if commitment delivered
TLAGER_ANNUAL_RETRO_PER_BRL = 10.0
TLAGER_SKU_CODE = "090425"


def _is_tlager(master: TennentsMaster, sku_code: str) -> bool:
    """The £10/brl annual retro applies to Tennent's Lager only. Match by
    brand (apostrophe/spacing-insensitive), falling back to the known code."""
    sku = master.find_sku(sku_code)
    if sku is not None and re.sub(r"[^a-z]", "", sku.brand.lower()) == "tennentslager":
        return True
    return master.canonical_sku(sku_code) == TLAGER_SKU_CODE


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


# ---------- monthly parsing ----------

@dataclass
class DeliveryLine:
    account: str
    customer_name: str
    sku_code: str                 # as quoted in the report (code OR alt code)
    sku_desc: str
    kegs: float
    barrels: float
    invoice_price: float          # per case/keg (gross, tenant-facing)
    off_per_brl: float            # positive £/brl
    retro_per_brl: float          # positive £/brl
    aod_per_brl: float            # positive £/brl (additional off-invoice)
    total_per_brl: float          # positive £/brl
    retro_due: float | None       # positive £, None if the column is absent
    net_price: float | None       # per keg, None if absent
    month: str = ""


_MONTH_ABBR = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _parse_period(values) -> str | None:
    """Most common 'YYYY/Mon' value → 'YYYY-MM' (e.g. '2026/Jun' → '2026-06')."""
    counts: Counter[str] = Counter(str(v).strip() for v in values if not pd.isna(v))
    for raw, _n in counts.most_common():
        m = re.match(r"(\d{4})\s*/\s*([A-Za-z]{3})", raw)
        if m and m.group(2).upper() in _MONTH_ABBR:
            return f"{m.group(1)}-{_MONTH_ABBR[m.group(2).upper()]:02d}"
    return None


@dataclass
class MonthlyReport:
    lines: list[DeliveryLine]             # kegs > 0 — the checkable deliveries
    excluded_lines: list[DeliveryLine]    # kegs <= 0 (returns/credits) — volume only
    period: str | None                    # 'YYYY-MM'
    sign_normalized: bool                 # True when report discounts were negative


def parse_monthly(path: str) -> MonthlyReport:
    df = pd.read_excel(path, sheet_name="Data")
    df.columns = [str(c).strip() for c in df.columns]
    required = {
        "Customer Name",
        "SKU",
        "Kegs",
        "Barrels",
        "Invoice Price (per case/keg)",
        "Off invoice Discount per Brl",
        "Retro discount per Brl",
        "Total Discount per Brl",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Monthly file missing columns: {missing}")

    has_aod = "AOD per Brl" in df.columns
    has_retro_due = "Retro Due" in df.columns
    has_net = "Net Price per keg" in df.columns

    df = df.dropna(subset=["Customer Name", "SKU"])

    # The report expresses discounts as NEGATIVE amounts (credits); the master
    # holds positive rates. Detect the convention file-wide and normalise.
    total_sum = pd.to_numeric(df["Total Discount per Brl"], errors="coerce").fillna(0).sum()
    sign = -1.0 if total_sum < 0 else 1.0

    def _f(v) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if pd.isna(f) else f

    period = _parse_period(df["Month"]) if "Month" in df.columns else None

    lines: list[DeliveryLine] = []
    excluded: list[DeliveryLine] = []
    for _, row in df.iterrows():
        account = extract_account(row["Customer Name"])
        sku_code = extract_sku(row["SKU"])
        if not account or not sku_code:
            continue
        line = DeliveryLine(
            account=account,
            customer_name=strip_account_suffix(row["Customer Name"]),
            sku_code=sku_code,
            sku_desc=strip_sku_suffix(row["SKU"]),
            kegs=_f(row["Kegs"]),
            barrels=_f(row["Barrels"]),
            invoice_price=_f(row["Invoice Price (per case/keg)"]),
            off_per_brl=sign * _f(row["Off invoice Discount per Brl"]),
            retro_per_brl=sign * _f(row["Retro discount per Brl"]),
            aod_per_brl=sign * _f(row["AOD per Brl"]) if has_aod else 0.0,
            total_per_brl=sign * _f(row["Total Discount per Brl"]),
            retro_due=(sign * _f(row["Retro Due"])) if has_retro_due else None,
            net_price=_f(row["Net Price per keg"]) if has_net else None,
            month=str(row["Month"]).strip() if "Month" in df.columns and not pd.isna(row["Month"]) else "",
        )
        (lines if line.kegs > 0 else excluded).append(line)

    if not lines:
        raise ValueError("Monthly file produced zero delivery lines after parsing")
    return MonthlyReport(lines=lines, excluded_lines=excluded, period=period,
                         sign_normalized=(sign < 0))


# ---------- finding shapes ----------

@dataclass
class DiscountMismatch:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    basis: str                    # 'agreed rate' | 'exception expected-current'
    expected: float
    actual: float
    delta_per_brl: float          # expected − actual; positive = tenant/FB SHORT
    kegs: float
    barrels: float
    delta_total: float


@dataclass
class ExceptionPendingRow:
    """A known Site_SKU_Exceptions override still in effect — NOT re-flagged
    as a mismatch, but the £ short vs the correct rate is tracked here."""
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    loaded: float | None
    correct: float | None
    actual: float
    kegs: float
    barrels: float
    short_vs_correct: float | None   # (correct − actual) × barrels, None if no correct rate
    direction: str
    status: str


@dataclass
class ExceptionResolvedRow:
    """Delivery now matches the CORRECT rate, not the Loaded one — Tennents'
    fix has landed. Action: mark the exception resolved in the workbook,
    bump the version and re-upload the master."""
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    loaded: float | None
    correct: float
    actual: float
    barrels: float
    status: str


@dataclass
class RetroArithmeticRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    retro_per_brl: float
    barrels: float
    retro_due: float
    calc_due: float
    delta: float


@dataclass
class LineArithmeticRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    off_per_brl: float
    retro_per_brl: float
    aod_per_brl: float
    total_per_brl: float
    delta: float


@dataclass
class ManagedRetroRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    retro_per_brl: float
    barrels: float
    retro_value: float


@dataclass
class NoRateRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    kegs: float
    barrels: float
    actual_total_per_brl: float
    note: str


@dataclass
class NotOnMasterRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    kegs: float
    barrels: float
    avg_invoice: float
    avg_discount_per_brl: float


@dataclass
class MasterArithmeticRow:
    sku_code: str
    product: str
    base: float
    hold: float
    implied: float
    correct: float
    delta: float


@dataclass
class WspVarianceRow:
    sku_code: str
    sku_desc: str
    wsp_per_brl: float
    implied_wsp_per_brl: float
    delta_per_brl: float
    barrels: float
    sites: list[str]


@dataclass
class TennentsSummary:
    file_name: str
    period: str | None
    line_count: int
    master_version: str
    discount_mismatches: list[DiscountMismatch]
    exception_pending: list[ExceptionPendingRow]
    exceptions_resolved: list[ExceptionResolvedRow]
    retro_arithmetic: list[RetroArithmeticRow]
    line_arithmetic: list[LineArithmeticRow]
    managed_retro: list[ManagedRetroRow]
    no_rate: list[NoRateRow]
    not_on_master: list[NotOnMasterRow]
    new_customers: list[tuple[str, str]]         # (account, customer)
    sites_did_not_buy: list[tuple[str, str]]     # (account, site)
    master_arithmetic: list[MasterArithmeticRow]
    wsp_variance: list[WspVarianceRow]
    total_discount_delta: float = 0.0            # net £ across mismatches (+ = short)
    pending_short_gbp: float = 0.0               # £ short this month on KNOWN exceptions
    barrels_total: float = 0.0
    tlager_barrels: float = 0.0
    retro_due_total: float = 0.0


# ---------- reconciliation ----------

def reconcile(
    file_name: str,
    master: TennentsMaster,
    report: MonthlyReport,
    discount_tolerance: float = TENNENTS_DISCOUNT_TOLERANCE,
) -> TennentsSummary:
    """Line-level reconciliation per the master workbook's README §4."""

    disc_buckets: dict[tuple, DiscountMismatch] = {}
    pending_buckets: dict[tuple, ExceptionPendingRow] = {}
    resolved_buckets: dict[tuple, ExceptionResolvedRow] = {}
    managed_buckets: dict[tuple, ManagedRetroRow] = {}
    no_rate_buckets: dict[tuple, NoRateRow] = {}
    nom_buckets: dict[tuple, dict] = {}
    wsp_buckets: dict[str, dict] = {}
    retro_arith: list[RetroArithmeticRow] = []
    line_arith: list[LineArithmeticRow] = []
    new_customers: dict[str, str] = {}
    delivered_accounts: set[str] = set()

    barrels_total = 0.0
    tlager_barrels = 0.0
    retro_due_total = 0.0

    for line in report.lines + report.excluded_lines:
        barrels_total += line.barrels
        if _is_tlager(master, line.sku_code):
            tlager_barrels += line.barrels

    for line in report.lines:
        delivered_accounts.add(line.account)
        retro_due_total += line.retro_due if line.retro_due is not None \
            else line.retro_per_brl * line.barrels

        site = master.site_for_account(line.account)
        if site is None:
            new_customers.setdefault(line.account, line.customer_name)
            continue

        canonical = master.canonical_sku(line.sku_code)
        rb = master.resolve(line.account, line.sku_code)

        # 1. Line arithmetic: Off + Retro + AOD must equal Total.
        arith_delta = line.off_per_brl + line.retro_per_brl + line.aod_per_brl - line.total_per_brl
        if abs(arith_delta) > LINE_ARITH_TOLERANCE:
            line_arith.append(LineArithmeticRow(
                account=line.account, customer_name=line.customer_name,
                sku_code=line.sku_code, sku_desc=line.sku_desc,
                off_per_brl=line.off_per_brl, retro_per_brl=line.retro_per_brl,
                aod_per_brl=line.aod_per_brl, total_per_brl=line.total_per_brl,
                delta=arith_delta,
            ))

        # 2. Retro exactness (§4): retro due == retro £/brl × barrels, to the penny.
        if line.retro_due is not None:
            calc = line.retro_per_brl * line.barrels
            if abs(line.retro_due - calc) > RETRO_EXACT_TOLERANCE:
                retro_arith.append(RetroArithmeticRow(
                    account=line.account, customer_name=line.customer_name,
                    sku_code=line.sku_code, sku_desc=line.sku_desc,
                    retro_per_brl=line.retro_per_brl, barrels=line.barrels,
                    retro_due=line.retro_due, calc_due=calc,
                    delta=line.retro_due - calc,
                ))

        # 3. Managed sites: zero retro + full discount off-invoice is CORRECT.
        #    A retro split at a managed site is a cash-timing review item, not
        #    a value error (Site_Master ACTION note).
        if site.is_managed and abs(line.retro_per_brl) > LINE_ARITH_TOLERANCE:
            k = (line.account, canonical)
            b = managed_buckets.get(k)
            if b is None:
                managed_buckets[k] = ManagedRetroRow(
                    account=line.account, customer_name=line.customer_name,
                    sku_code=canonical, sku_desc=line.sku_desc,
                    retro_per_brl=line.retro_per_brl, barrels=line.barrels,
                    retro_value=line.retro_per_brl * line.barrels,
                )
            else:
                b.barrels += line.barrels
                b.retro_value += line.retro_per_brl * line.barrels

        # 4. Total-discount check (§4). Note Gartocher's bespoke construct needs
        #    no special-casing here: only the TOTAL is validated for every site —
        #    the OID/retro split is never checked against the master (the split
        #    is site-specific; the master only carries totals).
        if rb.basis == "exception":
            ex: SkuException = rb.exception  # type: ignore[assignment]
            loaded = ex.loaded_total_per_brl
            correct = ex.correct_total_per_brl
            if loaded is not None and abs(line.total_per_brl - loaded) <= discount_tolerance:
                # Known state persists — suppressed from mismatches, tracked here.
                # Keyed/displayed by the exception's own SKU listing so a
                # compound row ("400751/400557") aggregates to one line and the
                # table mirrors the workbook's Site_SKU_Exceptions rows 1:1.
                k = (line.account, ex.sku_code_raw)
                short = (correct - line.total_per_brl) * line.barrels if correct is not None else None
                b = pending_buckets.get(k)
                if b is None:
                    pending_buckets[k] = ExceptionPendingRow(
                        account=line.account, customer_name=line.customer_name,
                        sku_code=ex.sku_code_raw, sku_desc=line.sku_desc,
                        loaded=loaded, correct=correct, actual=line.total_per_brl,
                        kegs=line.kegs, barrels=line.barrels,
                        short_vs_correct=short,
                        direction=ex.direction, status=ex.status,
                    )
                else:
                    b.kegs += line.kegs
                    b.barrels += line.barrels
                    if short is not None:
                        b.short_vs_correct = (b.short_vs_correct or 0.0) + short
            elif correct is not None and abs(line.total_per_brl - correct) <= discount_tolerance:
                k = (line.account, ex.sku_code_raw)
                b = resolved_buckets.get(k)
                if b is None:
                    resolved_buckets[k] = ExceptionResolvedRow(
                        account=line.account, customer_name=line.customer_name,
                        sku_code=ex.sku_code_raw, sku_desc=line.sku_desc,
                        loaded=loaded, correct=correct, actual=line.total_per_brl,
                        barrels=line.barrels, status=ex.status,
                    )
                else:
                    b.barrels += line.barrels
            else:
                # Matches neither the known-loaded nor the correct rate — a
                # genuinely new discrepancy against expected-current.
                expected = loaded if loaded is not None else correct
                if expected is not None:
                    _add_discount_mismatch(
                        disc_buckets, line, canonical, expected,
                        basis="exception expected-current",
                    )
        elif rb.basis == "unknown_sku":
            k = (line.account, line.sku_code)
            b = nom_buckets.setdefault(k, {
                "customer_name": line.customer_name, "sku_desc": line.sku_desc,
                "kegs": 0.0, "barrels": 0.0, "inv_sum": 0.0, "disc_sum": 0.0, "n": 0,
            })
            b["kegs"] += line.kegs
            b["barrels"] += line.barrels
            b["inv_sum"] += line.invoice_price
            b["disc_sum"] += line.total_per_brl
            b["n"] += 1
        elif rb.basis == "no_rate":
            k = (line.account, canonical)
            b = no_rate_buckets.get(k)
            if b is None:
                no_rate_buckets[k] = NoRateRow(
                    account=line.account, customer_name=line.customer_name,
                    sku_code=canonical, sku_desc=line.sku_desc,
                    kegs=line.kegs, barrels=line.barrels,
                    actual_total_per_brl=line.total_per_brl,
                    note=(rb.sku.notes if rb.sku else ""),
                )
            else:
                b.kegs += line.kegs
                b.barrels += line.barrels
        else:  # sku_master
            if abs(line.total_per_brl - rb.expected) > discount_tolerance:  # type: ignore[operator]
                _add_discount_mismatch(disc_buckets, line, canonical, rb.expected, basis="agreed rate")  # type: ignore[arg-type]

        # 5. WSP cross-check (monitoring): invoice/keg ÷ brl-per-keg + off-invoice
        #    discounts should reproduce the master WSP £/brl.
        if rb.sku is not None and rb.sku.wsp_per_brl and line.kegs > 0 and line.barrels > 0:
            implied = line.invoice_price * line.kegs / line.barrels \
                + line.off_per_brl + line.aod_per_brl
            b = wsp_buckets.setdefault(canonical, {
                "sku_desc": line.sku_desc, "wsp": float(rb.sku.wsp_per_brl),
                "weighted": 0.0, "barrels": 0.0, "sites": set(),
            })
            b["weighted"] += implied * line.barrels
            b["barrels"] += line.barrels
            b["sites"].add(line.customer_name)

    not_on_master = [
        NotOnMasterRow(
            account=k[0], customer_name=b["customer_name"],
            sku_code=k[1], sku_desc=b["sku_desc"],
            kegs=b["kegs"], barrels=b["barrels"],
            avg_invoice=b["inv_sum"] / b["n"],
            avg_discount_per_brl=b["disc_sum"] / b["n"],
        )
        for k, b in nom_buckets.items()
    ]

    wsp_rows = []
    for code, b in wsp_buckets.items():
        if b["barrels"] <= 0:
            continue
        implied = b["weighted"] / b["barrels"]
        delta = implied - b["wsp"]
        if abs(delta) > WSP_VARIANCE_TOLERANCE:
            wsp_rows.append(WspVarianceRow(
                sku_code=code, sku_desc=b["sku_desc"], wsp_per_brl=b["wsp"],
                implied_wsp_per_brl=implied, delta_per_brl=delta,
                barrels=b["barrels"], sites=sorted(b["sites"]),
            ))

    sites_did_not_buy = sorted(
        (s.account, s.site_name)
        for s in master.sites
        if s.account not in delivered_accounts
    )

    master_arith = [
        MasterArithmeticRow(
            sku_code=s.sku_code, product=s.product,
            base=float(s.contract_base_per_brl or 0), hold=float(s.hold_per_brl or 0),
            implied=s.implied_total or 0.0, correct=float(s.correct_total_per_brl or 0),
            delta=float(s.correct_total_per_brl or 0) - (s.implied_total or 0.0),
        )
        for s in master.arithmetic_errors()
    ]

    discount_mismatches = sorted(disc_buckets.values(), key=lambda r: -abs(r.delta_total))
    exception_pending = sorted(pending_buckets.values(), key=lambda r: -(r.short_vs_correct or 0.0))
    exceptions_resolved = sorted(resolved_buckets.values(), key=lambda r: (r.customer_name, r.sku_code))
    retro_arith.sort(key=lambda r: -abs(r.delta))
    line_arith.sort(key=lambda r: -abs(r.delta))
    managed_retro = sorted(managed_buckets.values(), key=lambda r: -r.retro_value)
    no_rate = sorted(no_rate_buckets.values(), key=lambda r: -r.barrels)
    not_on_master.sort(key=lambda r: -r.barrels)
    wsp_rows.sort(key=lambda r: -abs(r.delta_per_brl * r.barrels))

    return TennentsSummary(
        file_name=file_name,
        period=report.period,
        line_count=len(report.lines),
        master_version=master.version,
        discount_mismatches=discount_mismatches,
        exception_pending=exception_pending,
        exceptions_resolved=exceptions_resolved,
        retro_arithmetic=retro_arith,
        line_arithmetic=line_arith,
        managed_retro=managed_retro,
        no_rate=no_rate,
        not_on_master=not_on_master,
        new_customers=sorted(new_customers.items()),
        sites_did_not_buy=sites_did_not_buy,
        master_arithmetic=master_arith,
        wsp_variance=wsp_rows,
        total_discount_delta=sum(r.delta_total for r in discount_mismatches),
        pending_short_gbp=sum(r.short_vs_correct or 0.0 for r in exception_pending),
        barrels_total=barrels_total,
        tlager_barrels=tlager_barrels,
        retro_due_total=retro_due_total,
    )


def _add_discount_mismatch(
    buckets: dict[tuple, DiscountMismatch],
    line: DeliveryLine,
    canonical: str,
    expected: float,
    basis: str,
) -> None:
    delta = expected - line.total_per_brl
    k = (line.account, canonical, round(expected, 2), round(line.total_per_brl, 2))
    b = buckets.get(k)
    if b is None:
        buckets[k] = DiscountMismatch(
            account=line.account, customer_name=line.customer_name,
            sku_code=canonical, sku_desc=line.sku_desc, basis=basis,
            expected=expected, actual=line.total_per_brl,
            delta_per_brl=delta, kegs=line.kegs, barrels=line.barrels,
            delta_total=delta * line.barrels,
        )
    else:
        b.kegs += line.kegs
        b.barrels += line.barrels
        b.delta_total += delta * line.barrels


# ---------- HTML rendering ----------

def _money(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}£{abs(v):,.2f}"


def _money_neutral(v: float) -> str:
    return f"£{v:,.2f}"


def render_summary_html(s: TennentsSummary) -> str:
    parts: list[str] = []

    monthly_pace = ANNUAL_BARREL_COMMITMENT / 12.0
    pace_cls = "ok" if s.barrels_total >= monthly_pace else "warn"
    period_txt = escape(s.period or "—")

    parts.append(
        f"""<div class="result">
  <div class="summary-row"><span>File</span><code>{escape(s.file_name)}</code></div>
  <div class="summary-row"><span>Period</span><strong>{period_txt}</strong></div>
  <div class="summary-row"><span>Master</span><code>{escape(s.master_version or "—")}</code></div>
  <div class="summary-row"><span>Lines processed</span><strong>{s.line_count}</strong></div>
  <div class="summary-row"><span>Discount mismatches (new)</span><strong>{len(s.discount_mismatches)}</strong></div>
  <div class="summary-row"><span>Net mismatch Δ (positive = short)</span><strong>{_money(s.total_discount_delta)}</strong></div>
  <div class="summary-row"><span>Known corrections still short this month</span><strong>{_money(s.pending_short_gbp)}</strong></div>
  <div class="summary-row"><span>Retro due per report</span><strong>{_money_neutral(s.retro_due_total)}</strong></div>
  <div class="summary-row"><span>Barrels this month</span><strong class="{pace_cls}">{s.barrels_total:,.2f}</strong> <span class="sub">(commitment pace {monthly_pace:,.0f}/mo for {ANNUAL_BARREL_COMMITMENT:,}/yr)</span></div>
  <div class="summary-row"><span>T.Lager barrels (£{TLAGER_ANNUAL_RETRO_PER_BRL:.0f}/brl annual retro accrues)</span><strong>{s.tlager_barrels:,.2f}</strong></div>
</div>"""
    )

    # 1. headline
    parts.append("<h2>1. Discount mismatches — vs expected-current rate</h2>")
    parts.append(
        "<p class='sub'>Total discount £/brl vs the master (SKU rate, or the exception's "
        "expected-current where one applies), tolerance ±£0.50/brl. "
        "Positive Δ = got LESS discount than agreed (short). These are NEW discrepancies "
        "— known pending corrections are in section 2, not here.</p>"
    )
    if not s.discount_mismatches:
        parts.append("<p><em>None — every line matched its expected-current rate.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th><th>Basis</th>"
            "<th class='r'>Expected £/brl</th><th class='r'>Actual £/brl</th>"
            "<th class='r'>Δ / brl</th><th class='r'>Brl</th><th class='r'>Δ total</th>"
            "</tr></thead><tbody>"
        )
        for r in s.discount_mismatches:
            cls = "pos" if r.delta_total > 0 else "neg"
            parts.append(
                f"<tr class='{cls}'>"
                f"<td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td><td>{escape(r.basis)}</td>"
                f"<td class='r'>{_money_neutral(r.expected)}</td>"
                f"<td class='r'>{_money_neutral(r.actual)}</td>"
                f"<td class='r'>{_money(r.delta_per_brl)}</td>"
                f"<td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'><strong>{_money(r.delta_total)}</strong></td>"
                f"</tr>"
            )
        parts.append(
            f"<tr><td colspan='8' class='r'><strong>Net total</strong></td>"
            f"<td class='r'><strong>{_money(s.total_discount_delta)}</strong></td></tr>"
        )
        parts.append("</tbody></table>")

    # 2. known exceptions in effect
    parts.append(f"<h2>2. Known exceptions still in effect <span class='pill'>{len(s.exception_pending)}</span></h2>")
    parts.append(
        "<p class='sub'>Lines matching a Site_SKU_Exceptions override (expected-current = the Loaded rate). "
        "Already raised with Tennents — not re-flagged as mismatches. "
        "“Short vs correct” totals what this month adds to the pending correction.</p>"
    )
    if not s.exception_pending:
        parts.append("<p><em>None this month.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Loaded £/brl</th><th class='r'>Correct £/brl</th>"
            "<th class='r'>Brl</th><th class='r'>Short vs correct</th><th>Status</th>"
            "</tr></thead><tbody>"
        )
        for r in s.exception_pending:
            short = _money(r.short_vs_correct) if r.short_vs_correct is not None else "—"
            correct = _money_neutral(r.correct) if r.correct is not None else "TBC"
            loaded = _money_neutral(r.loaded) if r.loaded is not None else "—"
            parts.append(
                f"<tr><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{loaded}</td><td class='r'>{correct}</td>"
                f"<td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'><strong>{short}</strong></td>"
                f"<td class='sub'>{escape(r.status)}</td></tr>"
            )
        parts.append(
            f"<tr><td colspan='6' class='r'><strong>Total short on known corrections</strong></td>"
            f"<td class='r'><strong>{_money(s.pending_short_gbp)}</strong></td><td></td></tr>"
        )
        parts.append("</tbody></table>")

    # 3. exceptions that look resolved
    parts.append(f"<h2>3. Exceptions that look RESOLVED <span class='pill'>{len(s.exceptions_resolved)}</span></h2>")
    if not s.exceptions_resolved:
        parts.append("<p><em>None — no exception line matched its correct rate yet.</em></p>")
    else:
        parts.append(
            "<p class='sub'><strong>Action:</strong> Tennents' fix appears to have landed for these — "
            "mark the exception resolved in the master workbook, bump the version and re-upload.</p>"
        )
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Was loaded</th><th class='r'>Correct</th><th class='r'>Now charged</th><th class='r'>Brl</th>"
            "</tr></thead><tbody>"
        )
        for r in s.exceptions_resolved:
            loaded = _money_neutral(r.loaded) if r.loaded is not None else "—"
            parts.append(
                f"<tr class='neg'><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{loaded}</td>"
                f"<td class='r'>{_money_neutral(r.correct)}</td>"
                f"<td class='r'>{_money_neutral(r.actual)}</td>"
                f"<td class='r'>{r.barrels:.2f}</td></tr>"
            )
        parts.append("</tbody></table>")

    # 4. retro arithmetic
    parts.append(f"<h2>4. Retro arithmetic errors <span class='pill'>{len(s.retro_arithmetic)}</span></h2>")
    parts.append("<p class='sub'>Retro due must equal retro £/brl × barrels EXACTLY (README §4).</p>")
    if not s.retro_arithmetic:
        parts.append("<p><em>None — every retro due is exact.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Retro £/brl</th><th class='r'>Brl</th>"
            "<th class='r'>Due (report)</th><th class='r'>Due (calc)</th><th class='r'>Δ</th>"
            "</tr></thead><tbody>"
        )
        for r in s.retro_arithmetic:
            parts.append(
                f"<tr class='pos'><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.retro_per_brl)}</td>"
                f"<td class='r'>{r.barrels:.4f}</td>"
                f"<td class='r'>{_money_neutral(r.retro_due)}</td>"
                f"<td class='r'>{_money_neutral(r.calc_due)}</td>"
                f"<td class='r'><strong>{_money(r.delta)}</strong></td></tr>"
            )
        parts.append("</tbody></table>")

    # 5. line arithmetic
    parts.append(f"<h2>5. Line arithmetic errors <span class='pill'>{len(s.line_arithmetic)}</span></h2>")
    parts.append("<p class='sub'>Off-invoice + retro + AOD should equal the line's total discount.</p>")
    if not s.line_arithmetic:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Off</th><th class='r'>Retro</th><th class='r'>AOD</th>"
            "<th class='r'>Total</th><th class='r'>Δ</th>"
            "</tr></thead><tbody>"
        )
        for r in s.line_arithmetic:
            parts.append(
                f"<tr class='pos'><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.off_per_brl)}</td>"
                f"<td class='r'>{_money_neutral(r.retro_per_brl)}</td>"
                f"<td class='r'>{_money_neutral(r.aod_per_brl)}</td>"
                f"<td class='r'>{_money_neutral(r.total_per_brl)}</td>"
                f"<td class='r'><strong>{_money(r.delta)}</strong></td></tr>"
            )
        parts.append("</tbody></table>")

    # 6. managed-site retro splits
    parts.append(f"<h2>6. Managed sites on a retro split <span class='pill'>{len(s.managed_retro)}</span></h2>")
    parts.append(
        "<p class='sub'>Managed sites should take the FULL discount off-invoice — zero retro is CORRECT there. "
        "A retro split is a cash-timing review item, not a value error.</p>"
    )
    if not s.managed_retro:
        parts.append("<p><em>None — managed sites are all off-invoice.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Retro £/brl</th><th class='r'>Brl</th><th class='r'>Retro £</th>"
            "</tr></thead><tbody>"
        )
        for r in s.managed_retro:
            parts.append(
                f"<tr><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.retro_per_brl)}</td>"
                f"<td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'>{_money_neutral(r.retro_value)}</td></tr>"
            )
        parts.append("</tbody></table>")

    # 7. no agreed rate
    parts.append(f"<h2>7. Deliveries with no agreed rate <span class='pill'>{len(s.no_rate)}</span></h2>")
    if not s.no_rate:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append(
            "<p class='sub'>The SKU is on the master but has no CURRENT CORRECT rate (RATE TBC — "
            "chase Tennents for a written rate, then update the workbook).</p>"
        )
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Kegs</th><th class='r'>Brl</th><th class='r'>Charged disc £/brl</th><th>Master note</th>"
            "</tr></thead><tbody>"
        )
        for r in s.no_rate:
            parts.append(
                f"<tr><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{r.kegs:g}</td><td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'>{_money_neutral(r.actual_total_per_brl)}</td>"
                f"<td class='sub'>{escape(r.note)}</td></tr>"
            )
        parts.append("</tbody></table>")

    # 8. not on master
    parts.append(f"<h2>8. (Site, SKU) not on master <span class='pill'>{len(s.not_on_master)}</span></h2>")
    if not s.not_on_master:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append(
            "<p class='sub'>Deliveries of an SKU the SKU_Master sheet doesn't cover at all — "
            "add a row to the workbook (with source) and re-upload.</p>"
        )
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th><th>Description</th>"
            "<th class='r'>Kegs</th><th class='r'>Brl</th><th class='r'>Avg invoice</th><th class='r'>Avg disc £/brl</th>"
            "</tr></thead><tbody>"
        )
        for r in s.not_on_master:
            parts.append(
                f"<tr><td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)}</td><td>{escape(r.sku_desc)}</td>"
                f"<td class='r'>{r.kegs:g}</td><td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'>{_money_neutral(r.avg_invoice)}</td>"
                f"<td class='r'>{_money_neutral(r.avg_discount_per_brl)}</td></tr>"
            )
        parts.append("</tbody></table>")

    # 9. new customers
    parts.append(f"<h2>9. Customers not in Site_Master <span class='pill'>{len(s.new_customers)}</span></h2>")
    if not s.new_customers:
        parts.append("<p><em>None.</em></p>")
    else:
        parts.append("<p class='sub'>Accounts receiving deliveries that the master's Site_Master sheet doesn't know. Add the site (with operating model + discount construct) and re-upload.</p>")
        parts.append("<table><thead><tr><th>Account</th><th>Customer</th></tr></thead><tbody>")
        for acct, name in s.new_customers:
            parts.append(f"<tr><td>{escape(acct)}</td><td>{escape(name)}</td></tr>")
        parts.append("</tbody></table>")

    # 10. didn't buy
    parts.append(f"<h2>10. Sites that didn't buy this month <span class='pill'>{len(s.sites_did_not_buy)}</span></h2>")
    if not s.sites_did_not_buy:
        parts.append("<p><em>None — every site had at least one delivery.</em></p>")
    else:
        parts.append("<table><thead><tr><th>Account</th><th>Site</th></tr></thead><tbody>")
        for acct, name in s.sites_did_not_buy:
            parts.append(f"<tr><td>{escape(acct)}</td><td>{escape(name)}</td></tr>")
        parts.append("</tbody></table>")

    # 11. master data quality
    parts.append(f"<h2>11. Master data quality: base + hold ≠ CURRENT CORRECT <span class='pill'>{len(s.master_arithmetic)}</span></h2>")
    if not s.master_arithmetic:
        parts.append("<p><em>None — every SKU row's total equals base + hold.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>SKU</th><th>Product</th>"
            "<th class='r'>Base</th><th class='r'>Hold</th><th class='r'>Base+Hold</th>"
            "<th class='r'>CURRENT CORRECT</th><th class='r'>Δ</th>"
            "</tr></thead><tbody>"
        )
        for r in s.master_arithmetic:
            parts.append(
                f"<tr><td>{escape(r.sku_code)}</td><td>{escape(r.product)}</td>"
                f"<td class='r'>{_money_neutral(r.base)}</td>"
                f"<td class='r'>{_money_neutral(r.hold)}</td>"
                f"<td class='r'>{_money_neutral(r.implied)}</td>"
                f"<td class='r'>{_money_neutral(r.correct)}</td>"
                f"<td class='r'><strong>{_money(r.delta)}</strong></td></tr>"
            )
        parts.append("</tbody></table>")

    # 12. WSP monitoring
    parts.append(f"<h2>12. WSP variance (monitoring) <span class='pill'>{len(s.wsp_variance)}</span></h2>")
    parts.append(
        "<p class='sub'>Implied gross £/brl (invoice ÷ brl-per-keg + off-invoice) vs the master WSP, "
        "beyond ±£1/brl. Informational — checks the WSP loaded post-PINC, not the discount. "
        "Not written to Airtable. NB the 70p/keg small-container charge on 11G T.Lager can show here "
        "if Tennents rolls it into the invoice price.</p>"
    )
    if not s.wsp_variance:
        parts.append("<p><em>None — invoice prices are consistent with master WSPs.</em></p>")
    else:
        parts.append(
            "<table><thead><tr>"
            "<th>SKU</th><th>Description</th>"
            "<th class='r'>Master WSP £/brl</th><th class='r'>Implied £/brl</th>"
            "<th class='r'>Δ / brl</th><th class='r'>Brl</th><th class='r'>Sites</th>"
            "</tr></thead><tbody>"
        )
        for r in s.wsp_variance:
            sites_attr = "; ".join(r.sites)
            parts.append(
                f"<tr><td>{escape(r.sku_code)}</td><td>{escape(r.sku_desc)}</td>"
                f"<td class='r'>{_money_neutral(r.wsp_per_brl)}</td>"
                f"<td class='r'>{_money_neutral(r.implied_wsp_per_brl)}</td>"
                f"<td class='r'>{_money(r.delta_per_brl)}</td>"
                f"<td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r' title='{escape(sites_attr)}'>{len(r.sites)}</td></tr>"
            )
        parts.append("</tbody></table>")

    return "\n".join(parts)

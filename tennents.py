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

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from html import escape

import pandas as pd

from tennents_master import SkuException, TennentsMaster, suggest_sku

# Per-Brl total-discount tolerance (README §4: ±£0.50/brl, rounding)
TENNENTS_DISCOUNT_TOLERANCE = 0.50
# §4: "retro due must equal retro £/brl × barrels exactly" — exact to the penny
RETRO_EXACT_TOLERANCE = 0.005
# Off + Retro + AOD must equal Total on every line (internal consistency)
LINE_ARITH_TOLERANCE = 0.005
# Implied WSP vs master WSP — monitoring only, not persisted as findings
WSP_VARIANCE_TOLERANCE = 1.00
# Draft-email materiality: (SKU, rate) groups whose total shortfall is below this are
# rolled into one 'minor variances' line so the note to Tennents stays focused.
EMAIL_MINOR_GBP = 5.0

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
    actual_total_per_brl: float           # the FIRST line's total (see total_min/max)
    note: str
    # Range of totals charged across this bucket's lines — the findings page only
    # offers "Set rate = charged" when they agree (a single unambiguous figure).
    total_min: float = 0.0
    total_max: float = 0.0


@dataclass
class NotOnMasterRow:
    account: str
    customer_name: str
    sku_code: str
    sku_desc: str
    kegs: float
    barrels: float
    avg_invoice: float
    avg_discount_per_brl: float           # unweighted mean (see disc_min/max)
    disc_min: float = 0.0
    disc_max: float = 0.0


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
                "disc_min": line.total_per_brl, "disc_max": line.total_per_brl,
            })
            b["kegs"] += line.kegs
            b["barrels"] += line.barrels
            b["inv_sum"] += line.invoice_price
            b["disc_sum"] += line.total_per_brl
            b["disc_min"] = min(b["disc_min"], line.total_per_brl)
            b["disc_max"] = max(b["disc_max"], line.total_per_brl)
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
                    total_min=line.total_per_brl, total_max=line.total_per_brl,
                )
            else:
                b.kegs += line.kegs
                b.barrels += line.barrels
                b.total_min = min(b.total_min, line.total_per_brl)
                b.total_max = max(b.total_max, line.total_per_brl)
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
            disc_min=b["disc_min"], disc_max=b["disc_max"],
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

_TENNENTS_STYLE = """<style>
  .accept-btn { background:#33691e; color:#fff; border:0; padding:0.3em 0.7em; border-radius:4px; font-size:0.82em; cursor:pointer; white-space:nowrap; }
  .accept-btn:hover { background:#274f16; }
  .accept-btn:disabled { opacity:0.6; cursor:default; }
  .action-cell { white-space:nowrap; }
  .link-accept { display:inline-flex; gap:0.35em; align-items:center; margin-right:0.5em; }
  .link-sel { max-width:16em; font-size:0.85em; }
  .hint { color:#8a6500; font-size:0.85em; }
  tr.accepted td { background:#eef7ea; color:#567; }
  .accepted-tag { color:#1f7a1f; font-weight:700; font-size:0.85em; }
  .email-draft { max-width: 900px; }
  .email-draft label { display:block; font-weight:600; margin-top:0.6em; }
  .email-draft input[type=text] { width:100%; }
  .email-draft textarea { width:100%; min-height:20em; font-family: inherit; font-size:0.95em; }
  .email-actions { margin-top:0.6em; display:flex; gap:0.8em; align-items:center; flex-wrap:wrap; }
  .email-actions label.email-opt { display:inline-flex; gap:0.35em; align-items:center; font-weight:400; margin-top:0; }
</style>"""


_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]


def _period_label(period: str | None) -> str:
    """'2026-08' -> 'August 2026' for the operator-facing email; '' if unset."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", str(period or "").strip())
    if not m:
        return str(period or "")
    i = int(m.group(2)) - 1
    return f"{_MONTH_NAMES[i] if 0 <= i < 12 else m.group(2)} {m.group(1)}"


def _container_of(desc: str) -> str:
    """'Blackthorn Dry 5% 50L Keg' -> '50L'; '' when no size token."""
    m = re.search(r"\b(\d{1,3}\s*(?:L|G))\b", desc or "", re.I)
    return m.group(1).replace(" ", "").upper() if m else ""


def _tennents_findings_script(cfg: dict) -> str:
    # Same discipline as the LWC findings page: never emit NaN/Infinity (kills
    # JSON.parse), and escape < > & as \\u00xx so no SKU description / site name
    # can break out of the <script> data context.
    try:
        cfg_json = json.dumps(cfg, allow_nan=False)
    except ValueError:
        cfg_json = json.dumps({
            "acceptUrl": cfg.get("acceptUrl", ""), "sourceFile": cfg.get("sourceFile", ""),
            "canAccept": bool(cfg.get("canAccept")),
            "email": {"file": cfg.get("sourceFile", ""), "period": ""},
        })
    cfg_json = cfg_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        f'<script id="tennents-findings-config" type="application/json">{cfg_json}</script>'
        + _TENNENTS_JS
    )


# Client-side logic for the Tennents findings page: the Add-to-master buttons
# (POST to acceptUrl, confirm dialog, mark every row for that SKU done) and the
# live-drafted email to Tennents. Config comes from the JSON blob above — no
# server values are interpolated into JS. Plain string (NOT an f-string).
_TENNENTS_JS = """<script>
(function () {
  var el = document.getElementById('tennents-findings-config');
  if (!el) return;
  var CFG;
  try { CFG = JSON.parse(el.textContent); } catch (e) { return; }
  var accepted = new Set();   // SKU codes accepted / linked into the master -> drop from the draft
  var bodyDirty = false;
  var subject = document.getElementById('t-email-subject');
  var body = document.getElementById('t-email-body');
  var mailto = document.getElementById('t-email-mailto');
  var overBox = document.getElementById('t-email-include-over');

  function money(v) { return '\\u00a3' + (Number(v) || 0).toFixed(2); }
  function brl(b) { return (Number(b) || 0).toFixed(2) + ' brl'; }
  function key(s) { return String(s || '').trim().toUpperCase(); }
  function fmtPeriod(p) {
    var m = /^(\\d{4})-(\\d{2})$/.exec(String(p || ''));
    if (!m) return String(p || '');
    var names = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    var i = parseInt(m[2], 10) - 1;
    return (names[i] || m[2]) + ' ' + m[1];
  }

  function fmtSites(items) {
    var seen = {}, out = [];
    items.forEach(function (m) {
      var k = String(m.site) + '|' + String(m.account);
      if (seen[k]) return;
      seen[k] = 1;
      out.push(m.site + ' (' + m.account + ')');
    });
    return out.join(', ');
  }
  function sum(items, f) { var t = 0; items.forEach(function (m) { t += Number(m[f]) || 0; }); return t; }
  // Tennents load a rate per SKU across accounts, so the draft groups per-site
  // findings by SKU + applied rate: one line per (SKU, rate) naming the sites.
  function groupBy(items, keyFn) {
    var map = {}, order = [];
    items.forEach(function (m) {
      var k = keyFn(m);
      if (!map[k]) { map[k] = []; order.push(k); }
      map[k].push(m);
    });
    return order.map(function (k) { return map[k]; });
  }
  function label(m) { return m.desc + ' (' + m.sku + ')'; }

  function buildBody() {
    var e = CFG.email || {}, L = [], sec = 0;
    var minor = Number(CFG.minorGbp) || 0;
    var includeOver = !!(overBox && overBox.checked);
    function n() { return ++sec; }
    L.push('Hi David,');
    L.push('');
    L.push('Reviewing the ' + fmtPeriod(e.period) + ' draught pricing report (' + (e.file || '') + '), the following need your attention:');
    L.push('');

    // sign +1: FB got LESS discount than agreed (short); -1: more (over)
    function rateSection(title, items, sign) {
      // One group per SKU + AGREED rate + applied rate (bucketed to the nearest 50p so penny-level
      // rounding across sites can't split a group). The agreed rate is part of the key because a
      // site with an open exception reconciles against the exception's Loaded value, not the
      // master rate - two sites can share a SKU and an applied rate yet have different agreed rates.
      var groups = groupBy(items, function (m) { return m.sku + '|' + Number(m.expected).toFixed(2) + '|' + (Math.round(Number(m.actual) * 2) / 2).toFixed(2); });
      groups.sort(function (a, b) { return Math.abs(sum(b, 'delta_total')) - Math.abs(sum(a, 'delta_total')); });
      var major = [], minors = [], minorP = Math.round(minor * 100);
      groups.forEach(function (g) {   // compare in pennies: a float fold of 2dp values can land at 4.999...
        (Math.round(Math.abs(sum(g, 'delta_total')) * 100) < minorP ? minors : major).push(g);
      });
      if (!major.length && !minors.length) return;
      var word = sign > 0 ? 'short' : 'over';
      L.push(n() + ') ' + title);
      // per-brl from the reconciliation's own exact figures (a range only when it varies within the
      // 50p bucket), never re-derived from rounded totals
      function perBrlText(g) {
        var dlo = Infinity, dhi = -Infinity;
        g.forEach(function (x) { var d = Math.abs(Number(x.delta_brl)); if (d < dlo) dlo = d; if (d > dhi) dhi = d; });
        if (!isFinite(dlo)) { dlo = dhi = Math.abs(Number(g[0].expected) - Number(g[0].actual)); }
        return (dhi - dlo) > 0.005 ? (money(dlo) + ' to ' + money(dhi)) : money(dlo);
      }
      major.forEach(function (g) {
        var m = g[0], lo = Infinity, hi = -Infinity;
        var brls = sum(g, 'barrels'), tot = Math.abs(sum(g, 'delta_total'));
        g.forEach(function (x) { var a = Number(x.actual); if (a < lo) lo = a; if (a > hi) hi = a; });
        var applied = (hi - lo) > 0.005 ? (money(lo) + ' to ' + money(hi)) : money(m.actual);
        L.push('   - ' + label(m) + ': applied ' + applied + '/brl vs agreed ' + money(m.expected) + '/brl (' +
               perBrlText(g) + '/brl ' + word + ') at ' + fmtSites(g) + '; ' + brl(brls) + ', ' + money(tot) + ' ' + word);
      });
      if (minors.length) {
        var mt = 0;
        minors.forEach(function (g) { mt += Math.abs(sum(g, 'delta_total')); });
        L.push('   - Plus ' + minors.length + ' minor variance' + (minors.length === 1 ? '' : 's') + ' under ' + money(minor) +
               ' each, ' + money(mt) + ' in total (details on the reconciliation page): ' +
               minors.map(function (g) { return label(g[0]) + ' ' + perBrlText(g) + '/brl ' + word + ' at ' + fmtSites(g); }).join('; '));
      }
      L.push('   Total ' + word + ' across the above: ' + money(Math.abs(sum(items, 'delta_total'))) + '.');
      L.push('');
    }
    rateSection('Discounts applied BELOW the agreed rate - please correct these and credit the shortfall:', e.short || [], 1);
    if (includeOver) {
      rateSection('Discounts applied ABOVE the agreed rate - please confirm these are intended so we can align our records:', e.over || [], -1);
    }

    var pend = e.pending || [];
    if (pend.length) {
      L.push(n() + ') Known corrections still not loaded - this month adds ' + money(sum(pend, 'short')) + ' to the amounts already raised:');
      var pg = groupBy(pend, function (m) { return m.sku + '|' + Number(m.loaded).toFixed(2) + '|' + Number(m.correct).toFixed(2); });
      pg.sort(function (a, b) { return sum(b, 'short') - sum(a, 'short'); });
      pg.forEach(function (g) {
        var m = g[0];
        L.push('   - ' + label(m) + ': still at ' + money(m.loaded) + '/brl vs agreed ' + money(m.correct) + '/brl at ' + fmtSites(g) +
               '; ' + brl(sum(g, 'barrels')) + ', ' + money(sum(g, 'short')) + ' short this month');
      });
      L.push('');
    }
    var res = e.resolved || [];
    if (res.length) {
      L.push(n() + ') Corrections we can see have now landed - thank you, we will close these our end:');
      res.forEach(function (m) {
        L.push('   - ' + label(m) + ' at ' + m.site + ': now ' + money(m.actual) + '/brl (agreed ' + money(m.correct) + '/brl)');
      });
      L.push('');
    }
    var rates = [];
    (e.no_rate || []).forEach(function (x) { rates.push({ site: x.site, account: x.account, sku: x.sku, desc: x.desc, charged: x.charged, lo: x.lo, hi: x.hi, barrels: x.barrels, why: 'no agreed rate on our schedule' }); });
    (e.not_on_master || []).forEach(function (x) { rates.push({ site: x.site, account: x.account, sku: x.sku, desc: x.desc, charged: x.charged, lo: x.lo, hi: x.hi, barrels: x.barrels, why: 'SKU code not on our schedule' }); });
    rates = rates.filter(function (x) { return !accepted.has(key(x.sku)); });
    if (rates.length) {
      L.push(n() + ') Rates to confirm - please confirm the agreed total discount per brl in writing for the following:');
      rates.forEach(function (x) {
        var applied = (x.lo != null && x.hi != null && (Number(x.hi) - Number(x.lo)) > 0.5)
          ? (money(x.lo) + ' to ' + money(x.hi)) : money(x.charged);
        L.push('   - ' + label(x) + ' at ' + x.site + ' (' + x.account + '): ' + x.why + '; ' + applied + '/brl applied on ' + brl(x.barrels));
      });
      L.push('');
    }
    var ar = (e.retro_arith || []).concat(e.line_arith || []);
    if (ar.length) {
      L.push(n() + ') Arithmetic errors on the report - please correct:');
      ar.forEach(function (m) { L.push('   - ' + label(m) + ' at ' + m.site + ': ' + m.what); });
      L.push('');
    }
    L.push('Thanks,');
    if (CFG.signoff) L.push(CFG.signoff);
    return L.join('\\n');
  }

  function updateMailto() {
    if (!mailto) return;
    var s = subject ? subject.value : '';
    var b = body ? body.value : '';
    var href = 'mailto:?subject=' + encodeURIComponent(s) + '&body=' + encodeURIComponent(b);
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

  function acceptSku(btn) {
    var d = btn.dataset, mode = d.mode, sku = d.sku;
    var params = new URLSearchParams();
    params.set('mode', mode);
    params.set('sku_code', sku);
    params.set('sku_desc', d.desc || '');
    params.set('source_file', CFG.sourceFile || '');
    var msg;
    if (mode === 'link') {
      var wrap = btn.closest('.link-accept');
      var sel = wrap ? wrap.querySelector('.link-sel') : null;
      if (!sel || !sel.value) { window.alert('Choose the existing SKU to link to.'); return; }
      var label = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : sel.value;
      params.set('link_to', sel.value);
      msg = 'Link report code ' + sku + ' (' + (d.desc || '') + ') as an alternative code of:\\n\\n   ' + label +
            '\\n\\nFuture deliveries under ' + sku + ' will reconcile against the agreed rate of that SKU.';
    } else if (mode === 'new') {
      params.set('charged_total', d.charged || '');
      params.set('container', d.container || '');
      msg = 'Add ' + sku + ' ' + (d.desc || '') + ' to the Tennents master as a NEW SKU?\\n\\n' +
            'Agreed total discount = the ' + money(d.charged) + '/brl Tennents charged (estate-wide).\\n' +
            'WSP is left blank - fill it in the workbook once Tennents confirm.';
    } else {
      params.set('charged_total', d.charged || '');
      msg = 'Set the agreed rate for ' + sku + ' ' + (d.desc || '') + ' to the charged ' + money(d.charged) + '/brl (estate-wide)?';
    }
    if (!window.confirm(msg)) return;
    var orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Saving\\u2026';
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
        var tag = (mode === 'link') ? ('\\u2713 linked to ' + (res.j.sku_code || ''))
                : (mode === 'new') ? '\\u2713 added' : '\\u2713 rate set';
        // One SKU can sit at several sites (the same new code delivered to two
        // pubs); an accept is estate-wide, so mark every row for that code.
        Array.prototype.forEach.call(document.querySelectorAll('tr[data-sku]'), function (tr) {
          if (key(tr.dataset.sku) !== key(sku)) return;
          tr.classList.add('accepted');
          var td = tr.querySelector('.action-cell');
          if (td) {
            td.textContent = '';
            var s = document.createElement('span');
            s.className = 'accepted-tag';
            s.textContent = tag;
            td.appendChild(s);
          }
        });
        accepted.add(key(sku));
        if (bodyDirty) {
          var note = document.getElementById('t-email-dirty-note');
          if (note) note.style.display = 'inline';
        }
        rebuild();
      } else {
        btn.disabled = false;
        btn.textContent = orig;
        window.alert('Could not update the master: ' + ((res.j && res.j.error) || 'unknown error'));
      }
    }).catch(function (err) {
      btn.disabled = false;
      btn.textContent = orig;
      window.alert('Network error: ' + err);
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('.t-accept'), function (b) {
    b.addEventListener('click', function () { acceptSku(b); });
  });
  if (body) body.addEventListener('input', function () { bodyDirty = true; updateMailto(); });
  if (subject) subject.addEventListener('input', updateMailto);
  if (overBox) overBox.addEventListener('change', function () {
    if (bodyDirty) {
      var note = document.getElementById('t-email-dirty-note');
      if (note) note.style.display = 'inline';
    } else {
      rebuild();
    }
  });
  var copyBtn = document.getElementById('t-email-copy');
  if (copyBtn) copyBtn.addEventListener('click', function () {
    var text = (subject ? 'Subject: ' + subject.value + '\\n\\n' : '') + (body ? body.value : '');
    var done = document.getElementById('t-email-copied');
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


def _money(v: float) -> str:
    sign = "+" if v >= 0 else "−"
    return f"{sign}£{abs(v):,.2f}"


def _money_neutral(v: float) -> str:
    return f"£{v:,.2f}"


def render_summary_html(
    s: TennentsSummary,
    accept_url: str = "",
    can_accept: bool = False,
    source_file: str = "",
    master: TennentsMaster | None = None,
    actor_name: str = "",
) -> str:
    """Findings page. With can_accept (admin) the 'no agreed rate' / 'not on
    master' rows get Add-to-master actions (POST accept_url — link a new code to
    an existing SKU, add as new at the charged rate, or set a TBC rate); a draft
    email to Tennents is built client-side from the findings, and accepted SKUs
    drop out of it live. Mirrors the LWC findings page."""
    parts: list[str] = [_TENNENTS_STYLE]
    can_accept = bool(can_accept and accept_url)
    sku_choices = sorted(
        ((x.sku_code, f"{x.sku_code} {x.product or x.brand}") for x in (master.skus if master else [])),
        key=lambda t: t[1].upper(),
    )

    # An accept is ESTATE-WIDE, so the charged figure it would write must be a
    # single unambiguous rate: range the totals across every bucket sharing the
    # code (a code delivered to two pubs at two discounts is "mixed" — offer
    # Link, not a rate). Rows built without a range fall back to their figure.
    def _range(rows, lo_attr, hi_attr, fallback_attr) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for r in rows:
            lo, hi = getattr(r, lo_attr), getattr(r, hi_attr)
            if lo == 0.0 and hi == 0.0:
                lo = hi = getattr(r, fallback_attr)
            k = str(r.sku_code).strip().upper()
            cur = out.get(k)
            out[k] = (lo, hi) if cur is None else (min(cur[0], lo), max(cur[1], hi))
        return out

    norate_range = _range(s.no_rate, "total_min", "total_max", "actual_total_per_brl")
    nom_range = _range(s.not_on_master, "disc_min", "disc_max", "avg_discount_per_brl")

    def _mixed(rng: dict, code: str) -> tuple[bool, float, float]:
        lo, hi = rng.get(str(code).strip().upper(), (0.0, 0.0))
        return (hi - lo) > TENNENTS_DISCOUNT_TOLERANCE, lo, hi

    def _varies_hint(lo: float, hi: float) -> str:
        return (f"<span class='hint' title='Tennents charged more than one discount for this code this month — "
                f"an estate-wide rate can only be set from a single figure. Confirm the rate with Tennents and set it "
                f"in the workbook.'>charged rates vary (£{lo:,.2f}–£{hi:,.2f}/brl)</span>")

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
        act_h = "<th></th>" if can_accept else ""
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th>"
            "<th class='r'>Kegs</th><th class='r'>Brl</th><th class='r'>Charged disc £/brl</th><th>Master note</th>"
            f"{act_h}</tr></thead><tbody>"
        )
        for r in s.no_rate:
            act = ""
            if can_accept:
                mixed, lo, hi = _mixed(norate_range, r.sku_code)
                if mixed:
                    act = f"<td class='action-cell'>{_varies_hint(lo, hi)}</td>"
                elif r.actual_total_per_brl > 0.005:
                    act = (
                        "<td class='action-cell'><button type='button' class='accept-btn t-accept'"
                        " data-mode='set_rate'"
                        f" data-sku=\"{escape(r.sku_code, quote=True)}\""
                        f" data-desc=\"{escape(r.sku_desc, quote=True)}\""
                        f" data-charged=\"{r.actual_total_per_brl:.2f}\">Set rate = charged</button></td>"
                    )
                else:
                    act = ("<td class='action-cell'><span class='hint' title='Tennents applied no discount, "
                           "so there is no charged rate to accept — chase them for the rate'>£0 charged</span></td>")
            parts.append(
                f"<tr data-sku=\"{escape(r.sku_code, quote=True)}\">"
                f"<td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)} {escape(r.sku_desc)}</td>"
                f"<td class='r'>{r.kegs:g}</td><td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'>{_money_neutral(r.actual_total_per_brl)}</td>"
                f"<td class='sub'>{escape(r.note)}</td>{act}</tr>"
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
        act_h = "<th>Add to master</th>" if can_accept else ""
        parts.append(
            "<table><thead><tr>"
            "<th>Account</th><th>Customer</th><th>SKU</th><th>Description</th>"
            "<th class='r'>Kegs</th><th class='r'>Brl</th><th class='r'>Avg invoice</th><th class='r'>Avg disc £/brl</th>"
            f"{act_h}</tr></thead><tbody>"
        )
        for r in s.not_on_master:
            act = ""
            if can_accept:
                # The usual "unknown product" is a NEW CODE for an SKU we already
                # have (Tennents re-code containers) — so the primary action is
                # to LINK it, with the best name-match preselected. "Add as new"
                # only when a real discount was charged: a £0 charge as a new
                # SKU's agreed rate would silently bless £0.
                sug = suggest_sku(master, r.sku_desc) if master else None
                # A placeholder first, so with no suggestion nothing is preselected
                # (the JS refuses an empty choice) — a mis-click can't link a
                # genuinely new product to whichever SKU happens to sort first.
                opts = "<option value=''>— choose —</option>" + "".join(
                    f"<option value=\"{escape(code, quote=True)}\""
                    f"{' selected' if sug is not None and code == sug.sku_code else ''}>{escape(label)}</option>"
                    for code, label in sku_choices
                )
                mixed, lo, hi = _mixed(nom_range, r.sku_code)
                if mixed:
                    new_btn = _varies_hint(lo, hi)
                elif r.avg_discount_per_brl > 0.005:
                    new_btn = (
                        "<button type='button' class='accept-btn t-accept' data-mode='new'"
                        f" data-sku=\"{escape(r.sku_code, quote=True)}\""
                        f" data-desc=\"{escape(r.sku_desc, quote=True)}\""
                        f" data-container=\"{escape(_container_of(r.sku_desc), quote=True)}\""
                        f" data-charged=\"{r.avg_discount_per_brl:.2f}\">Add as new @ charged</button>"
                    )
                else:
                    new_btn = ("<span class='hint' title='Tennents applied no discount — adding this as a new SKU "
                               "would record £0 as the agreed rate. Link it to the existing SKU instead.'>"
                               "£0 charged — link instead</span>")
                act = (
                    "<td class='action-cell'>"
                    "<span class='link-accept'>"
                    f"<select class='link-sel' aria-label=\"Existing SKU to link {escape(r.sku_code, quote=True)} to\">{opts}</select>"
                    "<button type='button' class='accept-btn t-accept' data-mode='link'"
                    f" data-sku=\"{escape(r.sku_code, quote=True)}\""
                    f" data-desc=\"{escape(r.sku_desc, quote=True)}\">Link to existing</button>"
                    "</span>"
                    f"{new_btn}</td>"
                )
            parts.append(
                f"<tr data-sku=\"{escape(r.sku_code, quote=True)}\">"
                f"<td>{escape(r.account)}</td><td>{escape(r.customer_name)}</td>"
                f"<td>{escape(r.sku_code)}</td><td>{escape(r.sku_desc)}</td>"
                f"<td class='r'>{r.kegs:g}</td><td class='r'>{r.barrels:.2f}</td>"
                f"<td class='r'>{_money_neutral(r.avg_invoice)}</td>"
                f"<td class='r'>{_money_neutral(r.avg_discount_per_brl)}</td>{act}</tr>"
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

    # 13. Draft email to Tennents — built client-side from the findings above
    # (short-discounts to correct + credit, known corrections still accruing,
    # rates to confirm, arithmetic errors); accepting a no-rate / not-on-master
    # SKU into the master (§7/§8 buttons) drops it from the draft live. The
    # buttons' JS lives in the same block, so it is emitted whenever there is
    # anything actionable on the page.
    def _r(v) -> float:
        return round(float(v or 0.0), 2)

    def _pname(code: str, fallback: str) -> str:
        # The master's product name (covers both containers of a multi-container
        # SKU) rather than the report's truncated description ('Magners Dark Fru').
        sku = None
        if master is not None:
            # exception rows carry the RAW listing, which may be compound ('400751/400557')
            for part in str(code).replace("\\", "/").split("/"):
                sku = master.find_sku(part.strip()) if part.strip() else None
                if sku is not None:
                    break
        name = (sku.product or sku.brand) if sku is not None else ""
        return name or fallback

    email = {
        "file": s.file_name,
        "period": s.period or "",
        "short": [
            {"account": r.account, "site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "expected": _r(r.expected), "actual": _r(r.actual), "delta_brl": _r(r.delta_per_brl),
             "barrels": _r(r.barrels), "delta_total": _r(r.delta_total)}
            for r in s.discount_mismatches if r.delta_total > 0
        ],
        "over": [
            {"account": r.account, "site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "expected": _r(r.expected), "actual": _r(r.actual), "delta_brl": _r(r.delta_per_brl),
             "barrels": _r(r.barrels), "delta_total": _r(r.delta_total)}
            for r in s.discount_mismatches if r.delta_total < 0
        ],
        "pending": [
            {"account": r.account, "site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "loaded": _r(r.loaded), "correct": _r(r.correct), "barrels": _r(r.barrels),
             "short": _r(r.short_vs_correct)}
            for r in s.exception_pending if (r.short_vs_correct or 0.0) > 0
        ],
        "resolved": [
            {"site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "correct": _r(r.correct), "actual": _r(r.actual)}
            for r in s.exceptions_resolved
        ],
        "no_rate": [
            {"account": r.account, "site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "charged": _r(r.actual_total_per_brl), "barrels": _r(r.barrels),
             "lo": _r(norate_range.get(str(r.sku_code).strip().upper(), (r.actual_total_per_brl,))[0]),
             "hi": _r(norate_range.get(str(r.sku_code).strip().upper(), (0, r.actual_total_per_brl))[1])}
            for r in s.no_rate
        ],
        "not_on_master": [
            {"account": r.account, "site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "charged": _r(r.avg_discount_per_brl), "barrels": _r(r.barrels),
             "lo": _r(nom_range.get(str(r.sku_code).strip().upper(), (r.avg_discount_per_brl,))[0]),
             "hi": _r(nom_range.get(str(r.sku_code).strip().upper(), (0, r.avg_discount_per_brl))[1])}
            for r in s.not_on_master
        ],
        "retro_arith": [
            {"site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "what": f"retro due {_money_neutral(r.retro_due)} on the report vs {_money_neutral(r.calc_due)} "
                     f"({_money_neutral(r.retro_per_brl)}/brl x {r.barrels:.4f} brl)"}
            for r in s.retro_arithmetic
        ],
        "line_arith": [
            {"site": r.customer_name, "sku": r.sku_code, "desc": _pname(r.sku_code, r.sku_desc),
             "what": f"off {_money_neutral(r.off_per_brl)} + retro {_money_neutral(r.retro_per_brl)} + AOD "
                     f"{_money_neutral(r.aod_per_brl)} does not equal the total {_money_neutral(r.total_per_brl)}"}
            for r in s.line_arithmetic
        ],
    }
    has_email = any(email[k] for k in ("short", "over", "pending", "resolved",
                                       "no_rate", "not_on_master", "retro_arith", "line_arith"))
    if has_email:
        default_subject = f"FB Taverns — Tennents draught pricing, {_period_label(s.period) or s.file_name}"
        parts.append("<h2>13. Draft email to Tennents</h2>")
        parts.append(
            "<p class='sub'>Auto-drafted from the findings above, grouped the way Tennents load rates (one line per "
            "SKU and applied rate, naming the sites): discounts applied below the agreed rate to correct and "
            f"credit (variances under &pound;{EMAIL_MINOR_GBP:.0f} rolled into one line), known corrections still accruing, rates to "
            "confirm and any arithmetic errors. <strong>Over-discounts are left out by default</strong> (we monitor "
            "them, we don't raise them) &mdash; tick the box to include them. Accepting or linking an SKU into the "
            "master (sections 7 and 8) removes it from this draft. Edit freely, then copy or open in your mail app.</p>"
        )
        parts.append(
            "<div class='email-draft'>"
            "<label for='t-email-subject'>Subject</label>"
            f"<input type='text' id='t-email-subject' value=\"{escape(default_subject, quote=True)}\">"
            "<label for='t-email-body'>Body</label>"
            "<textarea id='t-email-body'></textarea>"
            "<div class='email-actions'>"
            "<button type='button' id='t-email-copy'>Copy email</button>"
            "<a class='button' id='t-email-mailto' href='#' style='margin-top:0'>Open in mail app</a>"
            "<label class='email-opt'><input type='checkbox' id='t-email-include-over'> Include over-discounts "
            "<span class='sub'>(off by default: monitored, not raised)</span></label>"
            "<span class='ok' id='t-email-copied' style='display:none'>Copied &check;</span>"
            "<span id='t-email-dirty-note' style='display:none'>&#9888; Edited &mdash; accepted items are no longer auto-removed; delete them by hand.</span>"
            "</div></div>"
        )
    if has_email or can_accept:
        parts.append(_tennents_findings_script({
            "acceptUrl": accept_url,
            "sourceFile": source_file or s.file_name,
            "canAccept": can_accept,
            "signoff": actor_name or "",
            "minorGbp": EMAIL_MINOR_GBP,
            "email": email,
        }))

    return "\n".join(parts)

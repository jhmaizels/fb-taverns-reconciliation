"""
FB Taverns drinks reconciliation — Phase 1 prototype.

Two subcommands:
  build-master  Read per-tenant pricing Excel files into an effective-dated master CSV.
  reconcile     Compare an LWC weekly sales Excel against the master CSV and report mismatches.

See README.md in this folder.
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

MASTER_COLUMNS = [
    "site_id",
    "product_code",
    "product_desc",
    "tenant_price",
    "fb_price",
    "retro_pct",
    "valid_from",
    "valid_to",
    "status",
    "reason",
    "source",
]

SITES_COLUMNS = ["site_id", "name", "status", "notes"]

REPORT_COLUMNS = [
    "type",
    "severity",
    "site_id",
    "site_name",
    "product_code",
    "product_desc",
    "invoice_no",
    "invoice_date",
    "qty",
    "expected_tenant_price",
    "actual_tenant_price",
    "expected_fb_price",
    "actual_fb_price",
    "delta_per_unit",
    "delta_total",
    "rule_valid_from",
    "rule_valid_to",
    "notes",
]


# ---------- shared helpers ----------

def _to_str_code(value) -> str:
    """Normalise a product or site code to a clean string (no trailing .0)."""
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _parse_site_from_name(name) -> tuple[str, str]:
    """Tenant-pricing 'Name' column is typically '809    HUMBER TAVERN'."""
    if pd.isna(name):
        return "", ""
    s = str(name).strip()
    m = re.match(r"^(\d+)\s+(.*)$", s)
    if m:
        return m.group(1), m.group(2).strip()
    return "", s


def _parse_date(value, default: date | None = None) -> date | None:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return default
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return default


# ---------- master loading & writing ----------

@dataclass
class Rule:
    site_id: str
    product_code: str
    product_desc: str = ""
    tenant_price: float | None = None
    fb_price: float | None = None
    retro_pct: float = 0.0
    valid_from: date | None = None
    valid_to: date | None = None
    status: str = "tenanted"
    reason: str = ""
    source: str = ""

    def to_row(self) -> dict:
        return {
            "site_id": self.site_id,
            "product_code": self.product_code,
            "product_desc": self.product_desc,
            "tenant_price": "" if self.tenant_price is None else f"{self.tenant_price:.4f}",
            "fb_price": "" if self.fb_price is None else f"{self.fb_price:.4f}",
            "retro_pct": f"{self.retro_pct:.4f}",
            "valid_from": self.valid_from.isoformat() if self.valid_from else "",
            "valid_to": self.valid_to.isoformat() if self.valid_to else "",
            "status": self.status,
            "reason": self.reason,
            "source": self.source,
        }


def load_master(path: str | Path) -> list[Rule]:
    p = Path(path)
    if not p.exists():
        return []
    rules: list[Rule] = []
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rules.append(
                Rule(
                    site_id=str(row.get("site_id", "")).strip(),
                    product_code=str(row.get("product_code", "")).strip(),
                    product_desc=row.get("product_desc", "") or "",
                    tenant_price=float(row["tenant_price"]) if row.get("tenant_price") else None,
                    fb_price=float(row["fb_price"]) if row.get("fb_price") else None,
                    retro_pct=float(row["retro_pct"]) if row.get("retro_pct") else 0.0,
                    valid_from=_parse_date(row.get("valid_from")),
                    valid_to=_parse_date(row.get("valid_to")),
                    status=row.get("status") or "tenanted",
                    reason=row.get("reason") or "",
                    source=row.get("source") or "",
                )
            )
    return rules


def write_master(path: str | Path, rules: list[Rule]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rules_sorted = sorted(
        rules,
        key=lambda r: (r.site_id, r.product_code, r.valid_from or date.min),
    )
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_COLUMNS)
        writer.writeheader()
        for r in rules_sorted:
            writer.writerow(r.to_row())


def load_sites(path: str | Path) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    sites: dict[str, dict] = {}
    with p.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sid = str(row.get("site_id", "")).strip()
            if sid:
                sites[sid] = {
                    "name": row.get("name") or "",
                    "status": (row.get("status") or "tenanted").strip().lower(),
                    "notes": row.get("notes") or "",
                }
    return sites


# ---------- build-master subcommand ----------

def _pick_new_price_column(columns: list[str]) -> str | None:
    """Per-tenant Excels label the new price column inconsistently."""
    preferred = ["Final New Price", "New Price 1st April", "New Price"]
    for name in preferred:
        if name in columns:
            return name
    for c in columns:
        if str(c).strip().lower().startswith("new price"):
            return c
    return None


def parse_tenant_pricing_folder(folder: str) -> tuple[list[Rule], dict[str, str], list[tuple[str, str]]]:
    """Returns (rules with tenant_price set, sites seen, skipped (filename, reason))."""
    files = sorted(glob.glob(str(Path(folder) / "*.xlsx")))
    rules: list[Rule] = []
    seen_sites: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []

    for fp in files:
        try:
            df = pd.read_excel(fp, sheet_name=0)
        except Exception as exc:
            skipped.append((os.path.basename(fp), f"read failed: {exc}"))
            continue

        df = df.rename(columns={c: str(c).strip() for c in df.columns})
        if not {"Name", "Product Code", "Description"}.issubset(df.columns):
            skipped.append((os.path.basename(fp), f"missing core columns; got {list(df.columns)}"))
            continue

        price_col = _pick_new_price_column(list(df.columns))
        fallback_col = "Current Price" if "Current Price" in df.columns else None
        if price_col is None and fallback_col is None:
            skipped.append((os.path.basename(fp), f"no price column found; got {list(df.columns)}"))
            continue

        for _, row in df.iterrows():
            site_id, site_name = _parse_site_from_name(row.get("Name"))
            if not site_id:
                continue
            product_code = _to_str_code(row.get("Product Code"))
            if not product_code:
                continue

            price = row.get(price_col) if price_col else None
            if price is None or pd.isna(price):
                price = row.get(fallback_col) if fallback_col else None
            if price is None or pd.isna(price):
                continue

            seen_sites.setdefault(site_id, site_name)
            rules.append(
                Rule(
                    site_id=site_id,
                    product_code=product_code,
                    product_desc=str(row.get("Description") or "").strip(),
                    tenant_price=float(price),
                    source=os.path.basename(fp),
                )
            )

    return rules, seen_sites, skipped


def parse_fb_cost_file(path: str) -> tuple[list[Rule], dict[str, str]]:
    """Wide-form file: rows are products; cols 0-4 = code,name,price,retro,net_price; cols 5+ = per-site tenant prices."""
    df = pd.read_excel(path, sheet_name=0, header=None)
    site_header = df.iloc[1]
    site_cols: dict[int, tuple[str, str]] = {}
    for c in range(5, df.shape[1]):
        h = site_header.iloc[c]
        if pd.isna(h):
            continue
        m = re.search(r"(\d{3,})\s*$", str(h).strip())
        if not m:
            continue
        sid = m.group(1)
        name = str(h).replace(sid, "").strip()
        site_cols[c] = (sid, name)

    seen_sites = {sid: name for sid, name in site_cols.values()}
    rules: list[Rule] = []

    for r in range(2, df.shape[0]):
        product_code = _to_str_code(df.iat[r, 0])
        product_name = str(df.iat[r, 1] or "").strip() if not pd.isna(df.iat[r, 1]) else ""
        if not product_code or not product_name:
            continue

        price_raw = df.iat[r, 2]
        retro_raw = df.iat[r, 3]
        # NB: fb_price is the LIST price (col 2), not Net price (col 4 = post-retro).
        # LWC's weekly MASTER column is the list price; retro is a separate monthly rebate.
        try:
            fb_price = float(price_raw) if not pd.isna(price_raw) else None
        except (TypeError, ValueError):
            fb_price = None
        try:
            retro_per_unit = float(retro_raw) if not pd.isna(retro_raw) else 0.0
        except (TypeError, ValueError):
            retro_per_unit = 0.0

        retro_pct = 0.0
        if fb_price and retro_per_unit:
            retro_pct = retro_per_unit / fb_price

        for col, (sid, _) in site_cols.items():
            cell = df.iat[r, col]
            if pd.isna(cell):
                continue
            try:
                tenant_price = float(cell)
            except (TypeError, ValueError):
                continue
            rules.append(
                Rule(
                    site_id=sid,
                    product_code=product_code,
                    product_desc=product_name,
                    tenant_price=tenant_price,
                    fb_price=fb_price,
                    retro_pct=retro_pct,
                    source=os.path.basename(path),
                )
            )
    return rules, seen_sites


def build_master(
    out_csv: str,
    valid_from: date,
    fb_cost_file: str | None = None,
    tenant_folder: str | None = None,
    sites_csv: str | None = None,
) -> None:
    if not fb_cost_file and not tenant_folder:
        sys.exit("Provide at least one of --fb-cost or --tenant-folder")

    by_key: dict[tuple[str, str], Rule] = {}
    seen_sites: dict[str, str] = {}
    skipped: list[tuple[str, str]] = []
    fb_count = tenant_count = 0

    if fb_cost_file:
        if not Path(fb_cost_file).exists():
            sys.exit(f"FB cost file not found: {fb_cost_file}")
        fb_rules, fb_sites = parse_fb_cost_file(fb_cost_file)
        fb_count = len(fb_rules)
        for sid, name in fb_sites.items():
            seen_sites.setdefault(sid, name)
        for r in fb_rules:
            by_key[(r.site_id, r.product_code)] = r

    if tenant_folder:
        if not Path(tenant_folder).exists():
            sys.exit(f"Tenant folder not found: {tenant_folder}")
        tenant_rules, tenant_sites, skipped = parse_tenant_pricing_folder(tenant_folder)
        tenant_count = len(tenant_rules)
        for sid, name in tenant_sites.items():
            seen_sites.setdefault(sid, name)
        for r in tenant_rules:
            existing = by_key.get((r.site_id, r.product_code))
            if existing is not None:
                existing.tenant_price = r.tenant_price
                if not existing.product_desc and r.product_desc:
                    existing.product_desc = r.product_desc
                existing.source = f"{existing.source} + {r.source}" if existing.source else r.source
            else:
                by_key[(r.site_id, r.product_code)] = r

    new_rules = list(by_key.values())
    for r in new_rules:
        r.valid_from = valid_from

    existing = load_master(out_csv)
    keys_in_new = set(by_key.keys())
    closed = 0
    for rule in existing:
        if (rule.site_id, rule.product_code) in keys_in_new and rule.valid_to is None:
            rule.valid_to = valid_from
            closed += 1

    combined = existing + new_rules
    write_master(out_csv, combined)

    print(f"Wrote {len(combined)} total rules to {out_csv}")
    print(f"  From FB cost file        : {fb_count}")
    print(f"  From per-tenant pricing  : {tenant_count}")
    print(f"  Net new rules this run   : {len(new_rules)}")
    print(f"  Prior open rules closed at {valid_from}: {closed}")
    if skipped:
        print("  Skipped tenant files:")
        for name, why in skipped:
            print(f"    - {name}: {why}")

    if sites_csv:
        sites_p = Path(sites_csv)
        existing_sites = load_sites(sites_p)
        added = 0
        for sid, name in seen_sites.items():
            if sid not in existing_sites:
                existing_sites[sid] = {"name": name, "status": "tenanted", "notes": ""}
                added += 1
        sites_p.parent.mkdir(parents=True, exist_ok=True)
        with sites_p.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SITES_COLUMNS)
            writer.writeheader()
            for sid in sorted(existing_sites.keys()):
                row = existing_sites[sid]
                writer.writerow({"site_id": sid, **row})
        print(f"  Sites file: {len(existing_sites)} entries ({added} new) -> {sites_csv}")


# ---------- LWC sales parser ----------

LWC_COLUMN_MAP = {
    "DEPOT": "depot",
    "ACCOUNT NO": "account_no",
    "SITE ID": "site_id",
    "ACCOUNT": "account_name",
    "PRODUCT CODE": "product_code",
    "PRODUCT DESC": "product_desc",
    "INVOICE NO": "invoice_no",
    "DATE": "invoice_date",
    "QTY": "qty",
    "B'BRLS": "barrels",
    "SALES": "sales_total",
    "UNIT": "unit_price",
    "MASTER": "master_price",
    "DIFF. MASTER": "diff_master",
    "DIFF. + VAT": "diff_plus_vat",
}


@dataclass
class InvoiceLine:
    site_id: str
    site_name: str
    product_code: str
    product_desc: str
    invoice_no: str
    invoice_date: date | None
    qty: float
    unit_price: float
    master_price: float
    diff_master: float
    raw: dict = field(default_factory=dict)


def _normalise_lwc_columns(df: pd.DataFrame) -> pd.DataFrame:
    new_cols = {}
    for c in df.columns:
        clean = re.sub(r"\s+", " ", str(c).replace("\n", " ")).strip().upper()
        new_cols[c] = LWC_COLUMN_MAP.get(clean, clean.lower())
    return df.rename(columns=new_cols)


def _find_line_sheet(xl: pd.ExcelFile) -> str:
    for cand in ("FB_Taverns_Del_Date", "Del_Date"):
        if cand in xl.sheet_names:
            return cand
    for s in xl.sheet_names:
        if "Date" in s and "Pivot" not in s:
            return s
    sys.exit(
        f"No line-level sheet found in workbook. Sheets present: {xl.sheet_names}. "
        "Older 'Diff. From Master' format is not yet supported in Phase 1."
    )


def parse_lwc_sales(path: str) -> list[InvoiceLine]:
    with pd.ExcelFile(path) as xl:
        sheet = _find_line_sheet(xl)
        df = pd.read_excel(xl, sheet_name=sheet)
    df = _normalise_lwc_columns(df)

    required = {"site_id", "product_code", "invoice_date", "qty", "unit_price", "master_price"}
    missing = required - set(df.columns)
    if missing:
        sys.exit(
            f"LWC file missing required columns: {missing}. Got: {list(df.columns)}"
        )

    lines: list[InvoiceLine] = []
    for _, row in df.iterrows():
        if pd.isna(row.get("site_id")) or pd.isna(row.get("product_code")):
            continue
        try:
            qty = float(row.get("qty") or 0)
            unit = float(row.get("unit_price") or 0)
            master = float(row.get("master_price") or 0)
        except (TypeError, ValueError):
            continue
        diff = float(row.get("diff_master") or 0) if not pd.isna(row.get("diff_master")) else 0.0
        lines.append(
            InvoiceLine(
                site_id=_to_str_code(row.get("site_id")),
                site_name=str(row.get("account_name") or "").strip(),
                product_code=_to_str_code(row.get("product_code")),
                product_desc=str(row.get("product_desc") or "").strip(),
                invoice_no=_to_str_code(row.get("invoice_no")),
                invoice_date=_parse_date(row.get("invoice_date")),
                qty=qty,
                unit_price=unit,
                master_price=master,
                diff_master=diff,
                raw=row.to_dict(),
            )
        )
    return lines


# ---------- reconciliation ----------

@dataclass
class Mismatch:
    type: str
    severity: str
    line: InvoiceLine
    rule: Rule | None = None
    expected_tenant_price: float | None = None
    actual_tenant_price: float | None = None
    expected_fb_price: float | None = None
    actual_fb_price: float | None = None
    delta_per_unit: float = 0.0
    delta_total: float = 0.0
    notes: str = ""

    def to_row(self) -> dict:
        return {
            "type": self.type,
            "severity": self.severity,
            "site_id": self.line.site_id,
            "site_name": self.line.site_name,
            "product_code": self.line.product_code,
            "product_desc": self.line.product_desc,
            "invoice_no": self.line.invoice_no,
            "invoice_date": self.line.invoice_date.isoformat() if self.line.invoice_date else "",
            "qty": self.line.qty,
            "expected_tenant_price": "" if self.expected_tenant_price is None else f"{self.expected_tenant_price:.4f}",
            "actual_tenant_price": "" if self.actual_tenant_price is None else f"{self.actual_tenant_price:.4f}",
            "expected_fb_price": "" if self.expected_fb_price is None else f"{self.expected_fb_price:.4f}",
            "actual_fb_price": "" if self.actual_fb_price is None else f"{self.actual_fb_price:.4f}",
            "delta_per_unit": f"{self.delta_per_unit:.4f}",
            "delta_total": f"{self.delta_total:.4f}",
            "rule_valid_from": self.rule.valid_from.isoformat() if self.rule and self.rule.valid_from else "",
            "rule_valid_to": self.rule.valid_to.isoformat() if self.rule and self.rule.valid_to else "",
            "notes": self.notes,
        }


def _severity(delta_total: float) -> str:
    a = abs(delta_total)
    if a < 0.05:
        return "low"
    if a < 0.50:
        return "medium"
    return "high"


def _index_rules(rules: list[Rule]) -> dict[tuple[str, str], list[Rule]]:
    idx: dict[tuple[str, str], list[Rule]] = {}
    for r in rules:
        idx.setdefault((r.site_id, r.product_code), []).append(r)
    # Sort newest valid_from first; if rules accidentally overlap, the most recent wins.
    for k in idx:
        idx[k].sort(key=lambda x: x.valid_from or date.min, reverse=True)
    return idx


def _lookup_rule(
    idx: dict[tuple[str, str], list[Rule]],
    site_id: str,
    product_code: str,
    on_date: date | None,
) -> Rule | None:
    candidates = idx.get((site_id, product_code), [])
    if not candidates:
        return None
    if on_date is None:
        return candidates[0]
    for r in candidates:
        vf = r.valid_from or date.min
        vt = r.valid_to or date.max
        if vf <= on_date < vt:
            return r
    return None


def reconcile_lines(
    lines: list[InvoiceLine],
    rules: list[Rule],
    sites: dict[str, dict],
    tolerance: float = 0.01,
) -> list[Mismatch]:
    idx = _index_rules(rules)
    sites_with_rules = {sid for sid, _ in {(r.site_id, r.product_code) for r in rules}}
    mismatches: list[Mismatch] = []

    for line in lines:
        site_info = sites.get(line.site_id)
        site_status = (site_info or {}).get("status") if site_info else None

        # arithmetic sanity check on LWC's own DIFF.MASTER number
        expected_diff = (line.unit_price - line.master_price) * line.qty
        if abs(expected_diff - line.diff_master) > 0.02:
            mismatches.append(
                Mismatch(
                    type="lwc_arithmetic_error",
                    severity=_severity(expected_diff - line.diff_master),
                    line=line,
                    expected_tenant_price=expected_diff,
                    actual_tenant_price=line.diff_master,
                    delta_per_unit=(expected_diff - line.diff_master) / line.qty if line.qty else 0,
                    delta_total=expected_diff - line.diff_master,
                    notes="LWC's DIFF. MASTER does not equal (UNIT - MASTER) * QTY",
                )
            )

        rule = _lookup_rule(idx, line.site_id, line.product_code, line.invoice_date)

        if not rule:
            if site_status == "managed":
                if abs(line.unit_price - line.master_price) > tolerance:
                    mismatches.append(
                        Mismatch(
                            type="site_should_be_managed",
                            severity=_severity((line.unit_price - line.master_price) * line.qty),
                            line=line,
                            expected_tenant_price=line.master_price,
                            actual_tenant_price=line.unit_price,
                            delta_per_unit=line.unit_price - line.master_price,
                            delta_total=(line.unit_price - line.master_price) * line.qty,
                            notes=f"Site {line.site_id} flagged managed but charged with margin",
                        )
                    )
                continue
            if line.site_id not in sites_with_rules:
                mismatches.append(
                    Mismatch(
                        type="unknown_site",
                        severity="medium",
                        line=line,
                        notes=f"No rules for site {line.site_id} ({line.site_name})",
                    )
                )
                continue
            mismatches.append(
                Mismatch(
                    type="no_rule_for_line",
                    severity="medium",
                    line=line,
                    notes=(
                        f"No rule for site {line.site_id} / product {line.product_code} "
                        f"on {line.invoice_date}"
                    ),
                )
            )
            continue

        if rule.status == "managed":
            if abs(line.unit_price - line.master_price) > tolerance:
                mismatches.append(
                    Mismatch(
                        type="site_should_be_managed",
                        severity=_severity((line.unit_price - line.master_price) * line.qty),
                        line=line,
                        rule=rule,
                        expected_tenant_price=line.master_price,
                        actual_tenant_price=line.unit_price,
                        delta_per_unit=line.unit_price - line.master_price,
                        delta_total=(line.unit_price - line.master_price) * line.qty,
                        notes=f"Rule says managed (no margin) but charged with margin",
                    )
                )
            continue

        if rule.tenant_price is not None:
            delta_unit = line.unit_price - rule.tenant_price
            if abs(delta_unit) > tolerance:
                mismatches.append(
                    Mismatch(
                        type="wrong_tenant_price",
                        severity=_severity(delta_unit * line.qty),
                        line=line,
                        rule=rule,
                        expected_tenant_price=rule.tenant_price,
                        actual_tenant_price=line.unit_price,
                        delta_per_unit=delta_unit,
                        delta_total=delta_unit * line.qty,
                    )
                )

        if rule.fb_price is not None:
            delta_unit = line.master_price - rule.fb_price
            if abs(delta_unit) > tolerance:
                mismatches.append(
                    Mismatch(
                        type="wrong_fb_price",
                        severity=_severity(delta_unit * line.qty),
                        line=line,
                        rule=rule,
                        expected_fb_price=rule.fb_price,
                        actual_fb_price=line.master_price,
                        delta_per_unit=delta_unit,
                        delta_total=delta_unit * line.qty,
                    )
                )

    return mismatches


def write_report(path: str, mismatches: list[Mismatch]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for m in mismatches:
            writer.writerow(m.to_row())


def print_summary(lines: list[InvoiceLine], mismatches: list[Mismatch]) -> None:
    print(f"\n=== Reconciliation summary ===")
    print(f"Lines processed : {len(lines)}")
    print(f"Mismatches      : {len(mismatches)}")
    if not mismatches:
        print("All clean.")
        return

    by_type: dict[str, list[Mismatch]] = {}
    for m in mismatches:
        by_type.setdefault(m.type, []).append(m)
    print("\nBy type:")
    for t, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        total = sum(m.delta_total for m in items)
        print(f"  {t:30s}  count={len(items):4d}  total_delta=£{total:+,.2f}")

    by_site: dict[str, float] = {}
    for m in mismatches:
        if m.type in ("wrong_tenant_price", "wrong_fb_price", "site_should_be_managed"):
            by_site[m.line.site_id] = by_site.get(m.line.site_id, 0) + abs(m.delta_total)
    if by_site:
        print("\nTop sites by abs £ exposure:")
        for sid, val in sorted(by_site.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  site {sid}  £{val:,.2f}")


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    bp = sub.add_parser(
        "build-master",
        help="Load FB cost file and/or per-tenant pricing Excels into master CSV",
    )
    bp.add_argument(
        "--fb-cost",
        default=None,
        help="Path to wide-form FB cost price file (.xlsx). Provides fb_price, retro, and per-site tenant prices.",
    )
    bp.add_argument(
        "--tenant-folder",
        default=None,
        help="Folder of per-tenant pricing .xlsx files. Overrides tenant_price for matching (site, product).",
    )
    bp.add_argument("--out", default="master_pricing.csv", help="Output master CSV path")
    bp.add_argument(
        "--valid-from",
        default=date.today().isoformat(),
        help="Effective date for the new prices (YYYY-MM-DD). Defaults to today.",
    )
    bp.add_argument(
        "--sites-out",
        default="sites.csv",
        help="Sites CSV to create/update with site_ids encountered.",
    )
    bp.add_argument(
        "--to-airtable",
        action="store_true",
        help="Also push the new rules to Airtable PricingRules, closing prior open rules at --valid-from.",
    )

    rp = sub.add_parser("reconcile", help="Reconcile an LWC weekly sales Excel")
    rp.add_argument("sales_file", help="LWC weekly sales .xlsx")
    rp.add_argument("--master", default="master_pricing.csv", help="Master pricing CSV (ignored if --use-airtable)")
    rp.add_argument("--sites", default="sites.csv", help="Sites CSV (ignored if --use-airtable)")
    rp.add_argument("--out", default=None, help="Mismatch report CSV (default: outputs/<sales-stem>__mismatches.csv)")
    rp.add_argument("--tolerance", type=float, default=0.01, help="Per-unit £ tolerance")
    rp.add_argument(
        "--use-airtable",
        action="store_true",
        help="Load rules/sites from Airtable and push mismatches + file record back to Airtable.",
    )

    args = p.parse_args(argv)

    if args.cmd == "build-master":
        vf = _parse_date(args.valid_from)
        if vf is None:
            sys.exit(f"Could not parse --valid-from: {args.valid_from}")
            return 2
        build_master(
            out_csv=args.out,
            valid_from=vf,
            fb_cost_file=args.fb_cost,
            tenant_folder=args.tenant_folder,
            sites_csv=args.sites_out,
        )
        if args.to_airtable:
            from airtable_io import upsert_pricing_rules
            new_rules = [r for r in load_master(args.out) if r.valid_from == vf]
            print(f"\nPushing {len(new_rules)} new rules to Airtable…")
            created, updated, closed = upsert_pricing_rules(new_rules, close_keys_at_date=vf)
            print(f"  Airtable: created={created} updated={updated} closed_prior={closed}")
        return 0

    if args.cmd == "reconcile":
        if args.use_airtable:
            from airtable_io import (
                load_rules_from_airtable,
                load_sites_from_airtable,
                upsert_file_record,
                write_mismatches,
            )
            print("Loading master from Airtable…")
            rules = load_rules_from_airtable()
            sites = load_sites_from_airtable()
        else:
            rules = load_master(args.master)
            if not rules:
                sys.exit(f"No rules loaded from {args.master}. Run build-master first.")
            sites = load_sites(args.sites) if args.sites else {}

        lines = parse_lwc_sales(args.sales_file)
        if not rules:
            sys.exit("No pricing rules loaded — refusing to reconcile.")
        mismatches = reconcile_lines(lines, rules, sites, tolerance=args.tolerance)

        out_path = args.out
        if out_path is None:
            stem = Path(args.sales_file).stem
            out_path = str(Path("outputs") / f"{stem}__mismatches.csv")
        write_report(out_path, mismatches)
        print_summary(lines, mismatches)
        print(f"\nReport written to: {out_path}")

        if args.use_airtable:
            print("\nPushing to Airtable…")
            file_id = upsert_file_record(args.sales_file, supplier="LWC", line_count=len(lines))
            n = write_mismatches(mismatches, file_id)
            print(f"  Files row: {file_id}")
            print(f"  Mismatches inserted: {n}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

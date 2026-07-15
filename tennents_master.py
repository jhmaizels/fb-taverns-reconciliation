"""
Tennents Direct master price file — FB_Taverns_Tennents_Master.xlsx.

This workbook is the PRIMARY price file for the Tennents Direct estate
(operator direction 2026-07-14; supersedes the "Commercial Data" per-account
agreements). Its own README sheet is the spec; §4 is the reconciliation logic:

  For each Draught Pricing Report row: expected total discount = SKU_Master
  "CURRENT CORRECT Total Discount" unless a Site_SKU_Exceptions row overrides
  it (use "Loaded" value as expected-current until the exception status shows
  resolved). Tolerance ±£0.50/brl (rounding). Retro check: retro due must
  equal retro £/brl × barrels exactly. Managed sites: zero retro + full
  discount off-invoice is CORRECT (see Site_Master construct column).
  Gartocher: flat £200/brl retro construct — validate total discount, not
  the split.

Sheets parsed:
  README              -> version string (section "7. Version")
  SKU_Master          -> estate-wide per-SKU rates (SkuRate)
  Site_Master         -> sites, operating model, discount construct (SiteInfo)
  Site_SKU_Exceptions -> per-(site, SKU) overrides (SkuException)

Update rules (workbook README §5): the workbook is the editing surface — on
any change the operator bumps the version and re-uploads; the app replaces
the stored master wholesale. Never back-edit history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

REQUIRED_SHEETS = ("SKU_Master", "Site_Master", "Site_SKU_Exceptions")

# SKU_Master internal consistency: base + hold should equal the CURRENT CORRECT
# total. £0.05 — tighter picks up spreadsheet float artefacts (same threshold
# as the old per-agreement master arithmetic check).
MASTER_ARITH_TOLERANCE = 0.05


# ---------- row shapes ----------

@dataclass
class SkuRate:
    sku_code: str
    alt_code: str
    brand: str
    product: str
    container: str
    brl_per_unit: float | None
    abv: float | None
    wsp_per_brl: float | None
    contract_base_per_brl: float | None
    on_contract: bool
    supplier_type: str                    # "C&C" | "3rd party"
    hold_per_brl: float
    correct_total_per_brl: float | None   # None = no agreed rate yet (RATE TBC)
    source: str = ""
    notes: str = ""

    @property
    def implied_total(self) -> float | None:
        if self.contract_base_per_brl is None:
            return None
        return float(self.contract_base_per_brl) + float(self.hold_per_brl or 0)


@dataclass
class SiteInfo:
    account: str            # Tennents account number as string; may be "TBC"
    site_name: str
    operating_model: str    # raw text, e.g. "Tenanted (TBC)" / "MANAGED (confirmed)"
    discount_construct: str
    notes: str = ""

    @property
    def is_managed(self) -> bool:
        return "MANAGED" in (self.operating_model or "").upper()

    @property
    def flat_retro_per_brl(self) -> float | None:
        """Bespoke flat retro £/brl (Gartocher: £200) parsed from the construct."""
        m = re.search(r"flat\s*£\s*([\d.]+)\s*/\s*brl", self.discount_construct or "", re.I)
        return float(m.group(1)) if m else None

    @property
    def is_bespoke(self) -> bool:
        return "BESPOKE" in (self.discount_construct or "").upper()


@dataclass
class SkuException:
    site_name: str
    account: str                          # resolved via Site_Master; "" if unknown
    sku_code_raw: str                     # as in the sheet; may be compound "400751/400557"
    product: str
    loaded_total_per_brl: float | None    # expected-current until resolved
    correct_total_per_brl: float | None   # target rate once Tennents fix lands
    direction: str = ""
    impact_gbp: float | None = None
    status: str = ""
    # Explicit override (the Airtable `resolved` checkbox). None = derive from
    # the status text, so ticking the box in Airtable retires an exception
    # without a workbook re-upload.
    resolved_flag: bool | None = None

    @property
    def sku_codes(self) -> list[str]:
        return [c.strip() for c in str(self.sku_code_raw).split("/") if c.strip()]

    @property
    def resolved(self) -> bool:
        if self.resolved_flag is not None:
            return self.resolved_flag
        return "resolved" in (self.status or "").lower()


@dataclass
class RateBasis:
    """Outcome of an expected-rate lookup for one (account, sku) pair."""
    basis: str                            # 'sku_master' | 'exception' | 'no_rate' | 'unknown_sku'
    expected: float | None                # expected-current total discount £/brl
    sku: SkuRate | None = None
    exception: SkuException | None = None


@dataclass
class TennentsMaster:
    version: str
    source: str
    skus: list[SkuRate]
    sites: list[SiteInfo]
    exceptions: list[SkuException]

    _sku_index: dict[str, SkuRate] = field(default_factory=dict, repr=False)
    _site_by_account: dict[str, SiteInfo] = field(default_factory=dict, repr=False)
    _exception_index: dict[tuple[str, str], SkuException] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.reindex()

    def reindex(self) -> None:
        self._sku_index = {}
        for s in self.skus:
            for code in (s.sku_code, s.alt_code):
                if code:
                    self._sku_index[str(code).strip().upper()] = s

        self._site_by_account = {
            s.account: s for s in self.sites if s.account and s.account.upper() != "TBC"
        }

        # Exceptions are keyed by (account, RAW sku code) — NOT canonicalised.
        # Tennents loads rates per specific SKU code, and the workbook's own
        # convention is per-code: a mis-load on the 30L container (GUI003 at
        # Maryhill) says nothing about the 50L (GUI002), and an exception that
        # covers both containers lists both codes ("400751/400557"). Resolved
        # exceptions are dropped — per README §5 the override stops applying.
        self._exception_index = {}
        site_by_name = {s.site_name.strip().upper(): s for s in self.sites}
        for ex in self.exceptions:
            if not ex.account:
                site = site_by_name.get(ex.site_name.strip().upper())
                if site:
                    ex.account = site.account
            if ex.resolved or not ex.account or ex.account.upper() == "TBC":
                continue
            for code in ex.sku_codes:
                self._exception_index[(ex.account, str(code).strip().upper())] = ex

    def canonical_sku(self, code: str) -> str:
        sku = self._sku_index.get(str(code).strip().upper())
        return sku.sku_code if sku else str(code).strip().upper()

    def find_sku(self, code: str) -> SkuRate | None:
        return self._sku_index.get(str(code).strip().upper())

    def site_for_account(self, account: str) -> SiteInfo | None:
        return self._site_by_account.get(str(account).strip())

    def resolve(self, account: str, sku_code: str) -> RateBasis:
        """Expected-current total discount for (account, sku) per README §4."""
        sku = self.find_sku(sku_code)
        ex = self._exception_index.get((str(account).strip(), str(sku_code).strip().upper()))
        if ex is not None:
            return RateBasis(basis="exception", expected=ex.loaded_total_per_brl, sku=sku, exception=ex)
        if sku is None:
            return RateBasis(basis="unknown_sku", expected=None)
        if sku.correct_total_per_brl is None:
            return RateBasis(basis="no_rate", expected=None, sku=sku)
        return RateBasis(basis="sku_master", expected=float(sku.correct_total_per_brl), sku=sku)

    def arithmetic_errors(self) -> list[SkuRate]:
        """SKU rows where contract base + hold ≠ CURRENT CORRECT total."""
        out = []
        for s in self.skus:
            if s.correct_total_per_brl is None or s.implied_total is None:
                continue
            if abs(float(s.correct_total_per_brl) - s.implied_total) > MASTER_ARITH_TOLERANCE:
                out.append(s)
        return out


# ---------- parsing helpers ----------

def _num(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("£", "").replace(",", "")
    if not s or s.upper() in {"TBC", "N/A", "NA", "-", "—"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _text(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _account_str(v) -> str:
    """17591767 / 17591767.0 / 'TBC' -> normalised string."""
    n = _num(v)
    if n is not None and float(n).is_integer():
        return str(int(n))
    return _text(v)


def _find_col(df: pd.DataFrame, *prefixes: str) -> str | None:
    """First column whose stripped name starts with any prefix (case-insensitive).

    Headers embed dates that move with each PINC ('WSP £/brl (post 2-Mar-26)',
    '£ Impact Jun-26'), so exact-name matching would break on every version bump.
    """
    for col in df.columns:
        name = str(col).strip().upper()
        for p in prefixes:
            if name.startswith(p.upper()):
                return col
    return None


def _require_cols(df: pd.DataFrame, sheet: str, cols: dict[str, str | None]) -> None:
    missing = [label for label, col in cols.items() if col is None]
    if missing:
        raise ValueError(f"{sheet} sheet is missing expected column(s): {', '.join(missing)}")


# ---------- workbook parsing ----------

def parse_master_workbook(path: str, source_name: str = "") -> TennentsMaster:
    """Parse FB_Taverns_Tennents_Master.xlsx into a TennentsMaster."""
    book = pd.read_excel(path, sheet_name=None)
    missing = [s for s in REQUIRED_SHEETS if s not in book]
    if missing:
        raise ValueError(
            f"Not a Tennents master workbook — missing sheet(s): {', '.join(missing)}. "
            f"Expected FB_Taverns_Tennents_Master.xlsx with sheets {', '.join(REQUIRED_SHEETS)}."
        )

    version = ""
    if "README" in book:
        rd = book["README"]
        if rd.shape[1] >= 2:
            for _, row in rd.iterrows():
                if "version" in _text(row.iloc[0]).lower():
                    version = _text(row.iloc[1])
                    break

    # --- SKU_Master ---
    df = book["SKU_Master"]
    df.columns = [str(c).strip() for c in df.columns]
    c_code = _find_col(df, "SKU Code")
    c_alt = _find_col(df, "Alt Code")
    c_brand = _find_col(df, "Brand")
    c_prod = _find_col(df, "Product")
    c_cont = _find_col(df, "Container")
    c_bpu = _find_col(df, "Brl per Unit")
    c_abv = _find_col(df, "ABV")
    c_wsp = _find_col(df, "WSP")
    c_base = _find_col(df, "Contract Base Discount")
    c_onc = _find_col(df, "On Contract")
    c_sup = _find_col(df, "C&C")
    c_hold = _find_col(df, "50% Hold")
    c_tot = _find_col(df, "CURRENT CORRECT")
    c_src = _find_col(df, "Source")
    c_note = _find_col(df, "Status / Notes", "Status/Notes", "Notes")
    _require_cols(df, "SKU_Master", {
        "SKU Code": c_code, "Product": c_prod,
        "CURRENT CORRECT Total Discount": c_tot,
    })

    skus: list[SkuRate] = []
    for _, row in df.iterrows():
        code = _text(row[c_code])
        if not code:
            continue
        skus.append(SkuRate(
            sku_code=code,
            alt_code=_text(row[c_alt]) if c_alt else "",
            brand=_text(row[c_brand]) if c_brand else "",
            product=_text(row[c_prod]),
            container=_text(row[c_cont]) if c_cont else "",
            brl_per_unit=_num(row[c_bpu]) if c_bpu else None,
            abv=_num(row[c_abv]) if c_abv else None,
            wsp_per_brl=_num(row[c_wsp]) if c_wsp else None,
            contract_base_per_brl=_num(row[c_base]) if c_base else None,
            on_contract=_text(row[c_onc]).upper().startswith("Y") if c_onc else False,
            supplier_type=_text(row[c_sup]) if c_sup else "",
            hold_per_brl=_num(row[c_hold]) or 0.0 if c_hold else 0.0,
            correct_total_per_brl=_num(row[c_tot]),
            source=_text(row[c_src]) if c_src else "",
            notes=_text(row[c_note]) if c_note else "",
        ))
    if not skus:
        raise ValueError("SKU_Master sheet produced zero SKU rows.")

    # --- Site_Master ---
    df = book["Site_Master"]
    df.columns = [str(c).strip() for c in df.columns]
    c_site = _find_col(df, "Site")
    c_acct = _find_col(df, "Tennents Account")
    c_model = _find_col(df, "Operating Model")
    c_constr = _find_col(df, "Discount Construct")
    c_note = _find_col(df, "Notes")
    _require_cols(df, "Site_Master", {
        "Site": c_site, "Tennents Account": c_acct, "Discount Construct": c_constr,
    })

    sites: list[SiteInfo] = []
    for _, row in df.iterrows():
        name = _text(row[c_site])
        account = _account_str(row[c_acct])
        # Trailing commentary rows ("ACTION: …") have no account cell at all.
        if not name or not account:
            continue
        sites.append(SiteInfo(
            account=account,
            site_name=name,
            operating_model=_text(row[c_model]) if c_model else "",
            discount_construct=_text(row[c_constr]),
            notes=_text(row[c_note]) if c_note else "",
        ))
    if not sites:
        raise ValueError("Site_Master sheet produced zero site rows.")

    # --- Site_SKU_Exceptions ---
    df = book["Site_SKU_Exceptions"]
    df.columns = [str(c).strip() for c in df.columns]
    c_site = _find_col(df, "Site")
    c_sku = _find_col(df, "SKU")
    c_prod = _find_col(df, "Product")
    c_loaded = _find_col(df, "Loaded Total Discount")
    c_correct = _find_col(df, "Correct Total Discount")
    c_dir = _find_col(df, "Direction")
    c_impact = _find_col(df, "£ Impact")
    c_status = _find_col(df, "Status")
    _require_cols(df, "Site_SKU_Exceptions", {
        "Site": c_site, "SKU": c_sku, "Loaded Total Discount": c_loaded,
    })

    exceptions: list[SkuException] = []
    for _, row in df.iterrows():
        site_name = _text(row[c_site])
        sku_raw = _text(row[c_sku])
        # The legend row ("Amber = …") has no SKU cell.
        if not site_name or not sku_raw:
            continue
        exceptions.append(SkuException(
            site_name=site_name,
            account="",  # resolved against Site_Master in reindex()
            sku_code_raw=sku_raw,
            product=_text(row[c_prod]) if c_prod else "",
            loaded_total_per_brl=_num(row[c_loaded]),
            correct_total_per_brl=_num(row[c_correct]) if c_correct else None,
            direction=_text(row[c_dir]) if c_dir else "",
            impact_gbp=_num(row[c_impact]) if c_impact else None,
            status=_text(row[c_status]) if c_status else "",
        ))

    return TennentsMaster(
        version=version,
        source=source_name,
        skus=skus,
        sites=sites,
        exceptions=exceptions,
    )

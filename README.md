# FB Taverns reconciliation — Phase 1 + 2 (local CLI + Airtable)

`reconcile.py` does two things from the command line:

1. **`build-master`** — load FB cost file + per-tenant pricing Excels into an effective-dated master (CSV; optionally also pushed to Airtable).
2. **`reconcile`** — compare an LWC weekly sales Excel against the master. Writes a CSV mismatch report; with `--use-airtable`, also reads the master from Airtable and pushes mismatches + a Files row back.

Phase-2 supporting scripts:
- `setup_airtable.py` — creates the 5-table schema in the Airtable base. Run once.
- `migrate_csv_to_airtable.py` — one-shot upload of `sites.csv` and `master_pricing.csv` into Airtable. Idempotent.

---

## Setup (one time)

```bash
pip install pandas openpyxl pyairtable python-dotenv requests
```

Create `.env` with:
```
AIRTABLE_TOKEN=pat...
AIRTABLE_BASE_ID=app...
```

Then:
```bash
python setup_airtable.py            # creates tables
python migrate_csv_to_airtable.py   # uploads existing CSVs (run after build-master)
```

`airtable_schema.json` is written by `setup_airtable.py` and consumed by everything else; commit it but don't edit by hand.

---

## 1. Build / refresh the master from current pricing

The master is **effective-dated** — every rule has a `valid_from` (and optionally `valid_to`). Each `build-master` run adds new rules dated `--valid-from` and closes any prior open rules for the same (site, product) at that date.

You can feed in either or both sources:

- **`--fb-cost`** points at the wide-form `FB Taverns Cost Price File_…xlsx`. Provides `fb_price` (Net price = price after retro), `retro_pct`, and per-site `tenant_price`.
- **`--tenant-folder`** points at the folder of per-tenant pricing Excels (`DOUG TROTMAN FB TAVERNS - …xlsx`). Each file's `Final New Price` (or `New Price 1st April`) populates `tenant_price` and **overrides** the FB cost file's per-site number for the same (site, product).

Typical usage when the new RPI prices take effect on 1 April:

```bash
python reconcile.py build-master \
  --fb-cost "../../../Pricing/Claude/FB Taverns Cost Price File_Apr 26 v1.xlsx" \
  --tenant-folder "../../../Pricing/Claude/Individual tenant pricing/excel" \
  --valid-from 2026-04-01
```

This writes `master_pricing.csv` and creates/updates `sites.csv` with any new site_ids encountered.

To load just one source (e.g. only an updated per-tenant pricing batch), pass only that flag.

### To feed in an updated per-tenant pricing letter

Drop the new `.xlsx` into the tenant pricing folder (or any folder) and re-run with the new effective date:

```bash
python reconcile.py build-master \
  --tenant-folder /path/to/folder/with/new/files \
  --valid-from 2026-07-01 \
  --to-airtable          # optional: also push to Airtable
```

The script automatically closes the prior rule for each (site, product) on 2026-07-01 and inserts the new one — full audit trail preserved in both CSV and Airtable.

### Master CSV schema

| column | meaning |
| --- | --- |
| `site_id` | 3-digit FB Taverns site id (e.g. `809`) |
| `product_code` | LWC product code (matches the sales file) |
| `product_desc` | human-readable name |
| `tenant_price` | per-unit £ tenant should pay |
| `fb_price` | per-unit £ FB Taverns should pay (Net price = list − retro). Blank if unknown. |
| `retro_pct` | retro as fraction of list price (0.10 = 10%) |
| `valid_from` | YYYY-MM-DD inclusive |
| `valid_to` | YYYY-MM-DD exclusive; blank = currently active |
| `status` | `tenanted` / `managed` / `supported` |
| `reason` | free text (e.g. "6-week Red Lion support") |
| `source` | which file the rule came from |

`sites.csv` has `site_id, name, status, notes`. Edit `status` to `managed` for any site that should have no margin — the reconciler will then flag any line where the tenant price ≠ FB price.

---

## 2. Reconcile a weekly LWC file

```bash
# Local CSV mode (Phase 1):
python reconcile.py reconcile path/to/sales.xlsx

# Airtable mode (Phase 2):
python reconcile.py reconcile path/to/sales.xlsx --use-airtable
```

Defaults: `--master master_pricing.csv`, `--sites sites.csv`, `--tolerance 0.01` (per-unit £).
The CSV mismatch report is always written to `outputs/<sales_filename>__mismatches.csv`. With `--use-airtable`, mismatches are also inserted into the **Mismatches** table with linked records to **Files**, **Sites**, **Products**, and **PricingRules**.

### Mismatch types

| type | meaning |
| --- | --- |
| `wrong_tenant_price` | LWC's `UNIT` ≠ master `tenant_price` (delta > tolerance) |
| `wrong_fb_price` | LWC's `MASTER` ≠ master `fb_price` (delta > tolerance) |
| `site_should_be_managed` | `sites.csv` says managed but the line is charged with a margin |
| `unknown_site` | `SITE ID` doesn't exist in the master at all |
| `no_rule_for_line` | Site is known but no rule for this product on this date — master needs updating |
| `lwc_arithmetic_error` | LWC's own `DIFF. MASTER` ≠ `(UNIT − MASTER) × QTY` (sanity check) |

Severity bands on `delta_total`: `low` < £0.05, `medium` < £0.50, `high` ≥ £0.50.

### Report columns

`type, severity, site_id, site_name, product_code, product_desc, invoice_no, invoice_date, qty, expected_tenant_price, actual_tenant_price, expected_fb_price, actual_fb_price, delta_per_unit, delta_total, rule_valid_from, rule_valid_to, notes`

---

## What's covered vs deferred

**Covered in Phase 1**
- LWC weekly sales (latest format with `FB_Taverns_Del_Date` sheet).
- Effective-dated pricing rules with append-only updates.
- Tenant price + FB price + LWC arithmetic checks.
- Managed-site detection.

**Deferred to later phases (per the brief)**
- Older LWC sales format (`Diff. From Master` layout, May–Jun 2025 files) — needs its own column mapping. The script will exit with a clear message if you try to reconcile one.
- ~~Tennents Direct (Scottish sites) parser — Phase 4.~~ **Done (web app only):** the
  `FB_Taverns_Tennents_Master.xlsx` workbook is the primary Tennents price file
  (`tennents_master.py` + `tennents.py`, routes `/tennents*`); see CLAUDE.md §5c/§6.
- Retro reconciliation against the monthly retro file — Phase 5. Retro % is already loaded into the master, so adding the monthly pass is straightforward.
- LLM fallback parsing for unknown formats / fuzzy product matching — Phase 6.
- Email ingestion + cloud deployment — Phases 2–3.

**Known data caveats from this run**
- The FB cost price file is flagged "needs revising" — many `wrong_fb_price` entries are likely stale-master noise rather than real overcharges. Confirm a handful by hand before treating the totals as actionable.
- Per-tenant pricing files contain the post-RPI `Final New Price`; if that uplift hasn't actually taken effect yet, set `--valid-from` to the future date when it will, and add the prior `Current Price` as a separate run with an earlier `--valid-from`.
- A few historical tenant files (SANCO, WILLINGTON) don't have a parseable site_id in the `Name` column and are silently skipped — fix the `Name` cell in the source file or extend `_parse_site_from_name`.

"""
Create the Tennents master-workbook tables in Airtable.

The FB_Taverns_Tennents_Master.xlsx workbook is the primary Tennents price
file (2026-07-14); its three data sheets get one table each:

  TennentsSkuMaster        <- SKU_Master (estate-wide per-SKU rates)
  TennentsSiteMaster       <- Site_Master (sites, operating model, construct)
  TennentsSiteSkuExceptions<- Site_SKU_Exceptions (per-(site, SKU) overrides)

Also adds the monthly-volume fields to Files (period_month, barrels_total,
tlager_barrels) used for barrelage-vs-commitment tracking on /tennents.

Idempotent, same style as setup_airtable.py: existing tables/fields are left
alone. Table ids are merged into airtable_schema.json. The old
TennentsAgreements table is NOT touched (kept as a historical record).

Run once, with AIRTABLE_TOKEN + AIRTABLE_BASE_ID in .env:
  python setup_tennents_tables.py
Then load the workbook:
  python load_tennents_master.py "path\\to\\FB_Taverns_Tennents_Master.xlsx"
"""

from __future__ import annotations

import json

from setup_airtable import (
    SCHEMA_OUT,
    f_checkbox,
    f_currency,
    f_long,
    f_number,
    f_select,
    f_text,
    find_table,
    list_tables,
    upsert_table,
    add_missing_fields,
)


def sku_master_spec():
    return {
        "name": "TennentsSkuMaster",
        "description": "Estate-wide Tennents SKU rates from the master workbook's SKU_Master sheet. Replaced wholesale on each master upload.",
        "fields": [
            f_text("sku_code"),
            f_text("alt_code"),
            f_text("brand"),
            f_text("product"),
            f_text("container"),
            f_number("brl_per_unit", precision=4),
            f_number("abv", precision=1),
            f_currency("wsp_per_brl"),
            f_currency("contract_base_per_brl"),
            f_checkbox("on_contract"),
            f_select("supplier_type", ["C&C", "3rd party"]),
            f_currency("hold_per_brl"),
            f_currency("correct_total_per_brl"),
            f_long("source"),
            f_long("notes"),
            f_text("version"),
            f_text("source_file"),
        ],
    }


def site_master_spec():
    return {
        "name": "TennentsSiteMaster",
        "description": "Tennents estate sites from the master workbook's Site_Master sheet (account, operating model, discount construct).",
        "fields": [
            f_text("account"),
            f_text("site_name"),
            f_text("operating_model"),
            f_long("discount_construct"),
            f_long("notes"),
            f_text("version"),
            f_text("source_file"),
        ],
    }


def exceptions_spec():
    return {
        "name": "TennentsSiteSkuExceptions",
        "description": "Per-(site, SKU) rate exceptions from the master workbook. The Loaded value is expected-current until resolved (README §4).",
        "fields": [
            f_text("exception_key"),
            f_text("site_name"),
            f_text("account"),
            f_text("sku_code"),  # raw, may be compound "400751/400557"
            f_text("product"),
            f_currency("loaded_total_per_brl"),
            f_currency("correct_total_per_brl"),
            f_text("direction"),
            f_currency("impact_gbp"),
            f_long("status"),
            f_checkbox("resolved"),
            f_text("version"),
            f_text("source_file"),
        ],
    }


def files_extra_fields_spec():
    return {
        "name": "Files",
        "fields": [
            f_text("period_month"),                    # 'YYYY-MM' from the report's Month column
            f_number("barrels_total", precision=2),
            f_number("tlager_barrels", precision=2),
        ],
    }


def main() -> int:
    tables = list_tables()

    sku_t = upsert_table(sku_master_spec(), tables)
    site_t = upsert_table(site_master_spec(), tables)
    exc_t = upsert_table(exceptions_spec(), tables)

    files_t = find_table("Files", tables)
    if files_t is None:
        raise SystemExit("Files table not found — run setup_airtable.py first.")
    added = add_missing_fields(files_t, files_extra_fields_spec())
    print(f"  table Files          exists ({files_t['id']}); +{added} fields")

    schema = json.loads(SCHEMA_OUT.read_text())
    schema["tables"]["TennentsSkuMaster"] = sku_t["id"]
    schema["tables"]["TennentsSiteMaster"] = site_t["id"]
    schema["tables"]["TennentsSiteSkuExceptions"] = exc_t["id"]
    SCHEMA_OUT.write_text(json.dumps(schema, indent=2))
    print(f"\nMerged table ids into {SCHEMA_OUT}")
    print("Next: python load_tennents_master.py <FB_Taverns_Tennents_Master.xlsx>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

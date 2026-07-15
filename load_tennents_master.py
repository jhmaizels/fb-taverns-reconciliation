"""
Load (or reload) the Tennents master workbook into Airtable from the CLI.

Same operation as POST /upload-tennents-master: parses
FB_Taverns_Tennents_Master.xlsx and wipes + recreates the three master
tables. Used for the initial load and as a recovery path.

  python load_tennents_master.py "C:\\path\\to\\FB_Taverns_Tennents_Master.xlsx"

Requires AIRTABLE_TOKEN + AIRTABLE_BASE_ID in .env and the tables from
setup_tennents_tables.py.
"""

from __future__ import annotations

import os
import sys

from tennents_master import parse_master_workbook


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = argv[1]
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return 2

    master = parse_master_workbook(path, source_name=os.path.basename(path))
    open_exceptions = sum(1 for ex in master.exceptions if not ex.resolved)
    no_rate = [s.sku_code for s in master.skus if s.correct_total_per_brl is None]
    print(f"Parsed {os.path.basename(path)}")
    print(f"  version    : {master.version or '—'}")
    print(f"  SKUs       : {len(master.skus)} ({len(no_rate)} with no agreed rate: {', '.join(no_rate) or '—'})")
    print(f"  sites      : {len(master.sites)} "
          f"({sum(1 for s in master.sites if s.is_managed)} managed, "
          f"{sum(1 for s in master.sites if s.is_bespoke)} bespoke construct)")
    print(f"  exceptions : {len(master.exceptions)} ({open_exceptions} open)")
    arith = master.arithmetic_errors()
    if arith:
        print(f"  WARNING    : {len(arith)} SKU rows where base + hold ≠ CURRENT CORRECT:")
        for s in arith:
            print(f"    {s.sku_code} {s.product}: {s.implied_total:.2f} vs {s.correct_total_per_brl:.2f}")

    from airtable_io import replace_tennents_master  # import after parse — needs env
    deleted, created = replace_tennents_master(master, source=os.path.basename(path))
    print(f"\nAirtable updated: {deleted} rows deleted, {created} created.")
    print("This workbook is now the primary Tennents price file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

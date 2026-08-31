"""Regression tests for LWC workbook sheet detection + parse errors.

The 2026-08-31 incident: LWC re-saved the weekly sales report with the data
tab named "scratch120151"; _find_line_sheet found no name match and called
sys.exit(), and the SystemExit escaped the /upload route's `except Exception`
handler and killed the uvicorn worker (Render "Exited with status 1").

Pins down:
 1. a scratch-named data tab is found by its header row (column fallback);
 2. a workbook with no line sheet raises LwcParseError — a catchable
    Exception, never SystemExit;
 3. name-based detection still wins (regression);
 4. a name-matched sheet missing required columns raises LwcParseError.

Standalone like the other test files (pytest optional):
    python test_parse_lwc_sheet_detection.py
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from reconcile import LwcParseError, parse_lwc_sales

HEADERS = [
    "DEPOT", "ACCOUNT\nNO", "SITE ID", "ACCOUNT", "PRODUCT\nCODE",
    "PRODUCT\nDESC", "INVOICE\nNO", "DATE", "QTY", "B'BRLS", "SALES",
    "UNIT", "MASTER", "DIFF. MASTER", "DIFF. + VAT",
]

ROW = [
    "BIRM", "123456", "801", "The Bell", "ABC1", "Test Bitter 9g",
    "INV001", datetime(2026, 8, 25), 2, 0.5, 200.0, 100.0, 95.0, 10.0, 12.0,
]


def _write(path: Path, sheets: list[tuple[str, list[list]]]) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)


def main() -> int:
    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)

        # 1. Scratch-named data tab behind two pivots — found via header fallback.
        p = td / "scratch_tab.xlsx"
        _write(p, [
            ("Site ID Pivot", [["Row Labels", "Sum of DIFF. + VAT"], ["801", 10.0]]),
            ("scratch120151", [HEADERS, ROW]),
        ])
        lines = parse_lwc_sales(str(p))
        check("scratch-named tab parsed via column fallback", len(lines) == 1)
        check(
            "row values survive fallback parse",
            bool(lines) and lines[0].site_id == "801" and lines[0].unit_price == 100.0,
        )

        # 2. No line sheet anywhere — LwcParseError, never SystemExit.
        p = td / "no_line_sheet.xlsx"
        _write(p, [("Site ID Pivot", [["Row Labels", "Sum of DIFF. + VAT"]])])
        try:
            parse_lwc_sales(str(p))
            check("missing line sheet raises", False)
        except LwcParseError as e:
            check("missing line sheet raises LwcParseError", "Sheets present" in str(e))
        except SystemExit:
            check("missing line sheet must not raise SystemExit", False)

        # 3. Canonical sheet name still wins.
        p = td / "named.xlsx"
        _write(p, [("FB_Taverns_Del_Date", [HEADERS, ROW])])
        check("canonical FB_Taverns_Del_Date still parsed", len(parse_lwc_sales(str(p))) == 1)

        # 4. Name-matched sheet missing required columns — LwcParseError.
        p = td / "missing_cols.xlsx"
        _write(p, [("Del_Date", [["SITE ID", "PRODUCT CODE", "DATE"], ["801", "ABC1", datetime(2026, 8, 25)]])])
        try:
            parse_lwc_sales(str(p))
            check("missing columns raises", False)
        except LwcParseError as e:
            check("missing columns raises LwcParseError", "missing required columns" in str(e))
        except SystemExit:
            check("missing columns must not raise SystemExit", False)

    print(f"\n{'OK' if not failures else 'FAILED'}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

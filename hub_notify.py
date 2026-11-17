"""Tell the hub which pubs took a delivery, so it can email the ones that didn't.

James, 17 Nov 2026: when the reconciliation is run for LWC (or Tennent's), the
area managers (plus Lewis and Jason) are sent the pubs with no drinks ordering
in that period.

This app knows who ordered; the HUB knows which pubs should have (status,
tenancy, country, a pub not open yet) and who covers each country, and it
already sends the staff emails. So at the end of a weekly LWC upload
(`POST /upload`) or a monthly Tennent's upload (`POST /upload-tennents`) this
module POSTs to the hub's `/api/drinks/no-orders`:

    {supplier, file_name, file_hash, period_from, period_to, period_label,
     ordered: [ids that took a delivery], price_file_sites: [{id, name}]}

and shows the hub's one-line answer on the results page. The hub decides,
sends (only when its switch is on) and never sends twice for the same file
hash, so re-uploading a file after a master correction is safe.

Auth: the bearer token the two apps already share, DEAL_PRODUCTS_TOKEN (the
hub sends it to read /api/site-prices; here it is sent back). HUB_ORIGIN
overrides the hub's address (default https://portal.fbtaverns.com).

Best effort: a hub that is down, slow or unconfigured never affects the
reconciliation; the page says the list was not sent.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import date

import requests

logger = logging.getLogger(__name__)

HUB_TIMEOUT_S = 15
DEFAULT_HUB_ORIGIN = "https://portal.fbtaverns.com"

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class HubNotice:
    """What the results page shows. `ok` False = the list did not reach the hub."""
    ok: bool
    message: str


def lwc_payload(file_name: str, file_hash: str, lines, price_file_sites: dict[str, str]) -> dict:
    """The weekly LWC file → the hub's payload.

    `ordered` is every SITE ID with a delivery (qty > 0). A line with no site
    (SITE ID 0, including stock on FB's own account) matches no hub pub, so it
    neither counts nor hurts. The period is the first and last invoice date in
    the file. `price_file_sites` is {site_id: name} for the sites on the master.
    """
    ordered = sorted({str(l.site_id).strip() for l in lines if str(l.site_id or "").strip() and (l.qty or 0) > 0})
    dates = sorted(l.invoice_date for l in lines if l.invoice_date)
    p_from = dates[0].isoformat() if dates else None
    p_to = dates[-1].isoformat() if dates else None
    return {
        "supplier": "lwc",
        "file_name": file_name,
        "file_hash": file_hash,
        "period_from": p_from,
        "period_to": p_to,
        "period_label": week_label(dates[0], dates[-1]) if dates else "",
        "ordered": ordered,
        "price_file_sites": [{"id": sid, "name": name} for sid, name in sorted(price_file_sites.items())],
    }


def tennents_payload(file_name: str, file_hash: str, report, master) -> dict:
    """The monthly Tennent's report → the hub's payload. Only checkable lines
    (kegs > 0) count as a delivery; a month of nothing but returns is not."""
    ordered = sorted({str(l.account).strip() for l in report.lines if str(l.account or "").strip() and (l.kegs or 0) > 0})
    p_from = p_to = None
    label = ""
    if report.period:
        y, m = (int(x) for x in report.period.split("-"))
        p_from = date(y, m, 1).isoformat()
        p_to = date(y, m, calendar.monthrange(y, m)[1]).isoformat()
        label = f"{calendar.month_name[m]} {y}"
    return {
        "supplier": "tennents",
        "file_name": file_name,
        "file_hash": file_hash,
        "period_from": p_from,
        "period_to": p_to,
        "period_label": label,
        "ordered": ordered,
        # An account still "TBC" on the master names no delivery point yet.
        "price_file_sites": [
            {"id": s.account, "name": s.site_name} for s in master.sites if str(s.account).strip().isdigit()
        ],
    }


def file_sha256(path: str) -> str:
    """The same hash the Files table dedupes on (airtable_io._file_hash)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def week_label(d1: date, d2: date) -> str:
    """'9–15 Nov 2026', '28 Oct – 3 Nov 2026' (the hub words it the same)."""
    if d1 == d2:
        return f"{d1.day} {_MONTHS[d1.month - 1]} {d1.year}"
    if d1.year == d2.year and d1.month == d2.month:
        return f"{d1.day}–{d2.day} {_MONTHS[d2.month - 1]} {d2.year}"
    if d1.year == d2.year:
        return f"{d1.day} {_MONTHS[d1.month - 1]} – {d2.day} {_MONTHS[d2.month - 1]} {d2.year}"
    return f"{d1.day} {_MONTHS[d1.month - 1]} {d1.year} – {d2.day} {_MONTHS[d2.month - 1]} {d2.year}"


def send_to_hub(payload: dict, *, post=None) -> HubNotice:
    """POST the payload; never raises. `post` is injectable for tests."""
    token = os.environ.get("DEAL_PRODUCTS_TOKEN", "")
    if not token:
        return HubNotice(False, "Not sent to the hub: DEAL_PRODUCTS_TOKEN is not set on the drinks app.")
    origin = (os.environ.get("HUB_ORIGIN") or DEFAULT_HUB_ORIGIN).rstrip("/")
    post = post or requests.post
    try:
        resp = post(
            f"{origin}/api/drinks/no-orders",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=HUB_TIMEOUT_S,
        )
    except Exception as e:  # network, timeout
        logger.warning("hub no-orders notify failed: %s", e)
        return HubNotice(False, "Not sent: the hub could not be reached. The pubs with no delivery are in the list above.")
    try:
        body = resp.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "") if isinstance(body, dict) else ""
    if resp.status_code == 200:
        return HubNotice(True, message or "The hub has the list.")
    logger.warning("hub no-orders notify answered %s: %s", resp.status_code, message)
    return HubNotice(False, f"The hub answered {resp.status_code}" + (f": {message}" if message else "."))


def notice_html(n: HubNotice | None) -> str:
    """One line for the results page."""
    if n is None:
        return ""
    from html import escape
    colour = "#166534" if n.ok else "#92400e"
    return (
        f"<p class='sub' style='color:{colour}'><strong>Pubs with no delivery → area managers:</strong> "
        f"{escape(n.message)}</p>"
    )

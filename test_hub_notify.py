"""The hand-off to the hub after a reconciliation (hub_notify.py, 17 Nov 2026).

The hub emails the area managers, Jason and Lewis the pubs with no delivery;
this app only says who DID take one, for which period, from which file.

    python test_hub_notify.py
"""

import os
from datetime import date
from types import SimpleNamespace as NS

import hub_notify
from reconcile import InvoiceLine


def line(site, qty, d):
    return InvoiceLine(site_id=site, site_name="", product_code="P", product_desc="", invoice_no="1",
                       invoice_date=d, qty=qty, unit_price=1.0, master_price=1.0, diff_master=0.0)


def test_lwc_payload():
    lines = [
        line("804", 2, date(2026, 11, 9)),
        line("821", 1, date(2026, 11, 15)),
        line("812", -1, date(2026, 11, 12)),   # a return alone is not an order
        line("0", 3, date(2026, 11, 10)),      # FB's own account: kept, matches no pub
        line("", 1, None),
    ]
    p = hub_notify.lwc_payload("w.xlsx", "h1", lines, {"804": "Bell", "812": "Wheatsheaf"})
    assert p["supplier"] == "lwc"
    assert p["ordered"] == ["0", "804", "821"], p["ordered"]
    assert (p["period_from"], p["period_to"], p["period_label"]) == ("2026-11-09", "2026-11-15", "9–15 Nov 2026")
    assert p["price_file_sites"] == [{"id": "804", "name": "Bell"}, {"id": "812", "name": "Wheatsheaf"}]
    assert p["file_hash"] == "h1"


def test_tennents_payload():
    report = NS(period="2026-10", lines=[NS(account="175", kegs=2), NS(account="176", kegs=0)])
    master = NS(sites=[NS(account="175", site_name="Corner House"), NS(account="TBC", site_name="New pub")])
    p = hub_notify.tennents_payload("t.xlsx", "h2", report, master)
    assert p["ordered"] == ["175"]
    assert (p["period_from"], p["period_to"], p["period_label"]) == ("2026-10-01", "2026-10-31", "October 2026")
    assert p["price_file_sites"] == [{"id": "175", "name": "Corner House"}]


def test_week_label():
    assert hub_notify.week_label(date(2026, 10, 28), date(2026, 11, 3)) == "28 Oct – 3 Nov 2026"
    assert hub_notify.week_label(date(2026, 12, 29), date(2027, 1, 4)) == "29 Dec 2026 – 4 Jan 2027"


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def test_send_to_hub():
    os.environ.pop("DEAL_PRODUCTS_TOKEN", None)
    n = hub_notify.send_to_hub({})
    assert not n.ok and "DEAL_PRODUCTS_TOKEN" in n.message

    os.environ["DEAL_PRODUCTS_TOKEN"] = "tok"
    os.environ["HUB_ORIGIN"] = "https://hub.example/"
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, auth=headers["Authorization"], timeout=timeout)
        return Resp(200, {"message": "Emailed the list of 2 pub(s) to a@b."})

    n = hub_notify.send_to_hub({"x": 1}, post=post)
    assert n.ok and n.message.startswith("Emailed"), n
    assert seen["url"] == "https://hub.example/api/drinks/no-orders"
    assert seen["auth"] == "Bearer tok"

    n = hub_notify.send_to_hub({}, post=lambda *a, **k: Resp(403, "Forbidden"))
    assert not n.ok and "403" in n.message

    def boom(*a, **k):
        raise TimeoutError("slow")

    n = hub_notify.send_to_hub({}, post=boom)
    assert not n.ok and "could not be reached" in n.message
    assert "&lt;" in hub_notify.notice_html(hub_notify.HubNotice(False, "<x>"))


if __name__ == "__main__":
    test_lwc_payload()
    test_tennents_payload()
    test_week_label()
    test_send_to_hub()
    print("ok")

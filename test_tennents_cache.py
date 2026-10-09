"""
Offline tests for the Tennents master cache (airtable_io._TtlCache) and the
write paths that patch it instead of re-sweeping Airtable:

  * fresh / stale / invalidated / published semantics of _TtlCache, with the
    background refresh stubbed so it is deterministic;
  * load_tennents_master reads the four tables ONCE and serves the cache;
  * set_tennents_site_price PATCHes by the cached row id (no table sweep) and
    the next load shows the change without another fetch;
  * add_tennents_site makes a filtered read, not a sweep;
  * get_tennents_master_info / list_bar_plan_changes come off the cache;
  * accept_tennents_sku invalidates, so the next load re-fetches.

Run standalone (exit 0 = pass, 1 = fail):

    python test_tennents_cache.py
"""
import sys
import time

import airtable_io as aio
from tennents_master import plan_add_site, plan_bar_plan_change

FAILS: list[str] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {label}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def test_ttl_cache_semantics():
    print("\n-- _TtlCache")
    c = aio._TtlCache("t", ttl=0.2)
    kicks = {"n": 0}
    c.refresh_async = lambda: kicks.__setitem__("n", kicks["n"] + 1)
    n = {"n": 0}

    def fetch():
        n["n"] += 1
        return f"V{n['n']}"

    _check("cold: fetches inline", c.get(fetch) == "V1" and n["n"] == 1)
    _check("fresh: served, no fetch", c.get(fetch) == "V1" and n["n"] == 1 and kicks["n"] == 0)
    time.sleep(0.25)
    _check("stale: served stale, refresh kicked", c.get(fetch) == "V1" and n["n"] == 1 and kicks["n"] == 1)
    c.invalidate()
    _check("invalidated: last value served stale, refresh kicked", c.get(fetch) == "V1" and n["n"] == 1 and kicks["n"] == 2)
    _check("force_refresh fetches inline", c.get(fetch, force_refresh=True) == "V2" and n["n"] == 2)
    c.publish("PATCHED")
    _check("published value is served fresh", c.get(fetch) == "PATCHED" and n["n"] == 2)
    _check("peek returns held value", c.peek() == "PATCHED")

    # A background fetch that started BEFORE a publish must not overwrite it.
    c2 = aio._TtlCache("t2", ttl=100)
    c2.get(lambda: "OLD")
    with c2._lock:
        gen_before = c2._gen
    c2.publish("NEW")
    with c2._lock:
        if c2._gen == gen_before:
            c2._value = "OLD-LATE"
    _check("stale in-flight fetch discarded after publish", c2.get(lambda: "x") == "NEW")


class FakeIO:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {aio.T[n]: [] for n in
                                              ("TennentsSkuMaster", "TennentsSiteMaster",
                                               "TennentsSiteSkuExceptions", "TennentsSitePrices", "Files")}
        self.sweeps: list[tuple[str, str]] = []   # (table, filter)
        self.patches: list[dict] = []
        self.creates: list[dict] = []
        self._n = 100

    def list_all(self, table_id, fields=None, filter_by_formula=None, **kw):
        self.sweeps.append((table_id, filter_by_formula or ""))
        rows = self.tables.setdefault(table_id, [])
        if filter_by_formula:
            # {account}='x'
            val = filter_by_formula.split("'")[1]
            rows = [r for r in rows if str(r["fields"].get("account", "")) == val]
        return [{"id": r["id"], "fields": dict(r["fields"]), "createdTime": r.get("createdTime", "")} for r in rows]

    def batch(self, records, op, table_id):
        rows = self.tables.setdefault(table_id, [])
        out = []
        for rec in records:
            if op == "update":
                self.patches.append(rec)
                for r in rows:
                    if r["id"] == rec["id"]:
                        r["fields"].update(rec["fields"])
                        out.append(r)
            else:
                self._n += 1
                r = {"id": f"rec{self._n}", "fields": dict(rec["fields"])}
                rows.append(r)
                self.creates.append(r)
                out.append(r)
        return out

    def wipe(self, table_id):
        self.tables[table_id] = []


def _install(fake: FakeIO):
    aio._list_all = fake.list_all
    aio._batch = fake.batch
    aio._wipe_table = fake.wipe
    aio.TENNENTS_CACHE = aio._TtlCache("tennents-master", 100)
    aio.TENNENTS_CACHE.refresh_async = lambda: None   # deterministic: no threads
    aio.TENNENTS_VOLUMES_CACHE = aio._TtlCache("tennents-volumes", 100)


def _seed(fake: FakeIO):
    fake.tables[aio.T["TennentsSkuMaster"]] = [
        {"id": "recS1", "createdTime": "2026-10-01T10:00:00.000Z", "fields": {
            "sku_code": "090425", "alt_code": "09000X", "product": "Tennents Lager", "container": "11G",
            "wsp_per_brl": 450.0, "correct_total_per_brl": 300.0, "source_file": "FB_Taverns_Tennents_Master.xlsx",
            "version": "v3"}},
        {"id": "recS2", "createdTime": "2026-10-01T10:00:00.000Z", "fields": {
            "sku_code": "401211", "product": "Bavarian", "container": "11G", "wsp_per_brl": 460.0,
            "correct_total_per_brl": 250.0, "source_file": "FB_Taverns_Tennents_Master.xlsx", "version": "v3"}},
        {"id": "recS3", "fields": {"sku_code": "999999", "product": "Rate TBC", "version": "v3"}},
    ]
    fake.tables[aio.T["TennentsSiteMaster"]] = [
        {"id": "recT1", "fields": {"account": "11110001", "site_name": "BELL ROCK", "operating_model": "Tenanted", "version": "v3"}},
    ]
    fake.tables[aio.T["TennentsSitePrices"]] = [
        {"id": "recP1", "fields": {"price_key": "11110001|090425", "account": "11110001", "site_name": "BELL ROCK",
                                   "sku_code": "090425", "off_invoice_per_brl": 100.0, "notes": "seeded",
                                   "source_file": "FB_Taverns_Tennents_Master.xlsx"}},
    ]


def test_master_cached_and_patched():
    print("\n-- load_tennents_master is cached; writes patch, not sweep")
    fake = FakeIO()
    _seed(fake)
    _install(fake)

    m = aio.load_tennents_master()
    first = len(fake.sweeps)
    _check("four tables read on the first load", first == 4, str(fake.sweeps))
    _check("row ids captured", m.site_prices[0].rec_id == "recP1" and m.skus[0].rec_id == "recS1")
    _check("provenance captured", m.skus[0].source_file.endswith(".xlsx") and m.skus[0].created_at.startswith("2026-10-01"))
    aio.load_tennents_master()
    _check("second load served from the cache", len(fake.sweeps) == first)

    info = aio.get_tennents_master_info()
    _check("banner info off the cache", len(fake.sweeps) == first and info["sku_count"] == 3
           and info["no_rate_count"] == 1 and info["site_count"] == 1
           and info["source_file"].endswith(".xlsx") and info["latest_uploaded_at"].startswith("2026-10-01"), str(info))

    # Update an existing site price: PATCH by id, no read at all.
    r = aio.set_tennents_site_price(m, plan_bar_plan_change(m, "11110001", "09000X", "118"), "james@x", "Nick")
    _check("updated by cached row id", r["action"] == "updated" and fake.patches[-1]["id"] == "recP1")
    _check("no Airtable read for the update", len(fake.sweeps) == first, str(fake.sweeps[first:]))
    m2 = aio.load_tennents_master()
    _check("next load shows the change without a fetch",
           len(fake.sweeps) == first and m2.off_invoice("11110001", "090425") == 118.0)
    _check("listed as a bar plan change off the cache", len(aio.list_bar_plan_changes()) == 1 and len(fake.sweeps) == first)

    # Create a new site price: one CREATE, no sweep (a filtered read at most).
    r = aio.set_tennents_site_price(m2, plan_bar_plan_change(m2, "11110001", "401211", "173"), "james@x")
    reads = fake.sweeps[first:]
    _check("created", r["action"] == "created" and fake.creates[-1]["fields"]["sku_code"] == "401211")
    _check("create used a filtered read, never a sweep", all(f for _, f in reads), str(reads))
    m3 = aio.load_tennents_master()
    _check("new row visible with its id", (sp := m3.site_price("11110001", "401211")) is not None
           and sp.rec_id == fake.creates[-1]["id"] and sp.off_invoice_per_brl == 173.0)
    _check("two bar plan changes listed", len(aio.list_bar_plan_changes()) == 2)

    # Add a pub: filtered read, then the cache carries it.
    before = len(fake.sweeps)
    site = plan_add_site(m3, "17599415", "Mallroad House")
    aio.add_tennents_site(m3, site, "james@x")
    reads = fake.sweeps[before:]
    _check("add pub: filtered read only", len(reads) == 1 and "17599415" in reads[0][1], str(reads))
    m4 = aio.load_tennents_master()
    _check("pub on the cached master", m4.site_for_account("17599415") is not None
           and aio.get_tennents_master_info()["site_count"] == 2)
    try:
        aio.add_tennents_site(m4, plan_add_site(m3, "17599415", "Mallroad House"), "james@x")
        _check("duplicate pub refused", False)
    except ValueError as e:
        _check("duplicate pub refused", "already on the master" in str(e))

    # A SKU write invalidates: the next load re-fetches.
    aio.accept_tennents_sku("set_rate", "999999", "james@x", "oct.xlsx", charged_total=280.0)
    _check("accept_tennents_sku invalidates the cache", aio.TENNENTS_CACHE._value is None)
    stale = aio.load_tennents_master()
    _check("the last master is served stale meanwhile (never an inline sweep)",
           stale is m4 and stale.find_sku("999999").correct_total_per_brl is None)
    m5 = aio.load_tennents_master(force_refresh=True)
    _check("re-fetched master carries the rate and the bar plan rows",
           m5.find_sku("999999").correct_total_per_brl == 280.0 and m5.off_invoice("11110001", "090425") == 118.0
           and m5.site_for_account("17599415") is not None)


def test_volumes_cached():
    print("\n-- monthly volumes cached, invalidated by a Files write")
    fake = FakeIO()
    _install(fake)
    fake.tables[aio.T["Files"]] = [
        {"id": "recF1", "fields": {"file_name": "sep.xlsx", "supplier": "Tennents", "period_month": "2026-09",
                                   "barrels_total": 120.5, "tlager_barrels": 80.0, "received_at": "2026-10-01T00:00:00Z"}},
    ]
    v1 = aio.list_tennents_monthly_volumes()
    n = len(fake.sweeps)
    aio.list_tennents_monthly_volumes()
    _check("served from the cache", len(fake.sweeps) == n and len(v1) == 1)
    aio.TENNENTS_VOLUMES_CACHE.invalidate()
    aio.TENNENTS_VOLUMES_CACHE.refresh_async = lambda: None
    aio.list_tennents_monthly_volumes()
    _check("stale served after invalidate (no inline fetch)", len(fake.sweeps) == n)


if __name__ == "__main__":
    test_ttl_cache_semantics()
    test_master_cached_and_patched()
    test_volumes_cached()
    print("\nALL PASS" if not FAILS else "\nFAILURES: " + ", ".join(FAILS))
    sys.exit(1 if FAILS else 0)

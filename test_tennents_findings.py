"""
Offline tests for the Tennents findings-page actions (the LWC-parity port):
multi-alt SKU codes, the "link to existing SKU" suggestion, the accept
primitive (link / new / set_rate), preserve-on-replace, and the rendered
buttons + draft-email config.

Run standalone:  python test_tennents_findings.py
"""
from __future__ import annotations

import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import airtable_io as aio  # noqa: E402
import tennents as tn  # noqa: E402
from tennents_master import SiteInfo, SkuRate, TennentsMaster, sku_codes, suggest_sku  # noqa: E402

PASS = True


def _check(label, cond, detail=""):
    global PASS
    PASS &= bool(cond)
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")


def sku(code, alt, brand, product, total=100.0, wsp=600.0):
    return SkuRate(code, alt, brand, product, "50L", 0.3055, 4.0, wsp, total, True, "C&C", 0.0, total)


def make_master() -> TennentsMaster:
    skus = [
        sku("401136", "400217/401187", "Heverlee", "Heverlee 4.4% 50L / 30L Keg", 370.14),
        sku("401175", "", "Blackthorn", "Blackthorn Dry 50L Keg", 293.49),
        sku("090425", "09000X", "Tennent's Lager", "T.Lager 11G / 22G Keg", 314.33),
        SkuRate("401211", "", "Tennent's", "T.Bavarian 50L", "50L", 0.3055, 4.7, None, None, False, "C&C", 0.0, None),
    ]
    sites = [SiteInfo("11110001", "STANDARD ARMS", "Tenanted (TBC)", "Standard split")]
    return TennentsMaster("vTEST", "fixture", skus, sites, [], [])


# ---------- multi-alt codes + suggestion ----------

def test_codes_and_suggest():
    print("\n-- multi-alt codes + suggest_sku")
    m = make_master()
    _check("sku_codes splits '/'-joined alts", sku_codes(m.skus[0]) == ["401136", "400217", "401187"], str(sku_codes(m.skus[0])))
    _check("second alt code resolves", m.find_sku("401187") is not None and m.find_sku("401187").sku_code == "401136")
    _check("canonical via second alt", m.canonical_sku("401187") == "401136")
    _check("leading-zero drift still tolerated", m.find_sku("90425") is not None)
    s = suggest_sku(m, "Blackthorn Dry 5% 50L Keg")
    _check("suggest: Blackthorn Dry new code -> 401175", s is not None and s.sku_code == "401175", str(s and s.sku_code))
    s = suggest_sku(m, "Heverlee 4.4% 30L Keg")
    _check("suggest: Heverlee 30L -> 401136", s is not None and s.sku_code == "401136", str(s and s.sku_code))
    _check("suggest: sizes/ABV alone match nothing", suggest_sku(m, "4.4% 30L Keg") is None)
    _check("suggest: unknown brand -> None", suggest_sku(m, "Peroni Nastro 50L") is None)


# ---------- fake Airtable for the write paths ----------

class FakeIO:
    """Stands in for airtable_io._list_all / _batch / _wipe_table, keyed by
    table_id — replace_tennents_master wipes FOUR tables, so only the SKU table
    is populated here and the others must start empty and stay separate."""

    def __init__(self, sku_rows: list[dict]):
        self.tables: dict[str, list[dict]] = {
            aio.T["TennentsSkuMaster"]: [{"id": r["id"], "fields": dict(r["fields"])} for r in sku_rows]
        }
        self.patches: list[dict] = []
        self.creates: list[dict] = []
        self._n = 100

    def _rows(self, table_id):
        return self.tables.setdefault(table_id, [])

    @property
    def rows(self):   # the SKU table, for assertions
        return self._rows(aio.T["TennentsSkuMaster"])

    def list_all(self, table_id, fields=None, **kw):
        out = []
        for r in self._rows(table_id):
            f = r["fields"] if fields is None else {k: v for k, v in r["fields"].items() if k in fields}
            out.append({"id": r["id"], "fields": f})
        return out

    def batch(self, records, op, table_id):
        rows = self._rows(table_id)
        if op == "create":
            made = []
            for rec in records:
                self._n += 1
                row = {"id": f"rec{self._n}", "fields": dict(rec["fields"])}
                rows.append(row)
                made.append(row)
                self.creates.append(row)
            return made
        for rec in records:
            self.patches.append(rec)
            for row in rows:
                if row["id"] == rec["id"]:
                    row["fields"].update(rec["fields"])
        return [{"id": r["id"], "fields": r["fields"]} for r in records]

    def wipe(self, table_id, key_field):
        rows = self._rows(table_id)
        n = len(rows)
        rows.clear()
        return n


def _install(fake: FakeIO):
    aio._list_all = fake.list_all
    aio._batch = fake.batch
    aio._wipe_table = fake.wipe


def test_accept_primitive():
    print("\n-- accept_tennents_sku")
    base = [
        {"id": "recA", "fields": {"sku_code": "401175", "alt_code": "", "correct_total_per_brl": 293.49, "source": "David"}},
        {"id": "recB", "fields": {"sku_code": "401211", "alt_code": "", "correct_total_per_brl": None, "source": "x"}},
        {"id": "recC", "fields": {"sku_code": "401136", "alt_code": "400217", "correct_total_per_brl": 370.14, "source": "x"}},
    ]
    fake = FakeIO(base)
    _install(fake)

    r = aio.accept_tennents_sku("link", "401220", "james@x", "Aug26.xlsx", link_to="401175", sku_desc="Blackthorn Dry 5% 50L Keg")
    _check("link: appends alt code", r["action"] == "linked" and r["alt_code"] == "401220", str(r))
    _check("link: stamped findings source", fake.patches[-1]["fields"]["source"].startswith(aio.FINDINGS_SOURCE_PREFIX))
    r = aio.accept_tennents_sku("link", "401220", "james@x", "Aug26.xlsx", link_to="401175")
    _check("link: idempotent", r["action"] == "already", str(r))
    r = aio.accept_tennents_sku("link", "401187", "james@x", "Aug26.xlsx", link_to="401136")
    _check("link: unions with an existing alt", r["alt_code"] == "400217/401187", str(r))
    try:
        aio.accept_tennents_sku("link", "401211", "james@x", "f", link_to="401175")
        _check("link: refuses to hijack another SKU's primary", False)
    except ValueError:
        _check("link: refuses to hijack another SKU's primary", True)
    try:
        aio.accept_tennents_sku("link", "401220", "james@x", "f", link_to="NOPE")
        _check("link: unknown target rejected", False)
    except ValueError:
        _check("link: unknown target rejected", True)

    r = aio.accept_tennents_sku("set_rate", "401211", "james@x", "Aug26.xlsx", charged_total="349.99")
    _check("set_rate: fills a TBC rate", r["action"] == "rate_set" and abs(r["correct_total_per_brl"] - 349.99) < 1e-9, str(r))
    _check("set_rate: base set too", fake.patches[-1]["fields"].get("contract_base_per_brl") == 349.99)
    r = aio.accept_tennents_sku("set_rate", "401211", "james@x", "Aug26.xlsx", charged_total="349.99")
    _check("set_rate: idempotent", r["action"] == "already", str(r))
    for bad in ("0", "-5", "abc", None):
        try:
            aio.accept_tennents_sku("set_rate", "401211", "james@x", "f", charged_total=bad)
            _check(f"set_rate: rejects charged {bad!r}", False)
        except ValueError:
            _check(f"set_rate: rejects charged {bad!r}", True)

    n_before = len(fake.rows)
    r = aio.accept_tennents_sku("new", "T00099999", "james@x", "Aug26.xlsx", sku_desc="Peroni 50L Keg", charged_total="150", container="50L")
    _check("new: creates a row at the charged rate", r["action"] == "created" and len(fake.rows) == n_before + 1, str(r))
    made = fake.creates[-1]["fields"]
    _check("new: no WSP invented", "wsp_per_brl" not in made)
    _check("new: container carried", made.get("container") == "50L")
    _check("new: findings-stamped", made["source"].startswith(aio.FINDINGS_SOURCE_PREFIX))
    try:
        aio.accept_tennents_sku("new", "T00099998", "james@x", "f", sku_desc="x", charged_total="0")
        _check("new: refuses £0 charged (would bless £0)", False)
    except ValueError:
        _check("new: refuses £0 charged (would bless £0)", True)
    r = aio.accept_tennents_sku("new", "401211", "james@x", "f", charged_total="349.99")
    _check("new on an existing SKU behaves as set_rate", r["action"] in ("already", "rate_set"), str(r))
    try:
        aio.accept_tennents_sku("bogus", "X", "james@x", "f")
        _check("unknown mode rejected", False)
    except ValueError:
        _check("unknown mode rejected", True)

    # --- review fixes: never overwrite an AGREED rate; hold-aware base; alt-code guards ---
    fake = FakeIO([
        {"id": "rA", "fields": {"sku_code": "AGREED", "alt_code": "", "correct_total_per_brl": 100.0, "source": "wb"}},
        {"id": "rH", "fields": {"sku_code": "HOLDY", "alt_code": "", "correct_total_per_brl": None, "hold_per_brl": 10.0, "source": "wb"}},
        {"id": "rP", "fields": {"sku_code": "P1", "alt_code": "ALT1/ALT2", "correct_total_per_brl": 50.0, "source": "wb"}},
        {"id": "rQ", "fields": {"sku_code": "P2", "alt_code": "", "correct_total_per_brl": 60.0, "source": "wb"}},
    ])
    _install(fake)
    for bad_mode, kw in (("set_rate", {}), ("new", {"sku_desc": "x"})):
        try:
            aio.accept_tennents_sku(bad_mode, "AGREED", "james@x", "f", charged_total="120", **kw)
            _check(f"{bad_mode}: refuses to overwrite an existing agreed rate", False)
        except ValueError as e:
            _check(f"{bad_mode}: refuses to overwrite an existing agreed rate", "already has an agreed rate" in str(e), str(e))
    r = aio.accept_tennents_sku("set_rate", "AGREED", "james@x", "f", charged_total="100")
    _check("set_rate: same figure on an agreed rate is a no-op", r["action"] == "already")
    r = aio.accept_tennents_sku("set_rate", "HOLDY", "james@x", "f", charged_total="349.99")
    base = fake.patches[-1]["fields"].get("contract_base_per_brl")
    _check("set_rate: contract base nets out the hold (base + hold == total)", abs(base - 339.99) < 1e-9, str(base))
    for mode, kw in (("link", {"link_to": "P2"}), ("new", {"sku_desc": "x", "charged_total": "70"})):
        try:
            aio.accept_tennents_sku(mode, "ALT2", "james@x", "f", **kw)
            _check(f"{mode}: refuses a code already held as another SKU's alt", False)
        except ValueError as e:
            _check(f"{mode}: refuses a code already held as another SKU's alt", "alt code of SKU P1" in str(e), str(e))
    # leading-zero drift: '90425' is the master's '090425', not a new SKU
    fake = FakeIO([{"id": "rZ", "fields": {"sku_code": "090425", "alt_code": "", "correct_total_per_brl": None, "source": "wb"}}])
    _install(fake)
    r = aio.accept_tennents_sku("new", "90425", "james@x", "f", sku_desc="T.Lager", charged_total="314.33")
    _check("new: drifted code fills the existing SKU's TBC rate instead of creating a duplicate",
           r["action"] == "rate_set" and r["sku_code"] == "090425" and len(fake.rows) == 1, str(r))
    r = aio.accept_tennents_sku("link", "90425", "james@x", "f", link_to="090425")
    _check("link: drifted code onto its own SKU is a no-op", r["action"] == "already", str(r))
    # compound / malformed codes are refused — a '/'-joined value is split on the next
    # load, so 'NEWC/ALT2' would smuggle P1's ALT2 onto P2 past the ownership guard
    fake = FakeIO([{"id": "rP", "fields": {"sku_code": "P1", "alt_code": "ALT1/ALT2", "correct_total_per_brl": 50.0, "source": "wb"}},
                   {"id": "rQ", "fields": {"sku_code": "P2", "alt_code": "", "correct_total_per_brl": 60.0, "source": "wb"}}])
    _install(fake)
    for bad in ("NEWC/ALT2", "NEWD\\90425"):
        for mode, kw in (("link", {"link_to": "P2"}), ("new", {"sku_desc": "x", "charged_total": "70"}),
                         ("set_rate", {"charged_total": "70"})):
            try:
                aio.accept_tennents_sku(mode, bad, "james@x", "f", **kw)
                _check(f"{mode}: refuses compound code {bad!r}", False)
            except ValueError as e:
                _check(f"{mode}: refuses compound code {bad!r}", "compound" in str(e), str(e))
    _check("compound refusal wrote nothing", not fake.patches and not fake.creates)
    # zero-width / soft-hyphen / fullwidth characters would mint a visually-identical duplicate row
    for bad in ("ALT2​", "NEW­C", "０９０４２５", "A|B", "A;B"):
        try:
            aio.accept_tennents_sku("new", bad, "james@x", "f", sku_desc="x", charged_total="70")
            _check(f"new: refuses non-ASCII / punctuation code {bad!r}", False)
        except ValueError:
            _check(f"new: refuses non-ASCII / punctuation code {bad!r}", True)
    _check("plain codes still accepted (T00045238-style)",
           aio.accept_tennents_sku("new", "T00099997", "james@x", "f", sku_desc="ok", charged_total="70")["action"] == "created")
    # set_rate: a charged total below the row's Mar-26 hold would give a negative base -> refused
    fake = FakeIO([{"id": "rH2", "fields": {"sku_code": "HOLDY2", "alt_code": "", "correct_total_per_brl": None,
                                             "hold_per_brl": 12.33, "source": "wb"}}])
    _install(fake)
    try:
        aio.accept_tennents_sku("set_rate", "HOLDY2", "james@x", "f", charged_total="5")
        _check("set_rate: refuses a charged total below the hold (negative base)", False)
    except ValueError as e:
        _check("set_rate: refuses a charged total below the hold (negative base)", "below" in str(e) and not fake.patches, str(e))


def test_preserve_on_replace():
    print("\n-- replace_tennents_master preserves findings rows")
    # Airtable has: a findings-added SKU the workbook lacks; a workbook SKU whose
    # findings-linked alt code the workbook lacks; a plain workbook row.
    live = [
        {"id": "r1", "fields": {"sku_code": "ZZZ999", "product": "New thing", "correct_total_per_brl": 150.0,
                                "contract_base_per_brl": 150.0, "source": "findings:james Aug26.xlsx", "version": "old", "source_file": "old"}},
        {"id": "r2", "fields": {"sku_code": "401175", "alt_code": "401220", "correct_total_per_brl": 293.49,
                                "source": "findings:james Aug26.xlsx", "version": "old", "source_file": "old"}},
        {"id": "r3", "fields": {"sku_code": "090425", "alt_code": "09000X", "correct_total_per_brl": 314.33, "source": "wb"}},
    ]
    fake = FakeIO(live)
    _install(fake)
    wb = TennentsMaster("v2", "wb", [
        sku("401175", "", "Blackthorn", "Blackthorn Dry 50L Keg", 293.49),   # workbook row: no alt
        sku("090425", "09000X", "Tennent's Lager", "T.Lager", 314.33),
    ], [], [], [])
    deleted, created, preserved = aio.replace_tennents_master(wb, source="wb_v2.xlsx")
    _check("returns (deleted, created, preserved)", deleted == 3 and created == 3 and preserved == 2, f"{deleted},{created},{preserved}")
    by = {r["fields"]["sku_code"]: r["fields"] for r in fake.rows}
    _check("findings-only SKU re-created", "ZZZ999" in by and by["ZZZ999"].get("correct_total_per_brl") == 150.0)
    _check("re-created row takes the new version", by["ZZZ999"].get("version") == "v2")
    _check("linked alt code unioned back onto the workbook row", by["401175"].get("alt_code") == "401220", str(by["401175"].get("alt_code")))
    _check("findings stamp kept (survives NEXT re-upload)", str(by["401175"].get("source", "")).startswith("findings:"))
    _check("plain workbook row untouched", by["090425"].get("alt_code") == "09000X" and by["090425"].get("source") is None)

    # A workbook that DOES carry the alt itself: nothing to patch, not counted.
    fake2 = FakeIO([live[1]])
    _install(fake2)
    wb2 = TennentsMaster("v3", "wb", [sku("401175", "401220", "Blackthorn", "Blackthorn Dry 50L Keg", 293.49)], [], [], [])
    d2, c2, p2 = aio.replace_tennents_master(wb2, source="wb_v3.xlsx")
    _check("no-op when the workbook already has the alt", p2 == 0 and not fake2.patches, f"{p2} {fake2.patches}")

    # --- review fix: the WORKBOOK WINS a code collision (no code on two rows) ---
    # (1) a findings-linked alt (401220 on 401175) that the workbook now makes a
    #     distinct SKU — listed FIRST, the order that previously produced the wrong rate.
    fake3 = FakeIO([live[1]])
    _install(fake3)
    wb3 = TennentsMaster("v4", "wb", [
        sku("401220", "", "Blackthorn", "Blackthorn Dry 50L Keg (new code)", 180.0),
        sku("401175", "", "Blackthorn", "Blackthorn Dry 50L Keg", 293.49),
    ], [], [], [])
    d3, c3, p3 = aio.replace_tennents_master(wb3, source="wb_v4.xlsx")
    by3 = {r["fields"]["sku_code"]: r["fields"] for r in fake3.rows}
    _check("collision: stale alt NOT re-attached to 401175", not by3["401175"].get("alt_code"), str(by3["401175"].get("alt_code")))
    _check("collision: 401220 exists exactly once as its own SKU", sum(1 for r in fake3.rows if r["fields"]["sku_code"] == "401220") == 1)
    m3 = aio.load_tennents_master()
    hit = m3.find_sku("401220")
    _check("collision: 401220 resolves to the workbook's own row @ £180, regardless of order",
           hit is not None and hit.sku_code == "401220" and abs(float(hit.correct_total_per_brl) - 180.0) < 1e-9)
    _check("collision: nothing counted as preserved (the workbook absorbed it)", p3 == 0, str(p3))
    # (2) a findings-CREATED SKU the workbook now lists as an alt of an existing SKU -> not resurrected
    fake4 = FakeIO([live[0]])   # ZZZ999 @150, findings-stamped
    _install(fake4)
    wb4 = TennentsMaster("v5", "wb", [sku("401136", "400217/ZZZ999", "Heverlee", "Heverlee 50L / 30L Keg", 370.14)], [], [], [])
    d4, c4, p4 = aio.replace_tennents_master(wb4, source="wb_v5.xlsx")
    _check("absorbed: findings SKU not re-created when the workbook carries it as an alt",
           all(r["fields"]["sku_code"] != "ZZZ999" for r in fake4.rows) and p4 == 0, f"{[r['fields']['sku_code'] for r in fake4.rows]} p={p4}")
    m4 = aio.load_tennents_master()
    _check("absorbed: the code resolves to the workbook SKU @ £370.14",
           m4.find_sku("ZZZ999") is not None and m4.find_sku("ZZZ999").sku_code == "401136")

    # --- verify-round fixes: leading-zero DRIFT in the collision guard; absorbed alts carried over ---
    # S1: findings-created '012345' @150; the workbook now carries '12345' @200 (Excel dropped the zero)
    fk = FakeIO([{"id": "d1", "fields": {"sku_code": "012345", "correct_total_per_brl": 150.0,
                                          "contract_base_per_brl": 150.0, "source": "findings:j f"}}])
    _install(fk)
    wbd = TennentsMaster("v6", "wb", [sku("12345", "", "Drift", "Drift 50L Keg", 200.0)], [], [], [])
    _, _, pd_ = aio.replace_tennents_master(wbd, source="wb_v6.xlsx")
    _check("drift: exactly ONE row for the drifted code",
           sum(1 for r in fk.rows if r["fields"]["sku_code"].lstrip("0") == "12345") == 1,
           str([r["fields"]["sku_code"] for r in fk.rows]))
    md = aio.load_tennents_master()
    _check("drift: both spellings resolve to the workbook row @ £200",
           all(md.find_sku(c) is not None and abs(float(md.find_sku(c).correct_total_per_brl) - 200.0) < 1e-9
               for c in ("012345", "12345")))
    _check("drift: nothing preserved (workbook wins)", pd_ == 0, str(pd_))
    # S2: findings-linked alt '090999' on 401175; the workbook now lists '90999' as its OWN SKU @180
    fk = FakeIO([{"id": "d2", "fields": {"sku_code": "401175", "alt_code": "090999",
                                          "correct_total_per_brl": 293.49, "source": "findings:j f"}}])
    _install(fk)
    wbd = TennentsMaster("v7", "wb", [sku("90999", "", "New", "New Thing 50L", 180.0),
                                      sku("401175", "", "Blackthorn", "Blackthorn Dry 50L Keg", 293.49)], [], [], [])
    aio.replace_tennents_master(wbd, source="wb_v7.xlsx")
    md = aio.load_tennents_master()
    by = {r["fields"]["sku_code"]: r["fields"] for r in fk.rows}
    _check("drift: stale drifted alt NOT re-attached", not by["401175"].get("alt_code"), str(by["401175"].get("alt_code")))
    _check("drift: '090999' and '90999' both resolve to the workbook SKU @ £180",
           all(md.find_sku(c) is not None and md.find_sku(c).sku_code == "90999" for c in ("090999", "90999")))
    # S3: the workbook now holds the drifted code as ANOTHER SKU's alt -> the findings alt is dropped
    fk = FakeIO([{"id": "d3", "fields": {"sku_code": "401175", "alt_code": "090999",
                                          "correct_total_per_brl": 293.49, "source": "findings:j f"}}])
    _install(fk)
    wbd = TennentsMaster("v9", "wb", [sku("P9", "90999", "Other", "Other 50L", 99.0),
                                      sku("401175", "", "Blackthorn", "Blackthorn Dry 50L Keg", 293.49)], [], [], [])
    aio.replace_tennents_master(wbd, source="wb_v9.xlsx")
    md = aio.load_tennents_master()
    by = {r["fields"]["sku_code"]: r["fields"] for r in fk.rows}
    _check("drift: an alt now claimed by another workbook SKU is dropped (resolves to P9)",
           md.find_sku("090999") is not None and md.find_sku("090999").sku_code == "P9" and not by["401175"].get("alt_code"))
    # S4: an absorbed findings SKU carries its own unclaimed alt onto the absorbing row
    fk = FakeIO([{"id": "d4", "fields": {"sku_code": "ZZZ999", "alt_code": "ZZZ998",
                                          "correct_total_per_brl": 150.0, "source": "findings:j f"}}])
    _install(fk)
    wbd = TennentsMaster("v8", "wb", [sku("401136", "400217/ZZZ999", "Heverlee", "Heverlee 50L / 30L Keg", 370.14)], [], [], [])
    _, _, p8 = aio.replace_tennents_master(wbd, source="wb_v8.xlsx")
    md = aio.load_tennents_master()
    _check("absorbed: its unclaimed alt is carried onto the absorbing row (ZZZ998 -> 401136)",
           md.find_sku("ZZZ998") is not None and md.find_sku("ZZZ998").sku_code == "401136" and p8 == 1,
           f"{md.find_sku('ZZZ998')} p={p8}")
    # own_alts branch: a re-created findings SKU keeps only the alts the workbook hasn't claimed
    fk = FakeIO([{"id": "d5", "fields": {"sku_code": "NEW1", "alt_code": "A1/A2",
                                          "correct_total_per_brl": 100.0, "source": "findings:j f"}}])
    _install(fk)
    wbd = TennentsMaster("v10", "wb", [sku("OLD1", "A2", "Old", "Old 50L", 50.0)], [], [], [])
    _, _, p10 = aio.replace_tennents_master(wbd, source="wb_v10.xlsx")
    md = aio.load_tennents_master()
    _check("re-created findings SKU keeps only its UNCLAIMED alt (A1); A2 stays the workbook's",
           md.find_sku("NEW1") is not None and md.find_sku("A1") is not None and md.find_sku("A1").sku_code == "NEW1"
           and md.find_sku("A2").sku_code == "OLD1" and p10 == 1, f"p={p10}")
    # ...and the DRIFT-spelled variant of that branch (pre-hardening left 090999 on TWO rows)
    fk = FakeIO([{"id": "d6", "fields": {"sku_code": "NEW1", "alt_code": "A1/090999",
                                          "correct_total_per_brl": 100.0, "source": "findings:j f"}}])
    _install(fk)
    wbd = TennentsMaster("v11", "wb", [sku("OLD1", "90999", "Old", "Old 50L", 50.0)], [], [], [])
    _, _, p11 = aio.replace_tennents_master(wbd, source="wb_v11.xlsx")
    md = aio.load_tennents_master()
    by = {r["fields"]["sku_code"]: r["fields"] for r in fk.rows}
    _check("re-created findings SKU drops a DRIFT-spelled alt the workbook claims (090999 ~ 90999 -> OLD1 only)",
           by["NEW1"].get("alt_code") == "A1" and md.find_sku("090999") is not None
           and md.find_sku("090999").sku_code == "OLD1" and md.find_sku("90999").sku_code == "OLD1" and p11 == 1,
           f"{by['NEW1'].get('alt_code')} p={p11}")


# ---------- rendering ----------

def _summary_with_findings():
    return tn.TennentsSummary(
        file_name="Aug26.xlsx", period="2026-08", line_count=3, master_version="vTEST",
        discount_mismatches=[tn.DiscountMismatch("11110001", "STANDARD ARMS", "GUI002", "GUINNESS 50L", "agreed rate",
                                                 249.0, 195.63, 53.37, 1.0, 0.3055, 16.30)],
        exception_pending=[], exceptions_resolved=[], retro_arithmetic=[], line_arithmetic=[], managed_retro=[],
        no_rate=[tn.NoRateRow("11110001", "STANDARD ARMS", "401211", "T.Bavarian 50L", 1.0, 0.3055, 349.99, "RATE TBC")],
        not_on_master=[
            tn.NotOnMasterRow("11110001", "STANDARD ARMS", "401220", "Blackthorn Dry 5% <b>50L</b> Keg", 1.0, 0.3055, 170.15, 0.0),
            tn.NotOnMasterRow("11110001", "STANDARD ARMS", "401187", "Heverlee 4.4% 30L Keg", 1.0, 0.1833, 107.8, 370.34),
        ],
        new_customers=[], sites_did_not_buy=[], master_arithmetic=[], wsp_variance=[],
        total_discount_delta=16.30, pending_short_gbp=0.0, barrels_total=1.0, tlager_barrels=0.0, retro_due_total=0.0,
    )


def test_render():
    print("\n-- render_summary_html")
    m = make_master()
    s = _summary_with_findings()

    html_v = tn.render_summary_html(s)   # viewer: no accept_url
    # (the email JS is still emitted for viewers and mentions the '.t-accept'
    # selector, so check for rendered BUTTON markup, not the bare substring)
    _check("viewer: no accept buttons", "data-mode=" not in html_v and "class='accept-btn t-accept'" not in html_v)
    _check("viewer: draft email still offered", "13. Draft email to Tennents" in html_v and "t-email-body" in html_v)
    _check("viewer: config + JS emitted for the email", 'id="tennents-findings-config"' in html_v)

    html_a = tn.render_summary_html(s, accept_url="/tennents/accept-sku", can_accept=True, source_file="Aug26.xlsx", master=m)
    _check("admin: set-rate button on the no-rate row", "data-mode='set_rate'" in html_a and 'data-sku="401211"' in html_a)
    _check("admin: link select + button on not-on-master rows", "class='link-sel'" in html_a and "data-mode='link'" in html_a)
    _check("admin: Blackthorn new code preselects Blackthorn Dry",
           '<option value="401175" selected>' in html_a, "")
    _check("admin: Heverlee 30L preselects Heverlee", '<option value="401136" selected>' in html_a)
    _check("admin: £0-charged row gets NO 'add as new' button (would bless £0)",
           "£0 charged — link instead" in html_a and html_a.count("data-mode='new'") == 1)
    _check("admin: charged row gets 'add as new' with container", "data-container=\"30L\"" in html_a)
    _check("rows carry data-sku for estate-wide marking", 'tr data-sku="401187"' in html_a)
    _check("description HTML-escaped in the table", "&lt;b&gt;50L&lt;/b&gt;" in html_a and "<b>50L</b>" not in html_a.split("tennents-findings-config")[0])
    cfg = html_a.split('id="tennents-findings-config" type="application/json">')[1].split("</script>")[0]
    _check("config JSON has no raw < > &", "<" not in cfg and ">" not in cfg and "&" not in cfg)
    _check("config carries the short mismatch + rates to confirm", '"short"' in cfg and '"401220"' in cfg and '"401187"' in cfg)
    _check("config acceptUrl + sourceFile", '"acceptUrl": "/tennents/accept-sku"' in cfg and '"sourceFile": "Aug26.xlsx"' in cfg)

    empty = tn.TennentsSummary("f.xlsx", "2026-08", 1, "v", [], [], [], [], [], [], [], [], [], [], [], [])
    html_e = tn.render_summary_html(empty, accept_url="/x", can_accept=True, master=m)
    _check("nothing actionable + no email: no draft section", "13. Draft email" not in html_e)

    # --- review fixes: placeholder option; buttons gated on an unambiguous charged figure ---
    _check("link select has a placeholder first", "<option value=''>— choose —</option>" in html_a)
    s2 = _summary_with_findings()
    # same no-rate SKU at two sites, charged at two different totals (estate-wide accept would be ambiguous)
    s2.no_rate = [
        tn.NoRateRow("11110001", "STANDARD ARMS", "401211", "T.Bavarian 50L", 1.0, 0.3055, 349.99, "", 349.99, 349.99),
        tn.NoRateRow("11110002", "MANAGED HOUSE", "401211", "T.Bavarian 50L", 1.0, 0.3055, 300.00, "", 300.00, 300.00),
    ]
    # same unknown code at two sites, two discounts (the real Aug-26 Heverlee 30L case)
    # one row carries a PADDED raw code ("(401187 )" in the report — _SKU_PAT keeps the space), so the
    # cross-row range only groups both rows if the keys are whitespace-normalised
    s2.not_on_master = [
        tn.NotOnMasterRow("11110001", "STANDARD ARMS", "401187 ", "Heverlee 4.4% 30L Keg", 1.0, 0.1833, 107.8, 370.34, 370.34, 370.34),
        tn.NotOnMasterRow("11110002", "MANAGED HOUSE", "401187", "Heverlee 4.4% 30L Keg", 1.0, 0.1833, 114.9, 161.31, 161.31, 161.31),
    ]
    html_m = tn.render_summary_html(s2, accept_url="/x", can_accept=True, source_file="f", master=m)
    _check("mixed no-rate: NO set-rate button, varies hint instead",
           "data-mode='set_rate'" not in html_m and "charged rates vary" in html_m)
    _check("mixed not-on-master: NO add-as-new, Link still offered",
           "data-mode='new'" not in html_m and html_m.count("data-mode='link'") == 2)
    cfg_m = html_m.split('id="tennents-findings-config" type="application/json">')[1].split("</script>")[0]
    cfg_j = json.loads(cfg_m.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))
    hev = [x for x in cfg_j["email"]["not_on_master"] if x["sku"].strip() == "401187"]
    # both rows must carry the CROSS-ROW range (a per-row fallback would give 370.34/370.34 and 161.31/161.31)
    _check("email config carries the cross-row charged RANGE on EVERY row of a mixed code",
           len(hev) == 2 and all(x["lo"] == 161.31 and x["hi"] == 370.34 for x in hev), str(hev))
    # a single-figure row still gets its button
    s3 = _summary_with_findings()
    html_s = tn.render_summary_html(s3, accept_url="/x", can_accept=True, master=m)
    _check("unambiguous no-rate still offers set-rate", "data-mode='set_rate'" in html_s)


def test_export_multi_alt():
    print("\n-- price export honours '/'-joined alt codes")
    import tennents_price_export as tpe
    from tennents_master import SkuException
    m = make_master()
    m.exceptions.append(SkuException("STANDARD ARMS", "11110001", "401187", "Heverlee 30L", 161.31, 370.14, "under", 38.28, "open"))
    m.reindex()
    site = tpe.find_site(m, "STANDARD ARMS")
    line = tpe._price_line(m, site, m.find_sku("401136"))
    _check("exception keyed on the SECOND alt code surfaces in the price-file note",
           "Correction pending" in line["note"], line["note"])
    # non-vacuous: the primary and brand/product hit NO mapping; only the SECOND alt (400889) is mapped,
    # so the old two-code loop returns "Other" for this SKU
    z = SkuRate("ZZZ1", "ZZZ2/400889", "Foo", "Foo 50L", "50L", 0.3055, 4.0, 600.0, 100.0, True, "C&C", 0.0, 100.0)
    _check("category resolves via the SECOND alt code", tpe._category(z) == "Standard Lager", tpe._category(z))


if __name__ == "__main__":
    test_codes_and_suggest()
    test_accept_primitive()
    test_preserve_on_replace()
    test_render()
    test_export_multi_alt()
    print("\nALL PASS" if PASS else "\nFAILURES")
    sys.exit(0 if PASS else 1)

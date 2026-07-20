"""
Offline test for cask classification in the LWC findings suggested-price.

Cask beer must be priced FB cost + a fixed £/keg margin, not the 40%-GP draught
rule (cost / 0.6). Cask is identified from the product description. Trade lines
often mark cask by container size ("... 9G") without the word "cask", so
_is_cask must recognise the 9-gallon firkin / 4.5G pin too — while NEVER tagging
a keg (which would under-price it).

No Airtable network access.

Run standalone (exit 0 = pass, 1 = fail):

    python test_cask_pricing.py
"""
import sys

import summary


def _check(desc, expected, why):
    got = summary._is_cask(desc)
    ok = got == expected
    print(f"  [{'ok' if ok else 'FAIL'}] _is_cask({desc!r}) = {got}  ({why})")
    return ok


def main() -> int:
    ok = True

    print("cask (must be True):")
    for desc in [
        "Sharp Doombar 9G Cask",          # word + size
        "SHARPS TWIN COAST PALE ALE 9G",  # size only, no word (the bug case)
        "NEPTUNE BREWERY SEA OF DREAMS 9G",
        "THEAKSTON XB 4.5G CASK",         # pin
        "DRAUGHT BASS 10G CASK",          # 10G but explicitly cask
        "Some Ale 9 Gallon",              # spelled-out gallon
    ]:
        ok &= _check(desc, True, "cask")

    print("keg / other draught (must be False — else we under-price):")
    for desc in [
        "Stella Artois 10G Keg",          # 10G is a keg here, not cask
        "Fosters 11G Keg",
        "JOHN SMITHS EXTRA SMOOTH 30L KEG",
        "Carling 22G",
        "Madri Lager 50L Keg",
        "SUNPRIDE PINEAPPLE TETRA 12X1L",  # 'pin' substring must not trigger
        "",
    ]:
        ok &= _check(desc, False, "not cask")

    print("suggested price routing (policy defaults: cask +£35, pin +£17.50, draught 40% GP):")
    cost = 90.0
    for label, got, want in [
        ("cask 9G   -> cost + £35", summary._suggested_price("SHARPS TWIN COAST PALE ALE 9G", cost), 125.0),
        ("pin 4.5G  -> cost + £17.50 (half)", summary._suggested_price("THEAKSTON XB 4.5G CASK", cost), 107.5),
        ("pin, size only", summary._suggested_price("Some Ale 4.5 Gallon", cost), 107.5),
        ("cask 10G  -> full £35 (not a pin)", summary._suggested_price("DRAUGHT BASS 10G CASK", cost), 125.0),
        ("keg       -> cost / 0.6", summary._suggested_price("Fosters 11G Keg", cost), 150.0),
    ]:
        good = got is not None and abs(got - want) < 0.005
        ok &= good
        print(f"  [{'ok' if good else 'FAIL'}] {label}: £{got} (want £{want})")

    print("pin margin follows the live policy value (policy £40 -> pin £20):")
    pol = {"cask_margin_gbp": 40.0}
    got = summary._suggested_price("THEAKSTON XB 4.5G CASK", cost, pol)
    good = got is not None and abs(got - 110.0) < 0.005
    ok &= good
    print(f"  [{'ok' if good else 'FAIL'}] pin with policy £40: £{got} (want £110.0)")

    print("white-label fixed prices override the policy:")
    for code, desc, want in [
        ("17910010", "Appleshed Premium Cider 50L", 145.0),
        ("15621274", "Black Sheep Smooth 50L Keg", 135.0),
        ("19100003", "Pilsner 11g", 135.0),
    ]:
        got = summary._suggested_price(desc, cost, code=code)
        good = got is not None and abs(got - want) < 0.005
        ok &= good
        print(f"  [{'ok' if good else 'FAIL'}] {code} {desc}: £{got} (want £{want})")
    # Fixed price stands even with no cost basis (a cost-less line must not
    # suppress the suggestion), and an unknown code falls through to policy.
    got = summary._suggested_price("Appleshed Premium Cider 50L", 0.0, code="17910010")
    good = got == 145.0
    ok &= good
    print(f"  [{'ok' if good else 'FAIL'}] white-label with no cost basis: £{got} (want £145.0)")
    got = summary._suggested_price("Fosters 11G Keg", cost, code="99999999")
    good = got is not None and abs(got - 150.0) < 0.005
    ok &= good
    print(f"  [{'ok' if good else 'FAIL'}] non-white-label code unaffected: £{got} (want £150.0)")

    print("findings table: sug-margin %% column + both accept buttons:")
    row = summary.OtherFindingRow(
        site_id="123", site_name="THE SHIP", product_code="55555",
        product_desc="Fosters 11G Keg", qty=2.0, charged=140.0, cost=90.0,
    )
    html = summary._acceptable_table([row], can_accept=True)
    checks = [
        ("Sug. margin % header", "Sug. margin %" in html),
        ("sug margin value (40%)", ">40.0%<" in html),
        ("Add at charged button", "Add at charged" in html),
        ("amendable input prefilled", 'class=\'sug-input\'' in html and 'value="150.00"' in html),
        ("Add at this price button", "Add at this price" in html),
    ]
    html_viewer = summary._acceptable_table([row], can_accept=False)
    checks.append(("viewer gets no buttons", "Add at" not in html_viewer))
    checks.append(("viewer still sees sug margin", "Sug. margin %" in html_viewer))
    wl_row = summary.OtherFindingRow(
        site_id="123", site_name="THE SHIP", product_code="17910010",
        product_desc="Appleshed Premium Cider 50L", qty=1.0, charged=150.0, cost=98.0,
    )
    wl_html = summary._acceptable_table([wl_row], can_accept=True)
    checks.append(("white-label suggested £145", "£145.00" in wl_html))
    checks.append(("white-label basis tooltip", "white-labelled FB Cider" in wl_html))
    for label, good in checks:
        ok &= good
        print(f"  [{'ok' if good else 'FAIL'}] {label}")

    print("email rows carry the white-label tag:")
    er = summary._email_missing_row(wl_row, None)
    good = er["wl"] == "FB Cider" and er["suggested"] == 145.0
    ok &= good
    print(f"  [{'ok' if good else 'FAIL'}] wl={er['wl']!r} suggested={er['suggested']}")
    er2 = summary._email_missing_row(row, None)
    good = er2["wl"] is None and abs(er2["suggested"] - 150.0) < 0.005
    ok &= good
    print(f"  [{'ok' if good else 'FAIL'}] non-wl row: wl={er2['wl']!r} suggested={er2['suggested']}")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

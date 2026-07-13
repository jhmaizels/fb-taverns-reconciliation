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

    print("suggested price routing (policy defaults: cask +£35, draught 40% GP):")
    cost = 90.0
    cask_sug = summary._suggested_price("SHARPS TWIN COAST PALE ALE 9G", cost)
    draught_sug = summary._suggested_price("Fosters 11G Keg", cost)
    for label, got, want in [
        ("cask 9G -> cost + £35", cask_sug, 125.0),
        ("keg    -> cost / 0.6", draught_sug, 150.0),
    ]:
        good = got is not None and abs(got - want) < 0.005
        ok &= good
        print(f"  [{'ok' if good else 'FAIL'}] {label}: £{got} (want £{want})")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Offline test for the master-snapshot cache, focused on the stale-fallback that
stops a post-write read from doing the ~30s inline Airtable rebuild (the
hub-proxy timeout the operator hits as "one button then the next overloaded").

No Airtable network access: _fetch_master_snapshot and refresh_master_cache_async
are stubbed so behaviour is deterministic and single-threaded.

Run standalone (exit 0 = pass, 1 = fail):

    python test_master_cache.py
"""
import sys
import time

import airtable_io as aio


def main() -> int:
    ok = True
    fetches = {"n": 0}
    refresh_kicks = {"n": 0}

    def fake_fetch():
        fetches["n"] += 1
        return f"SNAP#{fetches['n']}"

    def fake_refresh():
        refresh_kicks["n"] += 1  # no thread; we assert it was kicked, not run

    aio._fetch_master_snapshot = fake_fetch
    aio.refresh_master_cache_async = fake_refresh

    def reset(snapshot=None, ts=0.0, last=None):
        aio._MASTER_CACHE.update(
            {"snapshot": snapshot, "ts": ts, "gen": 0, "refreshing": False, "last": last}
        )

    def check(label, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")

    now = time.monotonic()

    # 1. Warm cache within TTL -> return cached, no fetch, no refresh kick.
    reset(snapshot="WARM", ts=now, last="WARM")
    fetches["n"] = 0; refresh_kicks["n"] = 0
    got = aio.load_master_snapshot()
    check("warm cache returns cached without fetching", got == "WARM" and fetches["n"] == 0)

    # 2. TTL lapsed -> serve stale cached + kick refresh, still no inline fetch.
    reset(snapshot="STALE", ts=now - (aio.MASTER_CACHE_TTL + 5), last="STALE")
    fetches["n"] = 0; refresh_kicks["n"] = 0
    got = aio.load_master_snapshot()
    check("TTL lapse serves stale + kicks refresh, no inline fetch",
          got == "STALE" and fetches["n"] == 0 and refresh_kicks["n"] == 1)

    # 3. THE HARDENING: a write invalidated snapshot to None but 'last' is
    #    retained -> serve last stale + kick refresh, NO 30s inline fetch.
    reset(snapshot="GOOD", ts=now)
    aio.invalidate_master_cache()          # simulates a master write
    check("invalidate retains last snapshot", aio._MASTER_CACHE["last"] == "GOOD"
          and aio._MASTER_CACHE["snapshot"] is None)
    fetches["n"] = 0; refresh_kicks["n"] = 0
    got = aio.load_master_snapshot()
    check("cold-after-write serves retained snapshot, NO inline fetch",
          got == "GOOD" and fetches["n"] == 0 and refresh_kicks["n"] == 1)

    # 4. publish_patched installs a fresh entry AND refreshes 'last'.
    reset(snapshot=None, ts=0.0, last="OLD")
    aio.publish_patched_snapshot("PATCHED")
    fetches["n"] = 0
    got = aio.load_master_snapshot()
    check("publish_patched serves the patched snapshot",
          got == "PATCHED" and aio._MASTER_CACHE["last"] == "PATCHED" and fetches["n"] == 0)

    # 5. Truly cold (no snapshot, no last, e.g. first read post-boot) -> inline
    #    fetch is the only option; it also seeds 'last'.
    reset(snapshot=None, ts=0.0, last=None)
    fetches["n"] = 0
    got = aio.load_master_snapshot()
    check("truly-cold start does the inline fetch and seeds last",
          got == "SNAP#1" and fetches["n"] == 1 and aio._MASTER_CACHE["last"] == "SNAP#1")

    # 6. invalidate on an already-cold cache must not clobber last with None.
    reset(snapshot=None, ts=0.0, last="KEEP")
    aio.invalidate_master_cache()
    check("invalidate on cold cache keeps the existing last", aio._MASTER_CACHE["last"] == "KEEP")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

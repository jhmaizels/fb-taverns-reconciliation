"""Independent backup of the FB Taverns drinks Airtable base.

Exports every table (Sites, Products, Files, PricingRules, Mismatches,
TennentsAgreements) to JSON with a manifest (record counts + sha256), into a
dated folder you copy off-site. This is the Airtable half of "Layer 2" — it
pairs with the tenancy-hub backup (tenancies/scripts/backup.ts). The full
runbook lives in tenancies/docs/BACKUP.md.

Run from the drinks repo (needs AIRTABLE_TOKEN + AIRTABLE_BASE_ID in .env/env):

    python backup_airtable.py                    # -> ./backups/airtable-<ts>/
    python backup_airtable.py D:/fb-backups      # explicit output root
    python backup_airtable.py --verify <folder>  # check an existing backup

Read-only against Airtable. Importing airtable_io calls load_dotenv() and loads
the shared schema, so this reuses the same paginated, retrying client the app
uses — no re-implemented auth or pagination.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import airtable_io
from airtable_io import BASE_ID, T, _list_all, _require_env

TOOL_VERSION = "1.0.0"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _restore_doc(manifest: dict) -> str:
    tables = ", ".join(manifest["tables"].keys())
    return f"""# Restore — drinks Airtable base

Point-in-time export of Airtable base `{manifest['source']['base_id']}`
taken {manifest['created_at']}.
Tables ({manifest['totals']['tables']}): {tables}.

## What's here
- `manifest.json` — every table with its record count, byte size and sha256.
- `airtable/<Table>.json` — a JSON array of the raw records
  (`id`, `createdTime`, `fields`), all fields, all pages.

## Verify (needs nothing else)
    python backup_airtable.py --verify "<path to this folder>"

## Restore options (in order of preference)
1. **Airtable snapshots** (base History -> snapshots) — the true point-in-time
   restore. Keep these enabled; this JSON export is the vendor-independent
   backstop, not a replacement.
2. **Rebuild via the API** — recreate records from each `fields` object. NOTE:
   Airtable assigns new record ids on create, so linked-record references
   (fields that store other records' ids) will not survive a full base
   recreation and must be re-linked. For the drinks base the linkage is light
   (Sites/Products codes are stable text keys), so a rebuild is feasible.
"""


def backup(out_root: Path) -> int:
    _require_env()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_root / f"airtable-{stamp}"
    (out / "airtable").mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "tool": "drinks-airtable-backup",
        "tool_version": TOOL_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"base_id": BASE_ID},
        "tables": {},
        "totals": {},
        "ok": True,
        "errors": [],
    }
    errors: list[str] = manifest["errors"]
    total = 0
    print(f"Backing up Airtable base {BASE_ID} -> {out}")
    for name, table_id in T.items():
        try:
            records = _list_all(table_id)  # all fields, all pages, retrying
            payload = json.dumps(records, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
            (out / "airtable" / f"{name}.json").write_bytes(payload)
            manifest["tables"][name] = {
                "records": len(records),
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
            total += len(records)
            print(f"  {name:<22} {len(records):>6} records  {len(payload) // 1024}KB")
        except Exception as e:  # noqa: BLE001 — one table failing must not lose the rest
            errors.append(f"{name}: {e}")
            print(f"  {name:<22} FAILED: {e}", file=sys.stderr)

    manifest["ok"] = not errors
    manifest["totals"] = {"tables": len(manifest["tables"]), "records": total}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "RESTORE.md").write_text(_restore_doc(manifest))

    if errors:
        print(f"\nINCOMPLETE — {len(errors)} error(s); partial backup at {out}", file=sys.stderr)
        return 1
    print(f"\nOK — {len(manifest['tables'])} tables, {total} records. Backup: {out}")
    print(f'Verify it:  python backup_airtable.py --verify "{out}"')
    return 0


def verify(folder: Path) -> int:
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json in {folder} — is that a backup folder?", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    problems: list[str] = []
    if not manifest.get("ok", False):
        problems.append(f"backup was written INCOMPLETE ({len(manifest.get('errors', []))} error(s) at capture time)")

    print(f"Verifying {folder}")
    print(f"  taken {manifest.get('created_at')}")

    seen = set()
    total = 0
    for name, entry in manifest.get("tables", {}).items():
        f = folder / "airtable" / f"{name}.json"
        seen.add(f"{name}.json")
        if not f.exists():
            problems.append(f"missing file airtable/{name}.json")
            continue
        data = f.read_bytes()
        if _sha256(data) != entry["sha256"]:
            problems.append(f"checksum mismatch airtable/{name}.json")
        if len(data) != entry["bytes"]:
            problems.append(f"size mismatch airtable/{name}.json ({len(data)} vs {entry['bytes']})")
        try:
            records = json.loads(data)
        except Exception as e:  # noqa: BLE001
            problems.append(f"unparseable JSON airtable/{name}.json: {e}")
            continue
        if len(records) != entry["records"]:
            problems.append(f"record count mismatch airtable/{name}.json ({len(records)} vs {entry['records']})")
        total += len(records)

    air_dir = folder / "airtable"
    if air_dir.exists():
        for p in air_dir.iterdir():
            if p.suffix == ".json" and p.name not in seen:
                problems.append(f"undeclared file airtable/{p.name} (not in manifest)")

    print(f"  {len(manifest.get('tables', {}))} tables, {total} records verified")
    if problems:
        print(f"\nFAIL — {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1
    print("\nIntegrity OK — Airtable backup is complete and every checksum matches.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--verify":
        if len(args) < 2:
            print("Usage: python backup_airtable.py --verify <folder>", file=sys.stderr)
            return 2
        return verify(Path(args[1]))
    out_root = Path(args[0]) if args else Path("backups")
    return backup(out_root)


if __name__ == "__main__":
    raise SystemExit(main())

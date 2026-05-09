"""One-shot CSV -> printqueue/orders.json importer.
Reverses the schema written by app.py's /ledger.csv endpoint (line 692).
Existing rows in orders.json are preserved; CSV rows whose filename already
exists in the ledger are skipped (dedup by 'filename'). CSV history is
prepended (older), existing rows kept after.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
CSV_PATH = REPO / "orders.csv"
LEDGER = REPO / "printqueue" / "orders.json"


def csv_to_record(row: dict) -> dict:
    def f(s: str) -> float:
        return float(s) if s.strip() else 0.0

    def i(s: str) -> int:
        return int(s) if s.strip() else 0

    return {
        "timestamp": row["Timestamp"],
        "customer": row["Customer"],
        "card": row["Library Card"],
        "flow": row["Flow"],
        "printer": row["Printer"],
        "filename": row["Output File"],
        "plate_count": i(row["Plates"]),
        "total_grams": f(row["Total Grams"]),
        "total_time_label": row["Total Time"],
        "price_cad": f(row["Price (CAD)"]),
        "colors": [c.strip() for c in row["Colours"].split(",") if c.strip()],
    }


def main() -> int:
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found", file=sys.stderr)
        return 1

    with CSV_PATH.open(newline="", encoding="utf-8-sig") as fh:
        csv_records = [csv_to_record(r) for r in csv.DictReader(fh)]

    LEDGER.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if LEDGER.exists():
        backup = LEDGER.with_suffix(".json.bak")
        backup.write_bytes(LEDGER.read_bytes())
        print(f"Backup written: {backup}")
        try:
            existing = json.loads(LEDGER.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"WARN: existing {LEDGER} is not valid JSON, treating as empty")
            existing = []

    existing_files = {r.get("filename") for r in existing if r.get("filename")}
    to_add = [r for r in csv_records if r["filename"] not in existing_files]
    combined = to_add + existing

    LEDGER.write_text(json.dumps(combined, indent=2), encoding="utf-8")

    print(f"CSV rows read         : {len(csv_records)}")
    print(f"Existing ledger rows  : {len(existing)}")
    print(f"Skipped (filename dup): {len(csv_records) - len(to_add)}")
    print(f"Added                 : {len(to_add)}")
    print(f"Total in {LEDGER.name}: {len(combined)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

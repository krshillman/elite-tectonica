"""
importer.py — Read all CSVs from /data and load them into SQLite.

Safe to run multiple times — existing planets are preserved via INSERT OR IGNORE.
The batch_id is extracted from the filename UUID so records are traceable.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from db import upsert_planet

DATA_DIR = Path(__file__).parent.parent / "data"

# Matches filenames like: bodies-search-UUID-pagenumber.csv
_FILENAME_RE = re.compile(
    r"bodies-search-([0-9A-Fa-f\-]+)-\d+.*\.csv$", re.IGNORECASE
)


def _extract_batch_id(path: Path) -> str:
    """Extract the UUID portion from a Spansh CSV filename."""
    m = _FILENAME_RE.match(path.name)
    return m.group(1) if m else "unknown"


def _to_float(value: str) -> float | None:
    """Convert a string to float, returning None for empty/invalid values."""
    try:
        return float(value) if value.strip() else None
    except (ValueError, AttributeError):
        return None


def load_all_csvs(data_dir: Path = DATA_DIR) -> dict:
    """
    Read every .csv file in data_dir and upsert into SQLite.

    Returns a summary dict:
        {
            "files_read": int,
            "rows_processed": int,
            "rows_inserted": int,   # approximate — based on rows processed
        }
    """
    csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        return {"files_read": 0, "rows_processed": 0}

    files_read = 0
    rows_processed = 0

    for csv_path in csv_files:
        batch_id = _extract_batch_id(csv_path)
        try:
            with open(csv_path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    upsert_planet(
                        system_name=row.get("System Name", "").strip(),
                        name=row.get("Name", "").strip(),
                        gravity=_to_float(row.get("Gravity", "")),
                        distance_to_arrival_ls=_to_float(
                            row.get("Distance to Arrival (LS)", "")
                        ),
                        atmosphere=row.get("Atmosphere", "").strip() or None,
                        distance_ly=_to_float(row.get("Distance (LY)", "")),
                        subtype=row.get("Subtype", "").strip() or None,
                        last_updated_at=row.get("Last Updated At", "").strip() or None,
                        batch_id=batch_id,
                    )
                    rows_processed += 1
            files_read += 1
        except Exception as exc:
            # Don't abort the whole import for one bad file
            print(f"[importer] WARNING: skipping {csv_path.name}: {exc}")

    return {
        "files_read": files_read,
        "rows_processed": rows_processed,
    }

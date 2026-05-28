"""
archiver.py — Append Completed + Skipped planets to a Parquet file, then delete
              them from SQLite.

The archive grows incrementally across Batches and becomes the labelled training
dataset for a future ML model predicting Stratum Tectonicas likelihood.
"""

from pathlib import Path

import pandas as pd

from db import delete_completed_skipped, get_archivable_planets

ARCHIVE_DIR = Path(__file__).parent.parent / "archive"
ARCHIVE_PATH = ARCHIVE_DIR / "stratum_archive.parquet"

# Explicit dtype map keeps the Parquet schema stable across appends.
_DTYPE_MAP: dict[str, str] = {
    "system_name":             "string",
    "name":                    "string",
    "gravity":                 "Float64",
    "distance_to_arrival_ls":  "Float64",
    "atmosphere":              "string",
    "distance_ly":             "Float64",
    "subtype":                 "string",
    "last_updated_at":         "string",
    "first_footfall":          "boolean",
    "no_of_biologicals":       "Int64",
    "contains_stratum":        "boolean",
    "status":                  "string",
    "notes":                   "string",
    "batch_id":                "string",
    "imported_at":             "string",
}


def archive_and_clear() -> int:
    """
    1. Read all Completed + Skipped planets from SQLite.
    2. Append them to the Parquet archive (creating the file if needed).
    3. Delete them from SQLite.

    Returns the number of rows archived.
    """
    rows = get_archivable_planets()
    if not rows:
        return 0

    df_new = pd.DataFrame([dict(r) for r in rows])

    for col, dtype in _DTYPE_MAP.items():
        if col in df_new.columns:
            try:
                df_new[col] = df_new[col].astype(dtype)
            except (ValueError, TypeError):
                pass  # Leave as-is if cast fails rather than losing data

    ARCHIVE_DIR.mkdir(exist_ok=True)

    if ARCHIVE_PATH.exists():
        df_existing = pd.read_parquet(ARCHIVE_PATH)
        df_out = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df_out = df_new

    df_out.to_parquet(ARCHIVE_PATH, index=False)

    return delete_completed_skipped()

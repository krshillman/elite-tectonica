# ADR 0003 — Parquet for the ML training archive

**Status:** Accepted  
**Date:** 2026-05-28

## Context

Completed and Skipped Planet records need to be archived for later use as a labelled ML training dataset to predict Stratum Tectonicas likelihood. The archive accumulates across multiple Batches over time. The archive must include all raw CSV features (Gravity, Atmosphere, Subtype, Distance, etc.) plus the manually-assigned labels (FF, Bios, Stratum, Status).

## Decision

Archive to a single growing **Parquet** file (`archive/stratum_archive.parquet`), appended on each archive operation. All columns — both raw CSV features and manual labels — are stored.

## Alternatives considered

| Option | Reason rejected |
|---|---|
| CSV | No compression, no schema enforcement, slow for large datasets |
| SQLite (same db) | Mixing working state with historical data creates schema coupling; archive is append-only and read-heavy |
| JSON Lines | No columnar compression; poor pandas interoperability at scale |

## Consequences

- `pandas.read_parquet` / `pyarrow` loads the archive instantly into a DataFrame ready for sklearn or similar.
- Parquet stores column types (bool, float, string, category) without lossy CSV round-tripping.
- The archive file grows incrementally — each archive operation appends rows from the current Batch's Completed + Skipped planets, preserving the full feature set for the ML pipeline.
- `pandas` and `pyarrow` are added as runtime dependencies.

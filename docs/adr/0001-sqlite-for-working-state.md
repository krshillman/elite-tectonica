# ADR 0001 — SQLite for working state

**Status:** Accepted  
**Date:** 2026-05-28

## Context

The app needs to persist Planet status, First Footfall, Bio Signals, Contains Stratum Tectonicas, and Notes across app restarts. The project originated as a Google Sheets / Excel workflow, so writing state back to an `.xlsx` file was the obvious default.

## Decision

Use a local SQLite `.db` file as the single source of truth for working state. Every edit auto-saves immediately.

## Alternatives considered

| Option | Reason rejected |
|---|---|
| Write back to `.xlsx` | No auto-save; one crash = lost edits; openpyxl is slow on 10k rows |
| JSON file | Unwieldy at 10k rows; no concurrent-safe writes; no query capability |
| Keep Google Sheets | Requires internet; defeats the "run locally" goal entirely |

## Consequences

- Zero "did I forget to save?" risk — every checkbox click, every status change is durable immediately.
- The archive pipeline (`SELECT * FROM planets WHERE status IN ('Completed','Skipped')`) can be run directly against SQLite before exporting to Parquet.
- The `.xlsx` file is relegated to an optional export artifact, not the primary store.

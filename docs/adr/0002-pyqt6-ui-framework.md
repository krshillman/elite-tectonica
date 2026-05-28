# ADR 0002 — PyQt6 as the UI framework

**Status:** Accepted  
**Date:** 2026-05-28

## Context

The app needs to display ~10,000 Planet rows grouped under ~thousands of System parent rows in a collapsible tree view, support inline editing (checkboxes, spinboxes, dropdowns, text), and be snappy enough to alt-tab to from a running game. A pure CLI or spreadsheet-export approach was also considered.

## Decision

Use **PyQt6** (`PyQt6`, `PyQt6-Qt6`) as the UI framework. The primary widget is `QTreeWidget` (or `QTreeView` + `QStandardItemModel`) with inline delegates for each column type.

## Alternatives considered

| Option | Reason rejected |
|---|---|
| tkinter | Built-in but no native tree widget with inline editing; looks dated |
| CustomTkinter | Modern aesthetics but still tkinter under the hood; poor table/tree support |
| Dear PyGui | Excellent for large data, but tree/hierarchy UX is less mature; harder to implement inline editing delegates |
| Web app (Flask + browser) | Cross-process copy-to-clipboard is awkward; overkill for single-user local tool |

## Consequences

- `QTreeWidget` gives first-class collapsible System → Planet hierarchy with per-column delegates.
- `QApplication.clipboard().setText()` makes one-click "copy system name" trivial.
- PyQt6 is LGPL licensed — no distribution concerns for personal use.
- Install adds ~50 MB (`pip install PyQt6`); acceptable for a desktop tool.

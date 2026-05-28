"""
main_window.py — Top-level QMainWindow for Elite Tectonica.

Toolbar actions
---------------
  📥 Import CSVs        — run importer.load_all_csvs() then reload tree
  📦 Archive & Clear    — confirm, run archiver.archive_and_clear(), reload tree
  🔄 Reload             — re-read DB without touching data

Status bar
----------
  Live aggregate counts: Systems · Planets · Completed · Skipped · In Progress · Pending
  Updated on startup, after every import/archive, and on every Status change in the tree.
"""

from __future__ import annotations

import archiver
import db
import importer as csv_importer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .planet_tree import PlanetTree


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Elite Tectonica — Stratum Tectonicas Tracker")
        self.resize(1200, 750)
        self._build_ui()
        self._refresh_status_bar()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Toolbar ──
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        act_import = QAction("📥  Import CSVs", self)
        act_import.setToolTip("Load all CSV files from the /data folder into SQLite")
        act_import.triggered.connect(self._import_csvs)
        toolbar.addAction(act_import)

        toolbar.addSeparator()

        act_archive = QAction("📦  Archive & Clear", self)
        act_archive.setToolTip(
            "Export Completed + Skipped planets to the Parquet archive "
            "then remove them from the working set"
        )
        act_archive.triggered.connect(self._archive_and_clear)
        toolbar.addAction(act_archive)

        toolbar.addSeparator()

        act_reload = QAction("🔄  Reload", self)
        act_reload.setToolTip("Refresh the tree from the database")
        act_reload.triggered.connect(self._reload)
        toolbar.addAction(act_reload)

        # ── Central widget ──
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        self.tree = PlanetTree()
        self.tree.statusBarUpdate.connect(self._refresh_status_bar)
        layout.addWidget(self.tree)

        # ── Status bar ──
        self._status_label = QLabel()
        status_bar = QStatusBar()
        status_bar.addWidget(self._status_label)
        self.setStatusBar(status_bar)

        # Initial tree load
        self._reload()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _reload(self) -> None:
        self.tree.reload()
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        t = db.get_totals()
        self._status_label.setText(
            f"Systems: {t['system_count']}  │  "
            f"Planets: {t['planet_count']}  │  "
            f"✓ Completed: {t['completed']}  │  "
            f"✗ Skipped: {t['skipped']}  │  "
            f"⟳ In Progress: {t['in_progress']}  │  "
            f"⬜ Pending: {t['pending']}"
        )

    def _import_csvs(self) -> None:
        result = csv_importer.load_all_csvs()
        if result["files_read"] == 0:
            QMessageBox.information(
                self,
                "Import",
                "No CSV files found in the /data folder.\n\n"
                "Place Spansh CSV exports (bodies-search-UUID-N.csv) "
                "in the data/ directory next to this project.",
            )
            return
        QMessageBox.information(
            self,
            "Import Complete",
            f"Read {result['files_read']} file(s) — "
            f"{result['rows_processed']} rows processed.\n\n"
            "Existing planets were left unchanged (INSERT OR IGNORE).",
        )
        self._reload()

    def _archive_and_clear(self) -> None:
        counts  = db.get_pending_in_progress_counts()
        pending = counts.get("pending") or 0
        in_prog = counts.get("in_progress") or 0

        warning = ""
        if pending > 0 or in_prog > 0:
            warning = (
                f"\n\n⚠  {pending} Pending and {in_prog} In Progress planet(s) "
                "will NOT be archived — only Completed + Skipped are exported. "
                "They will remain in the working set."
            )

        reply = QMessageBox.question(
            self,
            "Archive & Clear",
            "Export all Completed + Skipped planets to\n"
            "  archive/stratum_archive.parquet\n"
            "then delete them from the working database?"
            f"{warning}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        count = archiver.archive_and_clear()
        if count == 0:
            QMessageBox.information(
                self, "Archive", "Nothing to archive — no Completed or Skipped planets."
            )
            return

        QMessageBox.information(
            self,
            "Archive Complete",
            f"{count} planet(s) appended to archive/stratum_archive.parquet\n"
            "and removed from the working set.",
        )
        self._reload()

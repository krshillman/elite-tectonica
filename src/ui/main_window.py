"""
main_window.py — Top-level QMainWindow for Elite Tectonica.

Toolbar actions
---------------
  📥 Import CSVs        — run importer.load_all_csvs() then reload tree
  📦 Archive & Clear    — confirm, run archiver.archive_and_clear(), reload tree
  🔄 Reload             — re-read DB without touching data
  🛰  Fetch Coords       — download missing system coordinates from EDSM
                          (background thread) for live distance sorting
  🌙 Nightly Baseline   — download EDSM's nightly dump (once per UTC day)
                          and apply update-recency + coords locally, saving
                          thousands of rate-limited API calls
  📡 Fetch System Info  — download EDSM body update recency + traffic stats
                          (background thread); annotates likely-footfallen
                          systems with "recommend skip" notes
  📍 Refresh Nearby     — spend the limited live-API budget refreshing only
                          the N systems closest to the commander
  ⏭  Auto-Skip          — preview + confirm skipping of systems matching the
                          configurable rule (recently updated + busy traffic)
  ⚙  Skip Settings      — configure the auto-skip thresholds

Location tracking
-----------------
  A QTimer polls the Elite Dangerous journal logs every few seconds. When the
  commander jumps to a new system the tree is re-sorted closest-first from
  the new position, and the current system is shown in the status bar.

Journal auto-fill
-----------------
  The same poll tails new journal events and auto-fills labels:
    Touchdown                      → planet Pending → In Progress
    FSSBodySignals/SAASignalsFound → biological signal count
    ScanOrganic (Stratum species)  → Contains Stratum checked

Status bar
----------
  Live aggregate counts: Systems · Planets · Completed · Skipped · In Progress · Pending
  Updated on startup, after every import/archive, and on every Status change in the tree.
"""

from __future__ import annotations

from typing import Optional

import archiver
import autoskip
import coords as coords_fetcher
import db
import edsm_meta
import edsm_nightly
import importer as csv_importer
import journal
from PyQt6.QtCore import QThread, QTimer, pyqtSignal
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

from .dialogs import AutoSkipPreviewDialog, AutoSkipSettingsDialog
from .planet_tree import PlanetTree

JOURNAL_POLL_MS = 5_000  # how often to check the journal for a new location


class _CoordsFetchWorker(QThread):
    """Fetch missing system coordinates from EDSM without blocking the UI."""

    progressed = pyqtSignal(int, int)   # done, total
    finished_with = pyqtSignal(dict)    # summary dict

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        def _progress(done: int, total: int) -> bool:
            self.progressed.emit(done, total)
            return not self._cancelled

        summary = coords_fetcher.fetch_missing_coords(progress=_progress)
        self.finished_with.emit(summary)


class _MetaFetchWorker(QThread):
    """
    Fetch EDSM system metadata (update recency + traffic) in background.
    When the commander's position is known, closest systems are fetched
    first so nearby data is fresh immediately.

    ``nearby_only=True`` restricts the fetch to the N closest stale systems
    (settings: nearby_refresh_count), reserving the limited API budget for
    the systems that matter most right now.
    """

    progressed = pyqtSignal(int, int)   # done, total
    finished_with = pyqtSignal(dict)    # summary dict

    def __init__(self, parent=None, current_pos: Optional[tuple] = None,
                 nearby_only: bool = False):
        super().__init__(parent)
        self._cancelled = False
        self._current_pos = current_pos
        self._nearby_only = nearby_only

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        def _progress(done: int, total: int) -> bool:
            self.progressed.emit(done, total)
            return not self._cancelled

        if self._nearby_only and self._current_pos is not None:
            summary = edsm_meta.fetch_nearby_meta(
                self._current_pos, progress=_progress
            )
        else:
            summary = edsm_meta.fetch_missing_meta(
                progress=_progress, current_pos=self._current_pos
            )
        self.finished_with.emit(summary)


class _NightlyWorker(QThread):
    """Download + apply an EDSM dump baseline without blocking the UI.

    ``full=True`` runs the one-off FULL systems dump (several GB);
    otherwise the daily 7-day dump."""

    progressed = pyqtSignal(str)        # status message
    finished_with = pyqtSignal(dict)    # summary dict

    def __init__(self, parent=None, force: bool = False, full: bool = False):
        super().__init__(parent)
        self._cancelled = False
        self._force = force
        self._full = full

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        def _progress(message: str) -> bool:
            self.progressed.emit(message)
            return not self._cancelled

        if self._full:
            summary = edsm_nightly.run_full_baseline(progress=_progress)
        else:
            summary = edsm_nightly.run_baseline(
                progress=_progress, force=self._force
            )
        self.finished_with.emit(summary)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Elite Tectonica — Stratum Tectonicas Tracker")
        self.resize(1200, 750)
        self._location: Optional[journal.Location] = None
        self._coords_worker: Optional[_CoordsFetchWorker] = None
        self._meta_worker: Optional[_MetaFetchWorker] = None
        self._nightly_worker: Optional[_NightlyWorker] = None
        self._journal_watcher = journal.JournalWatcher()
        self._build_ui()
        self._refresh_status_bar()

        # Detect starting location, then poll the journal for jumps.
        self._check_journal()
        self._journal_timer = QTimer(self)
        self._journal_timer.timeout.connect(self._check_journal)
        self._journal_timer.start(JOURNAL_POLL_MS)

        # One-off: offer the FULL dump baseline if it has never been run.
        # Otherwise auto-run the small daily dump (at most once per UTC day).
        if (edsm_nightly.full_baseline_last_run() is None
                and not db.get_setting_bool("full_baseline_prompted", False)):
            QTimer.singleShot(2_000, self._suggest_full_baseline)
        elif edsm_nightly.is_due():
            QTimer.singleShot(2_000, lambda: self._run_nightly(force=False))

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

        toolbar.addSeparator()

        self._act_fetch_coords = QAction("🛰  Fetch Coords", self)
        self._act_fetch_coords.setToolTip(
            "Download galactic coordinates for systems from EDSM so the list "
            "can be sorted by live distance from your current position"
        )
        self._act_fetch_coords.triggered.connect(self._fetch_coords)
        toolbar.addAction(self._act_fetch_coords)

        self._act_nightly = QAction("🌙  Nightly Baseline", self)
        self._act_nightly.setToolTip(
            "Download an EDSM nightly dump and match it against your working "
            "set locally — saves thousands of rate-limited API calls.\n"
            "Daily: 7-day dump (runs automatically once per day).\n"
            "Full Refresh: entire galaxy dump (several GB) — run once for a "
            "complete baseline, then rely on the daily dump."
        )
        self._act_nightly.triggered.connect(
            lambda: self._run_nightly(force=True)
        )
        toolbar.addAction(self._act_nightly)

        self._act_fetch_meta = QAction("📡  Fetch System Info", self)
        self._act_fetch_meta.setToolTip(
            "Download EDSM body update dates and traffic stats per system.\n"
            "Recently-updated, busy systems have probably lost their first "
            "footfalls — they get a 'recommend skip' note."
        )
        self._act_fetch_meta.triggered.connect(self._fetch_meta)
        toolbar.addAction(self._act_fetch_meta)

        self._act_refresh_nearby = QAction("📍  Refresh Nearby", self)
        self._act_refresh_nearby.setToolTip(
            "Refresh live EDSM traffic + update data for only the closest "
            "systems to your current position (configurable in ⚙ Skip "
            "Settings). Uses far fewer rate-limited API calls than a full "
            "fetch."
        )
        self._act_refresh_nearby.triggered.connect(self._refresh_nearby)
        toolbar.addAction(self._act_refresh_nearby)

        toolbar.addSeparator()

        act_autoskip = QAction("⏭  Auto-Skip", self)
        act_autoskip.setToolTip(
            "Preview and skip systems matching the rule: EDSM recently "
            "updated AND traffic above threshold. Pinned systems are never "
            "included; nothing is skipped without confirmation."
        )
        act_autoskip.triggered.connect(self._auto_skip)
        toolbar.addAction(act_autoskip)

        act_skip_settings = QAction("⚙  Skip Settings", self)
        act_skip_settings.setToolTip("Configure the auto-skip rule thresholds")
        act_skip_settings.triggered.connect(self._skip_settings)
        toolbar.addAction(act_skip_settings)

        # Current-location indicator lives on the right edge of the toolbar
        self._location_label = QLabel("  📍 Location: unknown  ")
        toolbar.addSeparator()
        toolbar.addWidget(self._location_label)

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
        c = db.count_coords_cached()
        self._status_label.setText(
            f"Systems: {t['system_count']}  │  "
            f"Planets: {t['planet_count']}  │  "
            f"✓ Completed: {t['completed']}  │  "
            f"✗ Skipped: {t['skipped']}  │  "
            f"⟳ In Progress: {t['in_progress']}  │  "
            f"⬜ Pending: {t['pending']}  │  "
            f"🛰 Coords: {c['with_coords']}/{c['total_systems']}"
        )

    # ── Journal location tracking ─────────────────────────────────────────────

    def _check_journal(self) -> None:
        """Poll the journal; if the commander moved, re-sort the tree."""
        self._process_autofill_events()

        loc = journal.read_current_location()
        if loc is None:
            return

        moved = self._location is None or loc.system_name != self._location.system_name
        self._location = loc
        self._location_label.setText(f"  📍 {loc.system_name}  ")

        if moved:
            # Fall back to cached coords if this event had no StarPos
            pos = loc.pos or db.get_system_coords(loc.system_name)
            self.tree.reload(current_pos=pos, current_system=loc.system_name)

    def _process_autofill_events(self) -> None:
        """Apply new journal events (touchdown / bio signals / Stratum scans)."""
        changed = False
        for ev in self._journal_watcher.poll():
            if ev.kind == "touchdown":
                changed |= db.mark_planet_in_progress_by_name(ev.body_name) > 0
            elif ev.kind == "bio_signals" and ev.count is not None:
                changed |= db.set_planet_bios_by_name(ev.body_name, ev.count) > 0
            elif ev.kind == "stratum":
                changed |= db.set_planet_stratum_by_name(ev.body_name) > 0

        if changed:
            pos = None
            if self._location is not None:
                pos = self._location.pos or db.get_system_coords(
                    self._location.system_name
                )
            self.tree.reload(
                current_pos=pos,
                current_system=(
                    self._location.system_name if self._location else None
                ),
            )
            self._refresh_status_bar()

    # ── EDSM coordinate fetch ─────────────────────────────────────────────────

    def _fetch_coords(self) -> None:
        if self._coords_worker is not None and self._coords_worker.isRunning():
            return  # already fetching

        missing = db.get_systems_missing_coords()
        if not missing:
            QMessageBox.information(
                self, "Fetch Coordinates",
                "All systems already have cached coordinates."
            )
            return

        reply = QMessageBox.question(
            self,
            "Fetch Coordinates",
            f"{len(missing)} system(s) have no cached coordinates.\n\n"
            "Fetch them from EDSM now? This runs in the background and may "
            f"take ~{max(1, len(missing) // 100 * 2 // 60 + 1)} minute(s).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._act_fetch_coords.setEnabled(False)
        self._coords_worker = _CoordsFetchWorker(self)
        self._coords_worker.progressed.connect(self._on_coords_progress)
        self._coords_worker.finished_with.connect(self._on_coords_done)
        self._coords_worker.start()

    def _on_coords_progress(self, done: int, total: int) -> None:
        self._act_fetch_coords.setText(f"🛰  Fetching… {done}/{total}")

    def _on_coords_done(self, summary: dict) -> None:
        self._act_fetch_coords.setText("🛰  Fetch Coords")
        self._act_fetch_coords.setEnabled(True)
        self._refresh_status_bar()

        # Re-sort now that we have coordinates
        if self._location is not None:
            pos = self._location.pos or db.get_system_coords(self._location.system_name)
            self.tree.reload(current_pos=pos, current_system=self._location.system_name)

        msg = (
            f"Fetched coordinates for {summary['fetched']} of "
            f"{summary['requested']} system(s)."
        )
        if summary["errors"]:
            msg += f"\n\n⚠ {summary['errors']} batch(es) failed — try again later."
        not_found = summary["requested"] - summary["fetched"]
        if not_found > 0 and not summary["errors"]:
            msg += (
                f"\n\n{not_found} system(s) were not found on EDSM; "
                "they will sort to the bottom of the list."
            )
        QMessageBox.information(self, "Fetch Complete", msg)

    # ── EDSM system metadata fetch ────────────────────────────────────────────

    def _fetch_meta(self) -> None:
        if self._meta_worker is not None and self._meta_worker.isRunning():
            return  # already fetching

        missing = db.get_systems_missing_meta()
        if not missing:
            QMessageBox.information(
                self, "Fetch System Info",
                "All systems already have cached EDSM metadata."
            )
            return

        # ~2 requests/system at ~0.8 s each
        est_min = max(1, round(len(missing) * 0.8 / 60))
        pos = None
        if self._location is not None:
            pos = self._location.pos or db.get_system_coords(
                self._location.system_name
            )
        priority = (
            "\n\nSystems closest to your current position are fetched first."
            if pos is not None else ""
        )
        reply = QMessageBox.question(
            self,
            "Fetch System Info",
            f"{len(missing)} system(s) have no cached EDSM metadata "
            "(body update dates + traffic).\n\n"
            f"Fetch them now? This runs in the background and may take "
            f"~{est_min} minute(s).{priority}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._act_fetch_meta.setEnabled(False)
        self._meta_worker = _MetaFetchWorker(self, current_pos=pos)
        self._meta_worker.progressed.connect(self._on_meta_progress)
        self._meta_worker.finished_with.connect(self._on_meta_done)
        self._meta_worker.start()

    def _on_meta_progress(self, done: int, total: int) -> None:
        self._act_fetch_meta.setText(f"📡  Fetching… {done}/{total}")

    def _on_meta_done(self, summary: dict) -> None:
        self._act_fetch_meta.setText("📡  Fetch System Info")
        self._act_fetch_meta.setEnabled(True)

        # Annotate likely-footfallen systems with a "recommend skip" note
        noted = autoskip.annotate_candidates()

        self._reload()

        msg = (
            f"Fetched EDSM metadata for {summary['fetched']} of "
            f"{summary['requested']} system(s)."
        )
        if summary["errors"]:
            msg += f"\n\n⚠ {summary['errors']} system(s) failed — try again later."
        if noted:
            msg += (
                f"\n\n{noted} system(s) match the auto-skip rule and were "
                "annotated with a 'recommend skip' note.\n"
                "Use ⏭ Auto-Skip to review and skip them."
            )
        QMessageBox.information(self, "Fetch Complete", msg)

    # ── Nightly dump baseline ─────────────────────────────────────────────────

    def _suggest_full_baseline(self) -> None:
        """
        One-time startup prompt: the FULL galaxy dump has never been applied.
        Offer to run it now (best baseline: record dates + coords for every
        working-set system, zero API calls). Asked only once — declining
        falls back to the daily 7-day dump and the user can still run a
        Full Refresh later from 🌙 Nightly Baseline.
        """
        db.set_setting("full_baseline_prompted", "1")

        box = QMessageBox(self)
        box.setWindowTitle("Full Baseline (one-off)")
        box.setText(
            "The one-off FULL EDSM baseline has never been run.\n\n"
            "It downloads the entire systems dump (several GB, deleted "
            "after processing) and gives EVERY system in your working set "
            "a record date + coordinates without using any API calls.\n"
            "After this, the small daily 7-day dump keeps it current.\n\n"
            "Run it now in the background?"
        )
        btn_full = box.addButton(
            "Run Full Baseline", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(
            "Not now (use daily dump)", QMessageBox.ButtonRole.RejectRole
        )
        box.exec()

        if box.clickedButton() is btn_full:
            self._start_nightly_worker(full=True, notify=True)
        elif edsm_nightly.is_due():
            self._run_nightly(force=False)

    def _run_nightly(self, force: bool = False) -> None:
        """Download + apply an EDSM dump baseline in the background."""
        if (self._nightly_worker is not None
                and self._nightly_worker.isRunning()):
            return  # already running

        full = False
        if force:
            last = edsm_nightly.last_run() or "never"
            full_last = edsm_nightly.full_baseline_last_run() or "never"
            box = QMessageBox(self)
            box.setWindowTitle("Nightly Baseline")
            box.setText(
                "Match an EDSM dump against your working set locally — "
                "replaces thousands of live API calls for bulk data.\n\n"
                f"• Daily (7-day dump, ~tens of MB) — last run: {last}\n"
                f"• Full Refresh (ALL systems, several GB!) — last run: "
                f"{full_last}\n\n"
                "Run in the background?"
            )
            btn_daily = box.addButton(
                "Daily (7-day dump)", QMessageBox.ButtonRole.AcceptRole
            )
            btn_full = box.addButton(
                "Full Refresh (several GB)", QMessageBox.ButtonRole.ActionRole
            )
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is btn_full:
                full = True
            elif box.clickedButton() is not btn_daily:
                return  # cancelled

        self._start_nightly_worker(full=full, notify=force, force=force)

    def _start_nightly_worker(
        self, full: bool, notify: bool, force: bool = True
    ) -> None:
        self._act_nightly.setEnabled(False)
        self._act_nightly.setText("🌙  Nightly… starting")
        self._nightly_worker = _NightlyWorker(self, force=force, full=full)
        self._nightly_worker.progressed.connect(self._on_nightly_progress)
        self._nightly_worker.finished_with.connect(
            lambda summary: self._on_nightly_done(summary, notify=notify)
        )
        self._nightly_worker.start()

    def _on_nightly_progress(self, message: str) -> None:
        self._act_nightly.setText(f"🌙  {message}")

    def _on_nightly_done(self, summary: dict, notify: bool) -> None:
        self._act_nightly.setText("🌙  Nightly Baseline")
        self._act_nightly.setEnabled(True)

        if summary.get("ran"):
            # Dump advanced edsm_updated_at values — refresh recommendations
            autoskip.annotate_candidates()
            self._reload()

        if not notify:
            return  # silent when auto-run at startup

        if summary.get("error"):
            QMessageBox.warning(
                self, "Nightly Baseline",
                f"Baseline failed: {summary['error']}\n\nTry again later."
            )
        elif summary.get("cancelled"):
            QMessageBox.information(
                self, "Nightly Baseline", "Baseline cancelled."
            )
        elif summary.get("ran"):
            QMessageBox.information(
                self, "Nightly Baseline",
                f"Scanned {summary['scanned']:,} dump entries.\n\n"
                f"{summary['matched']} working-set system(s) were updated on "
                "EDSM within the last 7 days — their update-recency baseline "
                "has been refreshed locally (no API calls used).\n"
                f"Coordinates cached for {summary['coords']} system(s).\n\n"
                "Use 📍 Refresh Nearby to top up live traffic data for the "
                "systems closest to you."
            )

    # ── Nearby live refresh ───────────────────────────────────────────────────

    def _refresh_nearby(self) -> None:
        """Spend the live API budget on the N closest stale systems only."""
        if self._meta_worker is not None and self._meta_worker.isRunning():
            return  # a meta fetch is already running

        pos = None
        if self._location is not None:
            pos = self._location.pos or db.get_system_coords(
                self._location.system_name
            )
        if pos is None:
            QMessageBox.information(
                self, "Refresh Nearby",
                "Your current position is unknown — start Elite Dangerous "
                "(or jump once) so the journal reveals your location.\n\n"
                "Coordinates may also need fetching first (🛰)."
            )
            return

        self._act_refresh_nearby.setEnabled(False)
        self._meta_worker = _MetaFetchWorker(
            self, current_pos=pos, nearby_only=True
        )
        self._meta_worker.progressed.connect(self._on_nearby_progress)
        self._meta_worker.finished_with.connect(self._on_nearby_done)
        self._meta_worker.start()

    def _on_nearby_progress(self, done: int, total: int) -> None:
        self._act_refresh_nearby.setText(f"📍  Refreshing… {done}/{total}")

    def _on_nearby_done(self, summary: dict) -> None:
        self._act_refresh_nearby.setText("📍  Refresh Nearby")
        self._act_refresh_nearby.setEnabled(True)

        noted = autoskip.annotate_candidates()
        self._reload()

        if summary["requested"] == 0:
            QMessageBox.information(
                self, "Refresh Nearby",
                "Nothing to refresh — nearby systems already have fresh "
                "EDSM metadata."
            )
            return

        msg = (
            f"Refreshed live EDSM data for {summary['fetched']} of "
            f"{summary['requested']} nearby system(s)."
        )
        if summary["errors"]:
            msg += f"\n\n⚠ {summary['errors']} system(s) failed."
        if noted:
            msg += (
                f"\n\n{noted} system(s) match the auto-skip rule — "
                "use ⏭ Auto-Skip to review them."
            )
        QMessageBox.information(self, "Refresh Complete", msg)

    # ── Auto-skip ─────────────────────────────────────────────────────────────

    def _auto_skip(self) -> None:
        cfg = autoskip.get_config()
        if not cfg["enabled"]:
            QMessageBox.information(
                self, "Auto-Skip",
                "Auto-skip is disabled.\n\nEnable it in ⚙ Skip Settings."
            )
            return

        candidates = autoskip.get_candidates()
        if not candidates:
            QMessageBox.information(
                self, "Auto-Skip",
                "No systems match the auto-skip rule.\n\n"
                "Either you haven't fetched system info yet (📡), or every "
                "matching system is pinned / already handled."
            )
            return

        pos = None
        if self._location is not None:
            pos = self._location.pos or db.get_system_coords(
                self._location.system_name
            )

        dlg = AutoSkipPreviewDialog(candidates, current_pos=pos, parent=self)
        if dlg.exec() != AutoSkipPreviewDialog.DialogCode.Accepted:
            return

        # Pin the systems the user unchecked so they're never suggested again
        for name in dlg.to_pin:
            db.set_system_pinned(name, True)

        planets = autoskip.apply_skip(dlg.selected)
        self._reload()

        msg = (
            f"Skipped {planets} planet(s) across "
            f"{len(dlg.selected)} system(s)."
        )
        if dlg.to_pin:
            msg += f"\n\n📌 Pinned {len(dlg.to_pin)} system(s) you unchecked."
        QMessageBox.information(self, "Auto-Skip Complete", msg)

    def _skip_settings(self) -> None:
        dlg = AutoSkipSettingsDialog(self)
        dlg.exec()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._coords_worker is not None and self._coords_worker.isRunning():
            self._coords_worker.cancel()
            self._coords_worker.wait(3_000)
        if self._meta_worker is not None and self._meta_worker.isRunning():
            self._meta_worker.cancel()
            self._meta_worker.wait(3_000)
        if self._nightly_worker is not None and self._nightly_worker.isRunning():
            self._nightly_worker.cancel()
            self._nightly_worker.wait(3_000)
        super().closeEvent(event)

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

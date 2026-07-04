"""
planet_tree.py — Two-level QTreeWidget: System → Planet.

Layout
------
  Col 0  Name        System name (bold) / Planet name
  Col 1  Distance    Live distance from commander (LY) / arrival dist (LS)
  Col 2  EDSM Upd    Most recent EDSM body update date (system rows only)
  Col 3  Traffic     EDSM ships 24h/week (system rows only)
  Col 4  FF Chance   First-footfall likelihood tier (system rows only)
  Col 5  FF          First Footfall checkbox (planet rows only)
  Col 6  Bios        No of Biologicals spinbox (planet rows only)
  Col 7  Stratum     Contains Stratum Tectonicas checkbox (planet rows only)
  Col 8  Status      Status dropdown (planet) / summary text (system)
  Col 9  Notes       Free-text (planet rows only)

Pinned systems (📌) are excluded from Auto-Skip; toggle via context menu.

Behaviour
---------
- System children are lazy-loaded on first expand (placeholder child trick).
- Every manual-label edit auto-saves to SQLite immediately via itemChanged.
- Right-clicking a system row offers "Copy system name" and
  "Copy & Mark In Progress" (sets all Pending → In Progress, copies name).
- Row background is tinted by planet Status for quick visual scanning.
- When the commander's position is known (journal logs), systems are sorted
  closest-first using live distances computed from cached EDSM coordinates.
  The system currently occupied is flagged with 📍.
"""

from __future__ import annotations

import math
from typing import Optional

import autoskip
import db
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import QApplication, QHeaderView, QMenu, QTreeWidget, QTreeWidgetItem

from .delegates import ComboBoxDelegate, ReadOnlyDelegate, SpinBoxDelegate

# ── Column indices ────────────────────────────────────────────────────────────
COL_NAME    = 0
COL_DIST    = 1
COL_UPDATED = 2   # EDSM body-data update recency (system rows)
COL_TRAFFIC = 3   # EDSM ships 24h/week (system rows)
COL_CHANCE  = 4   # first-footfall likelihood tier (system rows)
COL_FF      = 5
COL_BIOS    = 6
COL_STRATUM = 7
COL_STATUS  = 8
COL_NOTES   = 9

_HEADERS = ["Name", "Distance", "EDSM Upd", "Traffic", "FF Chance", "FF",
            "Bios", "Stratum", "Status", "Notes"]

# FF-chance tier → foreground colour (dark-theme friendly)
_CHANCE_FG: dict[int, QColor] = {
    0: QColor("#e05555"),   # Very Low  — red
    1: QColor("#e08a3c"),   # Low       — orange
    2: QColor("#d6c04a"),   # Moderate  — yellow
    3: QColor("#7fca5f"),   # High      — light green
    4: QColor("#3fdc78"),   # Very High — bright green
}

# Status → background colour (dark-theme friendly)
_STATUS_BG: dict[str, QColor | None] = {
    "Completed":   QColor(28, 68, 40),   # dark forest green
    "Skipped":     QColor(68, 28, 28),   # dark muted red
    "In Progress": QColor(68, 55, 20),   # dark amber
    "Pending":     None,                  # default background
}

# Flags applied to every planet item
_PLANET_FLAGS = (
    Qt.ItemFlag.ItemIsEnabled
    | Qt.ItemFlag.ItemIsSelectable
    | Qt.ItemFlag.ItemIsEditable       # for Bios, Status, Notes
    | Qt.ItemFlag.ItemIsUserCheckable  # for FF, Stratum
)

_SYSTEM_FLAGS = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

_PLACEHOLDER_ROLE = Qt.ItemDataRole.UserRole  # col-0 UserRole stores id OR system name
_PLACEHOLDER_TAG  = "__placeholder__"


class PlanetTree(QTreeWidget):
    """
    Lazy-loading two-level tree.  Auto-saves every label edit to SQLite.
    Emits ``statusBarUpdate`` whenever an edit might change aggregate totals.
    """

    statusBarUpdate = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False
        self._setup_ui()
        self._setup_delegates()
        self.itemChanged.connect(self._on_item_changed)
        self.itemExpanded.connect(self._on_item_expanded)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setColumnCount(len(_HEADERS))
        self.setHeaderLabels(_HEADERS)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(False)
        self.setRootIsDecorated(True)

        hdr = self.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(COL_NAME,    QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_DIST,    QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_UPDATED, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_TRAFFIC, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_CHANCE,  QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_FF,      QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_BIOS,    QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_STRATUM, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_STATUS,  QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_NOTES,   QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(COL_NAME, 300)

    def _setup_delegates(self) -> None:
        # Block editing on name + checkbox columns
        ro = ReadOnlyDelegate(self)
        self.setItemDelegateForColumn(COL_NAME,    ro)
        self.setItemDelegateForColumn(COL_DIST,    ro)
        self.setItemDelegateForColumn(COL_UPDATED, ro)
        self.setItemDelegateForColumn(COL_TRAFFIC, ro)
        self.setItemDelegateForColumn(COL_CHANCE,  ro)
        self.setItemDelegateForColumn(COL_FF,      ro)
        self.setItemDelegateForColumn(COL_STRATUM, ro)
        # Custom editors for Bios and Status
        self.setItemDelegateForColumn(COL_BIOS,   SpinBoxDelegate(0, 20, self))
        self.setItemDelegateForColumn(COL_STATUS, ComboBoxDelegate(list(db.STATUS_VALUES), self))
        # COL_NOTES uses the default QStyledItemDelegate (QLineEdit)

    # ── Public API ────────────────────────────────────────────────────────────

    def reload(
        self,
        current_pos: Optional[tuple] = None,
        current_system: Optional[str] = None,
    ) -> None:
        """
        Clear and re-populate top-level system rows from the database.

        When ``current_pos`` (x, y, z in LY) is provided, systems are sorted
        by live distance from that position — closest first — so the next
        system to visit is always at the top. Systems without cached
        coordinates sink to the bottom (alphabetical).
        """
        if current_pos is not None:
            self._current_pos = current_pos
        if current_system is not None:
            self._current_system = current_system

        self._loading = True
        self.clear()

        bold = QFont()
        bold.setBold(True)

        rows = db.get_all_systems()

        pos = getattr(self, "_current_pos", None)
        distances: dict[str, Optional[float]] = {}
        for row in rows:
            if pos is not None and row["x"] is not None:
                distances[row["system_name"]] = math.sqrt(
                    (row["x"] - pos[0]) ** 2
                    + (row["y"] - pos[1]) ** 2
                    + (row["z"] - pos[2]) ** 2
                )
            else:
                distances[row["system_name"]] = None

        if pos is not None:
            rows = sorted(
                rows,
                key=lambda r: (
                    distances[r["system_name"]] is None,   # unknown coords last
                    distances[r["system_name"]] or 0.0,
                    r["system_name"],
                ),
            )

        here = getattr(self, "_current_system", None)
        meta_map = db.get_system_meta_map()

        for row in rows:
            name = row["system_name"]
            sys_item = QTreeWidgetItem(self)
            sys_item.setFlags(_SYSTEM_FLAGS)
            sys_item.setFont(COL_NAME, bold)
            meta = meta_map.get(name)
            pinned = bool(meta and meta.get("pinned"))
            label = f"📍 {name}" if name == here else name
            if pinned:
                label = f"📌 {label}"
            sys_item.setText(COL_NAME, label)
            sys_item.setData(COL_NAME, Qt.ItemDataRole.UserRole, name)

            # EDSM metadata columns (system rows only)
            if meta:
                updated = meta.get("edsm_updated_at")
                sys_item.setText(COL_UPDATED, (updated or "—")[:10])
                day = meta.get("traffic_day")
                week = meta.get("traffic_week")
                day_s = str(day) if day is not None else "—"
                week_s = str(week) if week is not None else "—"
                sys_item.setText(COL_TRAFFIC, f"{day_s} / {week_s}")
                sys_item.setToolTip(
                    COL_TRAFFIC, "EDSM traffic: last 24 h / last 7 days"
                )
                tier = autoskip.ff_chance(meta)
                if tier is not None:
                    sys_item.setText(
                        COL_CHANCE, autoskip.FF_CHANCE_LABELS[tier]
                    )
                    sys_item.setForeground(
                        COL_CHANCE, QBrush(_CHANCE_FG[tier])
                    )
                tip_parts = []
                if meta.get("first_discovered"):
                    tip_parts.append(f"First discovered: {meta['first_discovered']}")
                if meta.get("discovered_by"):
                    tip_parts.append(f"Discovered by: {meta['discovered_by']}")
                if meta.get("traffic_total") is not None:
                    tip_parts.append(f"Traffic (all-time): {meta['traffic_total']}")
                if tip_parts:
                    sys_item.setToolTip(COL_NAME, "\n".join(tip_parts))

            dist = distances[name]
            if name == here:
                sys_item.setText(COL_DIST, "HERE")
            elif dist is not None:
                sys_item.setText(COL_DIST, f"{dist:,.1f} ly")
            else:
                sys_item.setText(COL_DIST, "—" if pos is not None else "")

            self._set_system_summary_from_row(sys_item, row)
            # Placeholder so the expand arrow is shown before lazy-load
            ph = QTreeWidgetItem(sys_item)
            ph.setFlags(Qt.ItemFlag.NoItemFlags)
            ph.setData(0, _PLACEHOLDER_ROLE, _PLACEHOLDER_TAG)

        self._loading = False

    # ── Lazy loading ──────────────────────────────────────────────────────────

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.childCount() == 1:
            ph = item.child(0)
            if ph.data(0, _PLACEHOLDER_ROLE) == _PLACEHOLDER_TAG:
                item.takeChild(0)
                self._load_planets_for(item)

    def _load_planets_for(self, sys_item: QTreeWidgetItem) -> None:
        system_name = sys_item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        self._loading = True
        for p in db.get_planets_for_system(system_name):
            child = QTreeWidgetItem(sys_item)
            self._fill_planet_item(child, p)
        self._loading = False

    # ── Item population ───────────────────────────────────────────────────────

    def _fill_planet_item(self, item: QTreeWidgetItem, p) -> None:
        """Populate all columns of a planet row from a db.Row."""
        item.setFlags(_PLANET_FLAGS)

        # Col 0 — Name; store DB id in UserRole for later saves
        item.setText(COL_NAME, p["name"])
        item.setData(COL_NAME, Qt.ItemDataRole.UserRole, p["id"])

        # Col 1 — Distance to arrival within the system (LS)
        try:
            arrival = p["distance_to_arrival_ls"]
        except (IndexError, KeyError):
            arrival = None
        item.setText(COL_DIST, f"{arrival:,.0f} ls" if arrival is not None else "")

        # Col 2 — First Footfall
        item.setCheckState(
            COL_FF,
            Qt.CheckState.Checked if p["first_footfall"] else Qt.CheckState.Unchecked,
        )

        # Col 3 — No of Biologicals (integer; None → empty display, 0 in editor)
        bios = p["no_of_biologicals"]
        if bios is not None:
            item.setData(COL_BIOS, Qt.ItemDataRole.DisplayRole, bios)
        else:
            item.setText(COL_BIOS, "")

        # Col 4 — Contains Stratum Tectonicas
        item.setCheckState(
            COL_STRATUM,
            Qt.CheckState.Checked if p["contains_stratum"] else Qt.CheckState.Unchecked,
        )

        # Col 5 — Status
        status = p["status"] or "Pending"
        item.setText(COL_STATUS, status)

        # Col 6 — Notes
        item.setText(COL_NOTES, p["notes"] or "")

        self._apply_status_bg(item, status)

    def _apply_status_bg(self, item: QTreeWidgetItem, status: str) -> None:
        color = _STATUS_BG.get(status)
        for col in range(self.columnCount()):
            if color:
                item.setBackground(col, QBrush(color))
            else:
                item.setData(col, Qt.ItemDataRole.BackgroundRole, None)

    # ── System summary ────────────────────────────────────────────────────────

    def _set_system_summary_from_row(self, sys_item: QTreeWidgetItem, row) -> None:
        """Write the status summary text using pre-fetched aggregate row."""
        completed = row["completed_count"]
        skipped   = row["skipped_count"]
        in_prog   = row["in_progress_count"]
        total     = row["planet_count"]
        sys_item.setText(COL_STATUS, f"{completed}✓  {skipped}✗  {in_prog}⟳  / {total}")
        self._tint_system_row(sys_item, completed + skipped, total)

    def _refresh_system_summary(self, sys_item: QTreeWidgetItem) -> None:
        """
        Recount status from the already-expanded planet children.
        Avoids a DB round-trip for every single edit.
        """
        n = sys_item.childCount()
        if n == 0:
            return
        if sys_item.child(0).data(0, _PLACEHOLDER_ROLE) == _PLACEHOLDER_TAG:
            return  # Not yet expanded — summary was set from DB at load time

        total = completed = skipped = in_prog = 0
        for i in range(n):
            child = sys_item.child(i)
            status = child.text(COL_STATUS) or "Pending"
            total += 1
            if status == "Completed":
                completed += 1
            elif status == "Skipped":
                skipped += 1
            elif status == "In Progress":
                in_prog += 1

        sys_item.setText(COL_STATUS, f"{completed}✓  {skipped}✗  {in_prog}⟳  / {total}")
        self._tint_system_row(sys_item, completed + skipped, total)

    def _tint_system_row(
        self, sys_item: QTreeWidgetItem, done: int, total: int
    ) -> None:
        """Dim fully-exhausted system rows so active ones stand out."""
        dim = QBrush(QColor("#888888"))
        normal = QBrush(QColor("#dddddd"))
        fg = dim if (total > 0 and done == total) else normal
        for col in range(self.columnCount()):
            sys_item.setForeground(col, fg)

    # ── Edit → DB persistence ─────────────────────────────────────────────────

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._loading:
            return

        planet_id = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        if not isinstance(planet_id, int):
            return  # System row — nothing to persist

        if column == COL_FF:
            val = 1 if item.checkState(COL_FF) == Qt.CheckState.Checked else 0
            db.update_planet_field(planet_id, "first_footfall", val)

        elif column == COL_BIOS:
            raw = item.data(COL_BIOS, Qt.ItemDataRole.EditRole)
            try:
                val: int | None = int(raw)
            except (TypeError, ValueError):
                val = None
            db.update_planet_field(planet_id, "no_of_biologicals", val)

        elif column == COL_STRATUM:
            val = 1 if item.checkState(COL_STRATUM) == Qt.CheckState.Checked else 0
            db.update_planet_field(planet_id, "contains_stratum", val)

        elif column == COL_STATUS:
            status = item.text(COL_STATUS) or "Pending"
            db.update_planet_field(planet_id, "status", status)
            self._apply_status_bg(item, status)
            sys_item = item.parent()
            if sys_item:
                self._refresh_system_summary(sys_item)
            self.statusBarUpdate.emit()

        elif column == COL_NOTES:
            db.update_planet_field(planet_id, "notes", item.text(COL_NOTES))

    # ── Context menu ──────────────────────────────────────────────────────────

    def _show_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return

        is_system = item.parent() is None
        if not is_system:
            return  # No per-planet context menu yet

        system_name = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        meta = db.get_system_meta_map().get(system_name)
        pinned = bool(meta and meta.get("pinned"))

        menu = QMenu(self)
        act_copy     = menu.addAction("📋  Copy system name")
        act_mark     = menu.addAction("▶   Copy & Mark In Progress")
        menu.addSeparator()
        act_pin      = menu.addAction(
            "📌  Unpin (allow auto-skip)" if pinned
            else "📌  Pin (never auto-skip)"
        )

        chosen = menu.exec(self.viewport().mapToGlobal(pos))

        if chosen == act_copy:
            QApplication.clipboard().setText(system_name)

        elif chosen == act_mark:
            QApplication.clipboard().setText(system_name)
            db.set_system_in_progress(system_name)
            self._reload_system_children(item)
            self._refresh_system_summary(item)
            self.statusBarUpdate.emit()

        elif chosen == act_pin:
            db.set_system_pinned(system_name, not pinned)
            # Update the row label in place (📌 prefix)
            here = getattr(self, "_current_system", None)
            label = f"📍 {system_name}" if system_name == here else system_name
            if not pinned:  # it is now pinned
                label = f"📌 {label}"
            self._loading = True
            item.setText(COL_NAME, label)
            self._loading = False

    def _reload_system_children(self, sys_item: QTreeWidgetItem) -> None:
        """
        Refresh planet rows for an already-expanded system from the database.
        No-op if the system has never been expanded (placeholder still present).
        """
        n = sys_item.childCount()
        if n == 0:
            return
        if sys_item.child(0).data(0, _PLACEHOLDER_ROLE) == _PLACEHOLDER_TAG:
            return

        system_name = sys_item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        planets     = db.get_planets_for_system(system_name)
        planet_map  = {p["id"]: p for p in planets}

        self._loading = True
        for i in range(n):
            child = sys_item.child(i)
            pid   = child.data(COL_NAME, Qt.ItemDataRole.UserRole)
            if pid in planet_map:
                self._fill_planet_item(child, planet_map[pid])
        self._loading = False

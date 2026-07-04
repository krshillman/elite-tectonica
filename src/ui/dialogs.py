"""
dialogs.py — Auto-Skip preview and settings dialogs.

AutoSkipSettingsDialog
    Edit the configurable rule thresholds (enabled / max updated days /
    min weekly traffic). Persisted via autoskip.save_config().

AutoSkipPreviewDialog
    Shows every system currently matching the rule with a checkbox each
    (all checked by default). Nothing is skipped until the user confirms.
    Unchecked systems are auto-pinned so future passes leave them alone.
"""

from __future__ import annotations

import math
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

import autoskip
import db


class AutoSkipSettingsDialog(QDialog):
    """Edit auto-skip rule thresholds."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Auto-Skip Settings")
        self.setMinimumWidth(420)

        cfg = autoskip.get_config()

        layout = QVBoxLayout(self)

        info = QLabel(
            "A system is recommended for skipping when its EDSM body data "
            "was updated recently AND it sees regular traffic — meaning "
            "first footfalls have almost certainly been taken.\n\n"
            "Pinned systems (📌) are never auto-skipped."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()

        self.chk_enabled = QCheckBox("Enable auto-skip recommendations")
        self.chk_enabled.setChecked(cfg["enabled"])
        form.addRow(self.chk_enabled)

        self.spin_days = QSpinBox()
        self.spin_days.setRange(1, 3650)
        self.spin_days.setSuffix(" days")
        self.spin_days.setValue(cfg["max_updated_days"])
        self.spin_days.setToolTip(
            "Only systems whose EDSM body data was updated within this many "
            "days are considered 'recently visited'"
        )
        form.addRow("EDSM updated within:", self.spin_days)

        self.spin_traffic = QSpinBox()
        self.spin_traffic.setRange(0, 100000)
        self.spin_traffic.setSuffix(" ships/week")
        self.spin_traffic.setValue(cfg["min_traffic"])
        self.spin_traffic.setToolTip(
            "Minimum weekly EDSM traffic for a system to be considered "
            "'busy enough' to have lost its first footfalls"
        )
        form.addRow("Minimum traffic:", self.spin_traffic)

        # ── EDSM data budget (nightly dump + nearby refresh) ──
        self.chk_nightly = QCheckBox(
            "Auto-download EDSM nightly dump once per day"
        )
        self.chk_nightly.setChecked(db.get_setting_bool("nightly_enabled", True))
        self.chk_nightly.setToolTip(
            "Downloads EDSM's 'updated in the last 7 days' dump once per UTC "
            "day and matches it locally — bulk update-recency data without "
            "spending any rate-limited API calls"
        )
        form.addRow(self.chk_nightly)

        self.spin_nearby = QSpinBox()
        self.spin_nearby.setRange(1, 1000)
        self.spin_nearby.setSuffix(" systems")
        self.spin_nearby.setValue(db.get_setting_int("nearby_refresh_count", 50))
        self.spin_nearby.setToolTip(
            "How many of the closest systems 📍 Refresh Nearby updates via "
            "the live EDSM API (2 requests per system; EDSM allows ~360 "
            "requests/hour)"
        )
        form.addRow("Refresh Nearby count:", self.spin_nearby)

        self.spin_refresh_h = QSpinBox()
        self.spin_refresh_h.setRange(1, 720)
        self.spin_refresh_h.setSuffix(" hours")
        self.spin_refresh_h.setValue(db.get_setting_int("meta_refresh_hours", 24))
        self.spin_refresh_h.setToolTip(
            "Cached EDSM metadata older than this is considered stale and "
            "eligible for 📍 Refresh Nearby"
        )
        form.addRow("Refresh meta older than:", self.spin_refresh_h)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        autoskip.save_config(
            self.chk_enabled.isChecked(),
            self.spin_days.value(),
            self.spin_traffic.value(),
        )
        db.set_setting("nightly_enabled", 1 if self.chk_nightly.isChecked() else 0)
        db.set_setting("nearby_refresh_count", self.spin_nearby.value())
        db.set_setting("meta_refresh_hours", self.spin_refresh_h.value())
        self.accept()


class AutoSkipPreviewDialog(QDialog):
    """
    Preview + confirm dialog. ``candidates`` is a list of sqlite3.Row from
    db.get_autoskip_candidates(). After exec(), read:

      .selected  — list of candidate dicts the user confirmed for skipping
      .to_pin    — list of system names the user unchecked (auto-pin these)
    """

    def __init__(self, candidates: list, current_pos: Optional[tuple] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Auto-Skip Preview")
        self.setMinimumSize(720, 480)
        self.selected: list[dict] = []
        self.to_pin: list[str] = []
        self._candidates = [dict(c) for c in candidates]

        layout = QVBoxLayout(self)

        cfg = autoskip.get_config()
        summary = QLabel(
            f"{len(self._candidates)} system(s) have a Very Low or Low "
            f"first-footfall chance (traffic in the last 24 h, or EDSM "
            f"updated ≤ {cfg['max_updated_days']} days ago with traffic "
            f"≥ {cfg['min_traffic']}/week).\n"
            "Untick any you want to KEEP — they will be pinned (📌) and "
            "never suggested again."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(
            ["System", "FF Chance", "EDSM Updated", "Traffic /24h",
             "Traffic /wk", "Open Planets", "Distance"]
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)

        for c in self._candidates:
            item = QTreeWidgetItem(self.tree)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setText(0, c["system_name"])
            tier = autoskip.ff_chance(c)
            item.setText(
                1,
                autoskip.FF_CHANCE_LABELS.get(tier, "?")
                if tier is not None else "?"
            )
            item.setText(2, (c.get("edsm_updated_at") or "?")[:10])
            day = c.get("traffic_day")
            item.setText(3, str(day) if day is not None else "?")
            week = c.get("traffic_week")
            item.setText(4, str(week) if week is not None else "?")
            item.setText(5, str(c.get("open_count") or 0))
            if current_pos is not None and c.get("x") is not None:
                d = math.sqrt(
                    (c["x"] - current_pos[0]) ** 2
                    + (c["y"] - current_pos[1]) ** 2
                    + (c["z"] - current_pos[2]) ** 2
                )
                item.setText(6, f"{d:,.1f} ly")
            else:
                item.setText(6, "—")

        for col in range(7):
            self.tree.resizeColumnToContents(col)
        layout.addWidget(self.tree)

        # Check-all / uncheck-all convenience row
        row = QHBoxLayout()
        btn_all = QPushButton("Check all")
        btn_none = QPushButton("Uncheck all")
        btn_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        btn_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addStretch()
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Skip checked systems"
        )
        buttons.accepted.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _set_all(self, state: Qt.CheckState) -> None:
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, state)

    def _confirm(self) -> None:
        self.selected = []
        self.to_pin = []
        for i, c in enumerate(self._candidates):
            item = self.tree.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                self.selected.append(c)
            else:
                self.to_pin.append(c["system_name"])
        self.accept()

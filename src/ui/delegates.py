"""
delegates.py — Custom QStyledItemDelegate subclasses for inline planet editing.

Three delegate types cover all editable column types:

  ReadOnlyDelegate  — blocks editing entirely (Name, FF, Stratum columns)
  SpinBoxDelegate   — integer spinbox (No of Biologicals)
  ComboBoxDelegate  — dropdown (Status)

Notes column uses Qt's built-in QStyledItemDelegate (plain QLineEdit).

All delegates check `index.parent().isValid()` before creating an editor so
that system (top-level) rows are never accidentally editable.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QSpinBox, QStyledItemDelegate


class ReadOnlyDelegate(QStyledItemDelegate):
    """Prevents cell editing — installed on Name and checkbox columns."""

    def createEditor(self, parent, option, index):  # noqa: ARG002
        return None


class SpinBoxDelegate(QStyledItemDelegate):
    """Inline QSpinBox editor for integer columns (e.g. No of Biologicals)."""

    def __init__(self, min_val: int = 0, max_val: int = 20, parent=None):
        super().__init__(parent)
        self._min = min_val
        self._max = max_val

    def createEditor(self, parent, option, index):  # noqa: ARG002
        if not index.parent().isValid():
            return None  # Never edit system rows
        editor = QSpinBox(parent)
        editor.setRange(self._min, self._max)
        editor.setFrame(False)
        return editor

    def setEditorData(self, editor: QSpinBox, index):
        value = index.data(Qt.ItemDataRole.EditRole)
        try:
            editor.setValue(int(value))
        except (TypeError, ValueError):
            editor.setValue(self._min)

    def setModelData(self, editor: QSpinBox, model, index):
        model.setData(index, editor.value(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):  # noqa: ARG002
        editor.setGeometry(option.rect)


class ComboBoxDelegate(QStyledItemDelegate):
    """Inline QComboBox editor for the Status column."""

    def __init__(self, choices: list[str], parent=None):
        super().__init__(parent)
        self._choices = choices

    def createEditor(self, parent, option, index):  # noqa: ARG002
        if not index.parent().isValid():
            return None  # Never edit system rows
        editor = QComboBox(parent)
        editor.addItems(self._choices)
        editor.setFrame(False)
        return editor

    def setEditorData(self, editor: QComboBox, index):
        value = index.data(Qt.ItemDataRole.DisplayRole) or ""
        idx = editor.findText(value)
        if idx >= 0:
            editor.setCurrentIndex(idx)

    def setModelData(self, editor: QComboBox, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):  # noqa: ARG002
        editor.setGeometry(option.rect)

"""
main.py — Entry point for Elite Tectonica.

Run with:
    uv run python src/main.py
or (with venv activated):
    python src/main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is on sys.path so sibling modules (db, importer, etc.) resolve
# whether this file is run directly or via uv run.
_SRC = Path(__file__).parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import db
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow


def _elite_dark_palette() -> QPalette:
    """
    A dark palette loosely inspired by Elite Dangerous's HUD.
    Highlight colour is the game's signature amber-orange.
    """
    p = QPalette()
    # Backgrounds
    p.setColor(QPalette.ColorRole.Window,         QColor(28, 28, 28))
    p.setColor(QPalette.ColorRole.Base,           QColor(18, 18, 18))
    p.setColor(QPalette.ColorRole.AlternateBase,  QColor(34, 34, 34))
    p.setColor(QPalette.ColorRole.Button,         QColor(42, 42, 42))
    p.setColor(QPalette.ColorRole.Dark,           QColor(12, 12, 12))
    p.setColor(QPalette.ColorRole.Mid,            QColor(24, 24, 24))
    p.setColor(QPalette.ColorRole.Midlight,       QColor(50, 50, 50))
    # Text
    p.setColor(QPalette.ColorRole.WindowText,     QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.Text,           QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.ButtonText,     QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.BrightText,     QColor(255, 255, 255))
    # Highlight — Elite Dangerous amber
    p.setColor(QPalette.ColorRole.Highlight,      QColor(220, 110, 20))
    p.setColor(QPalette.ColorRole.HighlightedText,QColor(0, 0, 0))
    # Tooltips
    p.setColor(QPalette.ColorRole.ToolTipBase,    QColor(42, 42, 42))
    p.setColor(QPalette.ColorRole.ToolTipText,    QColor(220, 220, 220))
    # Links
    p.setColor(QPalette.ColorRole.Link,           QColor(255, 160, 50))
    p.setColor(QPalette.ColorRole.LinkVisited,    QColor(200, 120, 30))
    return p


def main() -> None:
    db.init_db()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")          # consistent cross-platform rendering
    app.setPalette(_elite_dark_palette())

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

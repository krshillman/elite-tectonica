"""
main.py — Root-level entry point for Elite Tectonica.

Run with:
    uv run python main.py
or (with venv activated):
    python main.py
"""

import sys
from pathlib import Path

# Add src/ to sys.path so the application modules resolve correctly.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from main import main  # noqa: E402  (src/main.py)

if __name__ == "__main__":
    main()

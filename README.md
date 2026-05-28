# Elite Tectonica

A desktop companion app for **Elite Dangerous** — track, archive, and explore planetary geology data across your fleet carrier expeditions.

Built with Python, PyQt6, SQLite, and Parquet.

---

## Features

- Import and store geological survey data from Elite Dangerous journals
- Browse planets and their geological sites in a tree view
- Dark HUD-inspired theme matching the Elite Dangerous aesthetic
- Archive survey data to Parquet for long-term storage and analysis

---

## Requirements

- Python 3.9+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

---

## Installation

### With uv (recommended)

```bash
uv sync
```

### With pip

```bash
pip install -e .
```

---

## Running

### With uv

```bash
uv run python main.py
```

### With venv activated

```bash
python main.py
```

---

## Project Structure

```
elite-tectonica/
├── main.py          # Root-level entry point
├── pyproject.toml   # Project metadata and dependencies
├── src/
│   ├── main.py      # Application bootstrap (Qt app, dark palette)
│   ├── db.py        # SQLite database initialisation and queries
│   ├── importer.py  # Journal data importer
│   ├── archiver.py  # Parquet archive writer
│   └── ui/
│       ├── main_window.py   # Main application window
│       ├── planet_tree.py   # Planet/geology tree view widget
│       └── delegates.py     # Custom item delegates
└── docs/
    └── adr/         # Architecture Decision Records
```

---

## Architecture Decisions

See [`docs/adr/`](docs/adr/) for recorded design decisions:

- [0001 — SQLite for working state](docs/adr/0001-sqlite-for-working-state.md)
- [0002 — PyQt6 UI framework](docs/adr/0002-pyqt6-ui-framework.md)
- [0003 — Parquet for archive](docs/adr/0003-parquet-for-archive.md)

---

## License

MIT

"""
db.py — SQLite schema creation and all CRUD operations.

Single source of truth for working state. Every mutation auto-commits.
"""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "tectonica.db"

STATUS_VALUES = ("Pending", "In Progress", "Completed", "Skipped")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create schema if it doesn't exist."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS planets (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                -- Raw CSV columns (all stored, most hidden in UI)
                system_name           TEXT NOT NULL,
                name                  TEXT NOT NULL,
                gravity               REAL,
                distance_to_arrival_ls REAL,
                atmosphere            TEXT,
                distance_ly           REAL,
                subtype               TEXT,
                last_updated_at       TEXT,
                -- Manual label columns (shown in UI)
                first_footfall        INTEGER NOT NULL DEFAULT 0,
                no_of_biologicals     INTEGER,
                contains_stratum      INTEGER NOT NULL DEFAULT 0,
                status                TEXT NOT NULL DEFAULT 'Pending',
                notes                 TEXT,
                -- Metadata
                batch_id              TEXT,
                imported_at           TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (name)
            );

            CREATE INDEX IF NOT EXISTS idx_planets_system
                ON planets (system_name);

            CREATE INDEX IF NOT EXISTS idx_planets_status
                ON planets (status);
        """)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_all_systems(min_planet_count: int = 1) -> list[sqlite3.Row]:
    """
    Return one row per system with aggregate counts.
    Only systems with >= min_planet_count planets are returned.
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                system_name,
                COUNT(*)                                            AS planet_count,
                SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'Pending'   THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'Skipped'   THEN 1 ELSE 0 END) AS skipped_count,
                SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress_count
            FROM planets
            GROUP BY system_name
            HAVING COUNT(*) >= ?
            ORDER BY system_name
        """, (min_planet_count,)).fetchall()
    return rows


def get_planets_for_system(system_name: str) -> list[sqlite3.Row]:
    """Return all planets for a given system, ordered by name."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, name, first_footfall, no_of_biologicals,
                   contains_stratum, status, notes
            FROM planets
            WHERE system_name = ?
            ORDER BY name
        """, (system_name,)).fetchall()
    return rows


def get_totals() -> dict:
    """Return aggregate counts for the status bar."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                COUNT(DISTINCT system_name) AS system_count,
                COUNT(*)                    AS planet_count,
                SUM(CASE WHEN status = 'Completed'   THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'Skipped'     THEN 1 ELSE 0 END) AS skipped,
                SUM(CASE WHEN status = 'Pending'     THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress
            FROM planets
        """).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_planet(
    system_name: str,
    name: str,
    gravity: Optional[float],
    distance_to_arrival_ls: Optional[float],
    atmosphere: Optional[str],
    distance_ly: Optional[float],
    subtype: Optional[str],
    last_updated_at: Optional[str],
    batch_id: Optional[str],
) -> None:
    """
    Insert a new planet, or ignore if it already exists (preserves manual edits).
    """
    with get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO planets
                (system_name, name, gravity, distance_to_arrival_ls,
                 atmosphere, distance_ly, subtype, last_updated_at, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (system_name, name, gravity, distance_to_arrival_ls,
              atmosphere, distance_ly, subtype, last_updated_at, batch_id))


def update_planet_field(planet_id: int, field: str, value) -> None:
    """
    Update a single user-editable field on a planet.
    Only whitelisted fields are allowed to guard against SQL injection.
    """
    allowed = {"first_footfall", "no_of_biologicals", "contains_stratum",
                "status", "notes"}
    if field not in allowed:
        raise ValueError(f"Field '{field}' is not editable")
    with get_connection() as conn:
        conn.execute(
            f"UPDATE planets SET {field} = ? WHERE id = ?",
            (value, planet_id)
        )


def set_system_in_progress(system_name: str) -> None:
    """
    Set all Pending planets in a system to In Progress.
    Called when the user copies the system name for in-game navigation.
    """
    with get_connection() as conn:
        conn.execute("""
            UPDATE planets
            SET status = 'In Progress'
            WHERE system_name = ? AND status = 'Pending'
        """, (system_name,))


def get_archivable_planets() -> list[sqlite3.Row]:
    """Return all Completed + Skipped planets with full column set for archiving."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT system_name, name, gravity, distance_to_arrival_ls,
                   atmosphere, distance_ly, subtype, last_updated_at,
                   first_footfall, no_of_biologicals, contains_stratum,
                   status, notes, batch_id, imported_at
            FROM planets
            WHERE status IN ('Completed', 'Skipped')
        """).fetchall()
    return rows


def get_pending_in_progress_counts() -> dict:
    """Return counts of Pending + In Progress planets for the archive dialog."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN status = 'Pending'     THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress
            FROM planets
        """).fetchone()
    return dict(row)


def delete_completed_skipped() -> int:
    """Delete all Completed + Skipped rows. Returns number of rows removed."""
    with get_connection() as conn:
        cur = conn.execute("""
            DELETE FROM planets WHERE status IN ('Completed', 'Skipped')
        """)
        return cur.rowcount


def delete_all_planets() -> None:
    """Nuclear option — wipe the whole working table."""
    with get_connection() as conn:
        conn.execute("DELETE FROM planets")

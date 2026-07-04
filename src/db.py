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
    # With WAL, synchronous=NORMAL is durable across app crashes and makes
    # bulk writes (dump baseline flushes, imports) far faster than FULL.
    conn.execute("PRAGMA synchronous=NORMAL")
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

            -- Galactic coordinates cache (fetched from EDSM / journal logs).
            -- Used to compute live distances from the commander's current
            -- position, since the CSV distance_ly is frozen at download time.
            CREATE TABLE IF NOT EXISTS system_coords (
                system_name TEXT PRIMARY KEY,
                x           REAL NOT NULL,
                y           REAL NOT NULL,
                z           REAL NOT NULL,
                fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Per-system EDSM metadata: how recently bodies were updated,
            -- who discovered the system, and traffic stats. Used to judge
            -- whether a first footfall is still likely (Auto-Skip rule).
            CREATE TABLE IF NOT EXISTS system_meta (
                system_name      TEXT PRIMARY KEY,
                edsm_updated_at  TEXT,     -- most recent body updateTime on EDSM
                first_discovered TEXT,     -- system discovery date on EDSM
                discovered_by    TEXT,     -- commander credited with discovery
                traffic_day      INTEGER,  -- ships seen in the last 24 hours
                traffic_week     INTEGER,  -- ships seen in the last 7 days
                traffic_total    INTEGER,  -- ships seen all-time
                pinned           INTEGER NOT NULL DEFAULT 0,  -- never auto-skip
                meta_source      TEXT NOT NULL DEFAULT 'api', -- 'api' | 'dump'
                fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Simple key/value store for user preferences.
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # Migration: traffic_day was added after system_meta first shipped
        cols = [r["name"] for r in
                conn.execute("PRAGMA table_info(system_meta)").fetchall()]
        if "traffic_day" not in cols:
            conn.execute(
                "ALTER TABLE system_meta ADD COLUMN traffic_day INTEGER"
            )
        # Migration: meta_source distinguishes rows seeded from the EDSM
        # nightly dump ('dump') from full API fetches ('api'). Pre-existing
        # rows were all API-fetched.
        if "meta_source" not in cols:
            conn.execute(
                "ALTER TABLE system_meta "
                "ADD COLUMN meta_source TEXT NOT NULL DEFAULT 'api'"
            )


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
                p.system_name,
                COUNT(*)                                            AS planet_count,
                SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN status = 'Pending'   THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'Skipped'   THEN 1 ELSE 0 END) AS skipped_count,
                SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress_count,
                c.x, c.y, c.z
            FROM planets p
            LEFT JOIN system_coords c ON c.system_name = p.system_name
            GROUP BY p.system_name
            HAVING COUNT(*) >= ?
            ORDER BY p.system_name
        """, (min_planet_count,)).fetchall()
    return rows


def get_planets_for_system(system_name: str) -> list[sqlite3.Row]:
    """
    Return all planets for a given system, closest to arrival first
    so the next body to visit is always at the top.
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, name, first_footfall, no_of_biologicals,
                   contains_stratum, status, notes, distance_to_arrival_ls
            FROM planets
            WHERE system_name = ?
            ORDER BY distance_to_arrival_ls IS NULL,
                     distance_to_arrival_ls, name
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


# ---------------------------------------------------------------------------
# System coordinates cache
# ---------------------------------------------------------------------------

def get_systems_missing_coords() -> list[str]:
    """Return system names in the working set that have no cached coordinates."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.system_name
            FROM planets p
            LEFT JOIN system_coords c ON c.system_name = p.system_name
            WHERE c.system_name IS NULL
            ORDER BY p.system_name
        """).fetchall()
    return [r["system_name"] for r in rows]


def upsert_system_coords(coords: list[tuple]) -> None:
    """
    Bulk insert/replace coordinates.
    ``coords`` is a list of (system_name, x, y, z) tuples.
    """
    if not coords:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO system_coords (system_name, x, y, z)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (system_name)
            DO UPDATE SET x = excluded.x, y = excluded.y, z = excluded.z,
                          fetched_at = datetime('now')
        """, coords)


def get_system_coords(system_name: str):
    """Return (x, y, z) for a system if cached, else None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT x, y, z FROM system_coords WHERE system_name = ?",
            (system_name,)
        ).fetchone()
    return (row["x"], row["y"], row["z"]) if row else None


# ---------------------------------------------------------------------------
# Settings (key/value store)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "autoskip_enabled": "1",
    "autoskip_max_updated_days": "365",   # EDSM-updated within N days
    "autoskip_min_traffic": "10",         # ships/week threshold
    "nightly_enabled": "1",               # auto-run the nightly dump baseline
    "nearby_refresh_count": "50",         # systems per Refresh Nearby run
    "meta_refresh_hours": "24",           # re-fetch meta older than N hours
}


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return a setting value, falling back to DEFAULT_SETTINGS then default."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row is not None:
        return row["value"]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key: str, value) -> None:
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))


def get_setting_int(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def get_setting_bool(key: str, default: bool = False) -> bool:
    return get_setting_int(key, 1 if default else 0) != 0


# ---------------------------------------------------------------------------
# System metadata (EDSM update recency / discovery / traffic)
# ---------------------------------------------------------------------------

def get_systems_missing_meta() -> list[str]:
    """
    Return system names in the working set with no API-fetched EDSM metadata.
    Rows seeded only from the nightly dump ('dump') count as missing because
    they carry no traffic / discovery data.
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.system_name
            FROM planets p
            LEFT JOIN system_meta m ON m.system_name = p.system_name
            WHERE m.system_name IS NULL OR m.meta_source = 'dump'
            ORDER BY p.system_name
        """).fetchall()
    return [r["system_name"] for r in rows]


def get_systems_missing_meta_with_coords() -> list[sqlite3.Row]:
    """
    Like get_systems_missing_meta but includes cached coordinates (may be
    NULL) so the fetcher can prioritise systems closest to the commander.
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.system_name, c.x, c.y, c.z
            FROM planets p
            LEFT JOIN system_meta m ON m.system_name = p.system_name
            LEFT JOIN system_coords c ON c.system_name = p.system_name
            WHERE m.system_name IS NULL OR m.meta_source = 'dump'
            ORDER BY p.system_name
        """).fetchall()
    return rows


def get_systems_for_meta_refresh(max_age_hours: int) -> list[sqlite3.Row]:
    """
    Systems that would benefit from a live API refresh, restricted to those
    with open (Pending / In Progress) planets and cached coordinates so
    they can be prioritised by distance. A system qualifies when its meta:
      - has never been fetched, or
      - was only seeded from the nightly dump (no traffic data), or
      - is older than ``max_age_hours``.
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.system_name, c.x, c.y, c.z
            FROM planets p
            JOIN system_coords c ON c.system_name = p.system_name
            LEFT JOIN system_meta m ON m.system_name = p.system_name
            WHERE p.status IN ('Pending', 'In Progress')
              AND (
                    m.system_name IS NULL
                 OR m.meta_source = 'dump'
                 OR (julianday('now') - julianday(m.fetched_at)) * 24 >= ?
              )
            ORDER BY p.system_name
        """, (max_age_hours,)).fetchall()
    return rows


def upsert_system_meta(
    system_name: str,
    edsm_updated_at: Optional[str],
    first_discovered: Optional[str],
    discovered_by: Optional[str],
    traffic_day: Optional[int],
    traffic_week: Optional[int],
    traffic_total: Optional[int],
) -> None:
    """Insert or refresh EDSM metadata for a system. Preserves the pinned flag."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO system_meta
                (system_name, edsm_updated_at, first_discovered, discovered_by,
                 traffic_day, traffic_week, traffic_total, meta_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'api')
            ON CONFLICT (system_name) DO UPDATE SET
                edsm_updated_at  = excluded.edsm_updated_at,
                first_discovered = excluded.first_discovered,
                discovered_by    = excluded.discovered_by,
                traffic_day      = excluded.traffic_day,
                traffic_week     = excluded.traffic_week,
                traffic_total    = excluded.traffic_total,
                meta_source      = 'api',
                fetched_at       = datetime('now')
        """, (system_name, edsm_updated_at, first_discovered, discovered_by,
              traffic_day, traffic_week, traffic_total))


def get_working_system_names() -> set[str]:
    """All distinct system names in the working set (for dump matching)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT system_name FROM planets"
        ).fetchall()
    return {r["system_name"] for r in rows}


def apply_dump_baseline(entries: list[tuple]) -> None:
    """
    Bulk-apply nightly-dump recency data.
    ``entries`` is a list of (system_name, edsm_updated_at) tuples.

    Inserts rows tagged meta_source='dump'; for existing rows it only
    advances edsm_updated_at (never regresses it) and never overwrites
    traffic / discovery data or the meta_source of API-fetched rows.
    """
    if not entries:
        return
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO system_meta (system_name, edsm_updated_at, meta_source)
            VALUES (?, ?, 'dump')
            ON CONFLICT (system_name) DO UPDATE SET
                edsm_updated_at = CASE
                    WHEN edsm_updated_at IS NULL
                         OR excluded.edsm_updated_at > edsm_updated_at
                    THEN excluded.edsm_updated_at
                    ELSE edsm_updated_at
                END
        """, entries)


def get_system_meta_map() -> dict[str, dict]:
    """Return {system_name: meta dict} for every system with cached metadata."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM system_meta").fetchall()
    return {r["system_name"]: dict(r) for r in rows}


def set_system_pinned(system_name: str, pinned: bool) -> None:
    """Pin/unpin a system. Pinned systems are never touched by Auto-Skip."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO system_meta (system_name, pinned) VALUES (?, ?)
            ON CONFLICT (system_name) DO UPDATE SET pinned = excluded.pinned
        """, (system_name, 1 if pinned else 0))


def get_autoskip_candidates(max_updated_days: int, min_traffic: int) -> list[sqlite3.Row]:
    """
    Return systems matching the auto-skip rule:
      - not pinned
      - EITHER: EDSM body data updated within ``max_updated_days``
                AND weekly traffic >= ``min_traffic``
        OR:     any traffic in the last 24 hours (someone is there right now)
      - at least one Pending / In Progress planet remaining
    """
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                p.system_name,
                m.edsm_updated_at, m.first_discovered, m.discovered_by,
                m.traffic_day, m.traffic_week, m.traffic_total,
                COUNT(*) AS planet_count,
                SUM(CASE WHEN p.status IN ('Pending', 'In Progress')
                    THEN 1 ELSE 0 END) AS open_count,
                c.x, c.y, c.z
            FROM planets p
            JOIN system_meta m ON m.system_name = p.system_name
            LEFT JOIN system_coords c ON c.system_name = p.system_name
            WHERE m.pinned = 0
              AND (
                    (m.edsm_updated_at IS NOT NULL
                     AND julianday('now') - julianday(m.edsm_updated_at) <= ?
                     AND COALESCE(m.traffic_week, 0) >= ?)
                 OR COALESCE(m.traffic_day, 0) > 0
              )
            GROUP BY p.system_name
            HAVING open_count > 0
            ORDER BY p.system_name
        """, (max_updated_days, min_traffic)).fetchall()
    return rows


def skip_system_planets(system_name: str, note: str) -> int:
    """
    Set all Pending / In Progress planets in a system to Skipped, appending
    ``note`` to each planet's notes (deduplicated). Returns rows changed.
    """
    with get_connection() as conn:
        cur = conn.execute("""
            UPDATE planets
            SET status = 'Skipped',
                notes = CASE
                    WHEN notes IS NULL OR notes = '' THEN ?
                    WHEN instr(notes, ?) > 0 THEN notes
                    ELSE notes || ' | ' || ?
                END
            WHERE system_name = ? AND status IN ('Pending', 'In Progress')
        """, (note, note, note, system_name))
        return cur.rowcount


def append_system_note(system_name: str, note: str) -> None:
    """
    Append ``note`` to every open (Pending / In Progress) planet in a system
    without changing status. Skips planets that already carry the note.
    """
    with get_connection() as conn:
        conn.execute("""
            UPDATE planets
            SET notes = CASE
                WHEN notes IS NULL OR notes = '' THEN ?
                ELSE notes || ' | ' || ?
            END
            WHERE system_name = ?
              AND status IN ('Pending', 'In Progress')
              AND (notes IS NULL OR instr(notes, ?) = 0)
        """, (note, note, system_name, note))


# ---------------------------------------------------------------------------
# Journal auto-fill (update planets by in-game body name)
# ---------------------------------------------------------------------------

def mark_planet_in_progress_by_name(body_name: str) -> int:
    """Touchdown auto-fill: Pending → In Progress. Returns rows changed."""
    with get_connection() as conn:
        cur = conn.execute("""
            UPDATE planets SET status = 'In Progress'
            WHERE name = ? AND status = 'Pending'
        """, (body_name,))
        return cur.rowcount


def set_planet_bios_by_name(body_name: str, count: int) -> int:
    """Bio-signal auto-fill from FSS/DSS scans. Returns rows changed."""
    with get_connection() as conn:
        cur = conn.execute("""
            UPDATE planets SET no_of_biologicals = ?
            WHERE name = ? AND (no_of_biologicals IS NULL
                                OR no_of_biologicals <> ?)
        """, (count, body_name, count))
        return cur.rowcount


def set_planet_stratum_by_name(body_name: str) -> int:
    """ScanOrganic (Stratum) auto-fill. Returns rows changed."""
    with get_connection() as conn:
        cur = conn.execute("""
            UPDATE planets SET contains_stratum = 1
            WHERE name = ? AND contains_stratum = 0
        """, (body_name,))
        return cur.rowcount


def count_coords_cached() -> dict:
    """Return {'total_systems': n, 'with_coords': n} for status display."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                COUNT(DISTINCT p.system_name) AS total_systems,
                COUNT(DISTINCT c.system_name) AS with_coords
            FROM planets p
            LEFT JOIN system_coords c ON c.system_name = p.system_name
        """).fetchone()
    return dict(row)

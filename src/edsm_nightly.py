"""
edsm_nightly.py — Once-per-day EDSM nightly-dump baseline.

Why
---
EDSM's live API is budgeted at ~360 requests/hour (leaky bucket), which is
far too small for bulk imports of thousands of systems. EDSM also publishes
full nightly JSON dumps (https://www.edsm.net/en/nightly-dumps). Downloading
one dump and querying it locally replaces thousands of API calls — the live
API budget is then reserved for refreshing systems *near* the commander
(traffic + discovery data), which the closest-first ordering already handles.

Which dumps
-----------
Two dumps are used, both applied the same way:

``systemsWithCoordinates.json.gz`` (FULL, several GB) — every known system
with its coordinates and record date. Run ONCE as a "full baseline" so
every working-set system gets a record date (old/pre-Odyssey dates feed
the "Very High FF chance" tiers) and coordinates. Only re-downloaded when
the user explicitly requests a Full Refresh.

``systemsWithCoordinates7days.json.gz`` (tens of MB) — systems updated in
the last 7 days. Runs daily to keep the baseline current:

  * A working-set system that appears in the dump was updated within the
    last week → someone has been there very recently → its
    ``edsm_updated_at`` baseline is advanced (feeds the FF-chance tiers).
  * Entries carry coordinates, so matched systems get their coords cached
    for free — saving coords API calls too.

Dump entries carry NO traffic/discovery data, so rows written here are
tagged ``meta_source='dump'`` and still count as "missing" for the live
API fetchers, which later upgrade them to ``meta_source='api'``.

Performance
-----------
The full dump holds >100 million entries while the working set holds a few
thousand names, so the pipeline is built to touch matching lines only:

  * The gzip stream is decompressed **directly off the HTTP response** —
    no multi-GB temp file is written to disk and the data is only read once.
  * Each line gets a cheap ``str.find``-based name pre-filter; full
    ``json.loads`` runs ONLY for lines whose system name is in the working
    set (or the rare line with escape sequences). This skips the JSON
    parser for >99.9% of the dump and is the difference between minutes
    and the better part of an hour on the full dump.

Scheduling
----------
``is_due()`` is true at most once per UTC day (tracked via the
``nightly_last_run`` setting) and only when ``nightly_enabled`` is on.
The main window auto-runs the baseline in the background on startup;
it can also be triggered manually (force=True re-runs same-day).
"""

from __future__ import annotations

import gzip
import io
import json
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

import db

DUMP_URL = "https://www.edsm.net/dump/systemsWithCoordinates7days.json.gz"
FULL_DUMP_URL = "https://www.edsm.net/dump/systemsWithCoordinates.json.gz"
TIMEOUT_S = 60
USER_AGENT = "EliteTectonica/1.0 (github.com/krshillman/elite-tectonica)"

_NAME_KEY = '"name":"'
_PROGRESS_EVERY = 250_000     # lines between progress callbacks
_FLUSH_EVERY = 50_000         # matched entries between DB flushes

# progress(message) -> bool; returning False cancels the run
ProgressFn = Callable[[str], bool]


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def is_due() -> bool:
    """True when the baseline is enabled and hasn't run yet today (UTC)."""
    if not db.get_setting_bool("nightly_enabled", True):
        return False
    return db.get_setting("nightly_last_run") != _today_utc()


def last_run() -> Optional[str]:
    return db.get_setting("nightly_last_run")


def full_baseline_last_run() -> Optional[str]:
    """Date the one-off FULL dump baseline last ran, or None if never."""
    return db.get_setting("full_baseline_last_run")


class _CountingReader(io.RawIOBase):
    """Wraps the HTTP response so download progress can be reported."""

    def __init__(self, raw):
        self._raw = raw
        self.bytes_read = 0

    def readable(self) -> bool:  # pragma: no cover - io plumbing
        return True

    def readinto(self, b) -> int:
        chunk = self._raw.read(len(b))
        n = len(chunk)
        b[:n] = chunk
        self.bytes_read += n
        return n


def _extract_name(line: str) -> Optional[str]:
    """
    Cheap name pre-filter: slice the value of ``"name":"..."`` out of the
    raw JSON line without invoking the JSON parser. Returns None when no
    name key is present (array brackets, malformed lines) and the sentinel
    ``"\\"`` fallback marker when escape sequences make slicing unsafe.
    """
    i = line.find(_NAME_KEY)
    if i == -1:
        return None
    start = i + len(_NAME_KEY)
    end = line.find('"', start)
    if end == -1:
        return None
    name = line[start:end]
    # An escape sequence means the quote we found may be inside the value;
    # signal the caller to fall back to a full JSON parse.
    if "\\" in name:
        return "\\"
    return name


def _parse_stream(
    text: io.TextIOBase,
    counting: _CountingReader,
    total_bytes: int,
    progress: Optional[ProgressFn],
) -> Optional[dict]:
    """
    Stream-parse dump lines and apply matches against the working set.
    Returns a summary dict, or None if cancelled.
    """
    working = db.get_working_system_names()
    meta_entries: list[tuple] = []    # (system_name, edsm_updated_at)
    coord_entries: list[tuple] = []   # (system_name, x, y, z)
    scanned = 0
    matched = 0
    coords_n = 0

    total_mb = f"/{total_bytes / (1 << 20):.0f}" if total_bytes else ""

    def _flush() -> None:
        nonlocal matched, coords_n
        db.apply_dump_baseline(meta_entries)
        db.upsert_system_coords(coord_entries)
        matched += len(meta_entries)
        coords_n += len(coord_entries)
        meta_entries.clear()
        coord_entries.clear()

    for line in text:
        scanned += 1
        if progress is not None and scanned % _PROGRESS_EVERY == 0:
            mb = counting.bytes_read / (1 << 20)
            if progress(
                f"scanning… {scanned:,} systems ({mb:.0f}{total_mb} MB)"
            ) is False:
                return None

        # Fast path: skip lines whose system name isn't in the working set
        # without paying for json.loads (>99.9% of the full dump).
        name = _extract_name(line)
        if name is None:
            continue
        if name != "\\" and name not in working:
            continue

        try:
            entry = json.loads(line.strip().rstrip(","))
        except json.JSONDecodeError:
            continue
        name = entry.get("name")
        if name not in working:
            continue

        date = entry.get("date")
        if date:
            meta_entries.append((name, date))
        c = entry.get("coords") or {}
        if all(k in c for k in ("x", "y", "z")):
            coord_entries.append((name, c["x"], c["y"], c["z"]))

        # Flush periodically so a cancel/network drop keeps partial progress.
        if len(meta_entries) >= _FLUSH_EVERY:
            _flush()

    _flush()
    return {"scanned": scanned, "matched": matched, "coords": coords_n}


def run_baseline(
    progress: Optional[ProgressFn] = None, force: bool = False
) -> dict:
    """
    Stream today's 7-day dump (if due) and apply it to the working set.

    Returns a summary dict:
      {"ran": bool, "scanned": n, "matched": n, "coords": n,
       "error": str | None, "cancelled": bool}
    """
    if not force and not is_due():
        return {"ran": False, "scanned": 0, "matched": 0, "coords": 0,
                "error": None, "cancelled": False}
    return _run_dump(DUMP_URL, progress, on_success=lambda:
                     db.set_setting("nightly_last_run", _today_utc()))


def run_full_baseline(progress: Optional[ProgressFn] = None) -> dict:
    """
    One-off (or explicit Full Refresh): stream the FULL systems dump
    (several GB compressed — expect a long run) and apply it, giving
    every working-set system a record date and coordinates without any
    API calls. Same summary dict shape as run_baseline().
    """
    def _mark() -> None:
        db.set_setting("full_baseline_last_run", _today_utc())
        # The full dump supersedes today's 7-day dump.
        db.set_setting("nightly_last_run", _today_utc())

    return _run_dump(FULL_DUMP_URL, progress, on_success=_mark)


def _run_dump(
    url: str,
    progress: Optional[ProgressFn],
    on_success: Callable[[], None],
) -> dict:
    """
    Shared stream pipeline for both dumps:
      HTTP response → byte counter → gzip decompress → line parse → DB.
    Nothing is written to disk; the dump is consumed in a single pass.
    """
    summary = {"ran": False, "scanned": 0, "matched": 0, "coords": 0,
               "error": None, "cancelled": False}

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            counting = _CountingReader(resp)
            buffered = io.BufferedReader(counting, buffer_size=1 << 20)
            gz = gzip.GzipFile(fileobj=buffered)
            text = io.TextIOWrapper(gz, encoding="utf-8")

            result = _parse_stream(text, counting, total, progress)
            if result is None:
                summary["cancelled"] = True
                return summary

        summary.update(result)
        summary["ran"] = True
        on_success()
    except Exception as exc:  # network errors, corrupt gz, etc.
        summary["error"] = str(exc)
        print(f"[nightly] WARNING: baseline failed: {exc}")

    return summary

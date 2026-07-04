"""
journal.py — Read Elite Dangerous journal logs to find the commander's
current star system, plus a live event watcher for auto-filling labels.

The game writes newline-delimited JSON events to:
    %USERPROFILE%\\Saved Games\\Frontier Developments\\Elite Dangerous\\Journal.*.log

The current location is the most recent FSDJump / Location / CarrierJump
event. All three carry "StarSystem" and "StarPos" [x, y, z] (in LY).

The JournalWatcher additionally tails the newest journal file incrementally
and yields auto-fill events:

  Touchdown          → planet status Pending → In Progress ("BodyName")
  FSSBodySignals /
  SAASignalsFound    → biological signal count for a body ("$SAA_SignalType_Biological;")
  ScanOrganic        → Stratum Tectonicas detection ("Species" contains "Stratum")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple, Optional

JOURNAL_DIR = (
    Path.home() / "Saved Games" / "Frontier Developments" / "Elite Dangerous"
)

_LOCATION_EVENTS = ("FSDJump", "Location", "CarrierJump")

# Cheap substring pre-filter so we don't json.loads every line
_EVENT_MARKERS = tuple(f'"event":"{e}"' for e in _LOCATION_EVENTS)

_WATCH_EVENTS = ("Touchdown", "FSSBodySignals", "SAASignalsFound", "ScanOrganic")
_WATCH_MARKERS = tuple(f'"event":"{e}"' for e in _WATCH_EVENTS)

_BIO_SIGNAL_TYPE = "$SAA_SignalType_Biological;"


class Location(NamedTuple):
    system_name: str
    pos: Optional[tuple]  # (x, y, z) in LY, or None if StarPos missing


def read_current_location(journal_dir: Path = JOURNAL_DIR) -> Optional[Location]:
    """
    Return the commander's current location, or None if it can't be determined
    (no journal directory, no logs, or no location events yet).

    Scans the most recent journal files (newest first) and returns the last
    location event found in the first file that contains one.
    """
    if not journal_dir.is_dir():
        return None

    try:
        files = sorted(
            journal_dir.glob("Journal*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    # The newest file may not contain a location event yet (e.g. game just
    # launched), so fall back through a few recent files.
    for path in files[:5]:
        loc = _scan_file(path)
        if loc is not None:
            return loc
    return None


def _scan_file(path: Path) -> Optional[Location]:
    """Return the LAST location event in a journal file, or None."""
    last: Optional[Location] = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(marker in line for marker in _EVENT_MARKERS):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") not in _LOCATION_EVENTS:
                    continue
                system = event.get("StarSystem")
                if not system:
                    continue
                star_pos = event.get("StarPos")
                pos = tuple(star_pos) if isinstance(star_pos, list) and len(star_pos) == 3 else None
                last = Location(system, pos)
    except OSError:
        return None
    return last


# ---------------------------------------------------------------------------
# Live event watcher (auto-fill)
# ---------------------------------------------------------------------------

class AutoFillEvent(NamedTuple):
    kind: str                  # "touchdown" | "bio_signals" | "stratum"
    body_name: str
    count: Optional[int] = None  # bio signal count (bio_signals only)


class JournalWatcher:
    """
    Incrementally tails the newest journal file and extracts auto-fill
    events. Call ``poll()`` periodically (e.g. from a QTimer); it returns
    the list of new AutoFillEvents since the last call.

    On first poll the watcher seeks to the END of the current journal so
    that historic events are never replayed into the database.
    """

    def __init__(self, journal_dir: Path = JOURNAL_DIR):
        self._dir = journal_dir
        self._path: Optional[Path] = None
        self._offset = 0

    def poll(self) -> list[AutoFillEvent]:
        newest = self._newest_journal()
        if newest is None:
            return []

        if self._path is None or newest != self._path:
            # New session file — start from its end (skip history) the very
            # first time, but from the beginning of a *rotated* new file so
            # no live events are missed mid-session.
            first_attach = self._path is None
            self._path = newest
            self._offset = newest.stat().st_size if first_attach else 0

        events: list[AutoFillEvent] = []
        try:
            with open(self._path, encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                for line in fh:
                    if not line.endswith("\n"):
                        break  # partial line still being written — retry later
                    self._offset += len(line.encode("utf-8", errors="replace"))
                    ev = self._parse_line(line)
                    if ev is not None:
                        events.append(ev)
        except OSError:
            return []
        return events

    def _newest_journal(self) -> Optional[Path]:
        try:
            files = sorted(
                self._dir.glob("Journal*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        return files[0] if files else None

    @staticmethod
    def _parse_line(line: str) -> Optional[AutoFillEvent]:
        if not any(marker in line for marker in _WATCH_MARKERS):
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        kind = event.get("event")

        if kind == "Touchdown":
            body = event.get("Body")
            if body and event.get("OnPlanet", True):
                return AutoFillEvent("touchdown", body)

        elif kind in ("FSSBodySignals", "SAASignalsFound"):
            body = event.get("BodyName")
            if body:
                for sig in event.get("Signals") or []:
                    if sig.get("Type") == _BIO_SIGNAL_TYPE:
                        return AutoFillEvent("bio_signals", body,
                                             count=sig.get("Count"))

        elif kind == "ScanOrganic":
            body = event.get("Body")  # may be an int BodyID in some builds
            species = (event.get("Species_Localised")
                       or event.get("Species") or "")
            if "stratum" in species.lower():
                name = body if isinstance(body, str) else event.get("BodyName")
                if name:
                    return AutoFillEvent("stratum", name)

        return None

"""
coords.py — Fetch galactic coordinates for systems from the EDSM API.

The Spansh CSV export only carries "Distance (LY)" measured from the search
reference point at download time — useless once the commander moves. To
compute live distances we need each system's absolute (x, y, z) coordinates,
which EDSM provides. Results are cached in the ``system_coords`` table so
each system is only ever fetched once.

API: https://www.edsm.net/api-v1/systems (supports multiple systemName[]
query params per request, so we batch to stay polite).
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

import db
import edsm_backoff

EDSM_URL = "https://www.edsm.net/api-v1/systems"
BATCH_SIZE = 100          # systems per request (keeps URL well under limits)
REQUEST_DELAY_S = 0.5     # courtesy delay between requests
TIMEOUT_S = 20
USER_AGENT = "EliteTectonica/1.0 (github.com/krshillman/elite-tectonica)"


def distance_ly(a: tuple, b: tuple) -> float:
    """Euclidean distance between two (x, y, z) galactic positions in LY."""
    return math.sqrt(
        (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
    )


def fetch_batch(system_names: list[str]) -> list[tuple]:
    """
    Query EDSM for one batch of system names.
    Returns a list of (system_name, x, y, z) tuples for systems found.
    """
    params = [("systemName[]", name) for name in system_names]
    params.append(("showCoordinates", "1"))
    url = EDSM_URL + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    results: list[tuple] = []
    for entry in payload:
        name = entry.get("name")
        c = entry.get("coords") or {}
        if name and all(k in c for k in ("x", "y", "z")):
            results.append((name, c["x"], c["y"], c["z"]))
    return results


def fetch_missing_coords(
    progress: Optional[Callable[[int, int], bool]] = None,
) -> dict:
    """
    Fetch coordinates for every system in the working set that has none
    cached, saving to SQLite as each batch completes (so partial progress
    is never lost).

    ``progress(done, total)`` is called after each batch; if it returns
    False the fetch stops early.

    Returns {"requested": n, "fetched": n, "errors": n}.
    """
    missing = db.get_systems_missing_coords()
    total = len(missing)
    fetched = 0
    errors = 0
    backoff_level = 0

    i = 0
    while i < total:
        batch = missing[i:i + BATCH_SIZE]
        try:
            results = fetch_batch(batch)
            db.upsert_system_coords(results)
            fetched += len(results)
            backoff_level = 0  # success → reset backoff
        except Exception as exc:
            if edsm_backoff.is_rate_limited(exc):
                backoff_level += 1
                if not edsm_backoff.backoff_wait(
                    backoff_level, "coords", progress, i, total
                ):
                    break  # cancelled during backoff
                continue   # retry the same batch
            errors += 1
            print(f"[coords] WARNING: batch failed: {exc}")

        i += BATCH_SIZE

        if progress is not None:
            if progress(min(i, total), total) is False:
                break

        if i < total:
            time.sleep(REQUEST_DELAY_S)

    return {"requested": total, "fetched": fetched, "errors": errors}

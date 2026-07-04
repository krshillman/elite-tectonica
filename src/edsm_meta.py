"""
edsm_meta.py — Fetch per-system metadata from EDSM: body update recency,
discovery info, and traffic stats.

Why
---
A system whose bodies were updated on EDSM recently, and which sees regular
traffic, has almost certainly had its first footfalls taken already — so it
is a candidate for auto-skipping. Conversely a system nobody has visited in
years is prime first-footfall territory.

Endpoints (one system per request, so this is slower than the coords fetch)
---------------------------------------------------------------------------
  https://www.edsm.net/api-system-v1/bodies?systemName=X
      → per-body "updateTime"; we keep the most recent one.

  https://www.edsm.net/api-system-v1/traffic?systemName=X
      → {"traffic": {"total": n, "week": n, "day": n},
         "discovery": {"commander": "...", "date": "..."}}

Results are cached in the ``system_meta`` table; only systems with no cached
metadata are fetched (use force=True to refresh everything).

When the commander's position is known, systems are fetched closest-first so
the data most relevant to the next few jumps arrives immediately, while
far-away systems trickle in later (lazy priority).

API budget strategy
-------------------
EDSM's live API is leaky-bucket limited (~360 requests/hour), which is far
too small for bulk work. The nightly-dump baseline (edsm_nightly.py) handles
bulk recency data locally; ``fetch_nearby_meta`` spends the precious live
budget only on the N systems closest to the commander, keeping their
traffic / discovery data fresh. Rate limiting shows up as an HTTP 429 OR,
historically, as an empty 200 response — both are handled.
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

BODIES_URL  = "https://www.edsm.net/api-system-v1/bodies"
TRAFFIC_URL = "https://www.edsm.net/api-system-v1/traffic"
REQUEST_DELAY_S = 0.4     # ~2 systems/sec (2 requests each) — polite pace
TIMEOUT_S = 20
USER_AGENT = "EliteTectonica/1.0 (github.com/krshillman/elite-tectonica)"


def _get_json(url: str, params: dict) -> Optional[dict | list]:
    """
    GET a JSON payload; returns None for "no data" responses ([] / {}).

    Raises ``edsm_backoff.RateLimitError`` on a completely empty body —
    EDSM's historic way of signalling the rate limit (a 429 is raised by
    urllib as HTTPError and handled by the caller's backoff loop).
    """
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read().decode("utf-8")
    if not raw.strip():
        raise edsm_backoff.RateLimitError("empty response body (rate limited)")
    if raw in ("[]", "{}"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def fetch_system_meta(system_name: str) -> dict:
    """
    Fetch metadata for one system. Returns a dict with keys:
      edsm_updated_at, first_discovered, discovered_by,
      traffic_day, traffic_week, traffic_total
    Missing values are None (system may be unknown to EDSM).
    """
    meta = {
        "edsm_updated_at": None,
        "first_discovered": None,
        "discovered_by": None,
        "traffic_day": None,
        "traffic_week": None,
        "traffic_total": None,
    }

    # ── Bodies → most recent updateTime ──
    bodies_payload = _get_json(BODIES_URL, {"systemName": system_name})
    if isinstance(bodies_payload, dict):
        update_times = [
            b["updateTime"]
            for b in bodies_payload.get("bodies") or []
            if b.get("updateTime")
        ]
        if update_times:
            meta["edsm_updated_at"] = max(update_times)

    time.sleep(REQUEST_DELAY_S)

    # ── Traffic + discovery ──
    traffic_payload = _get_json(TRAFFIC_URL, {"systemName": system_name})
    if isinstance(traffic_payload, dict):
        traffic = traffic_payload.get("traffic") or {}
        if "day" in traffic:
            meta["traffic_day"] = traffic.get("day")
        if "week" in traffic:
            meta["traffic_week"] = traffic.get("week")
        if "total" in traffic:
            meta["traffic_total"] = traffic.get("total")
        discovery = traffic_payload.get("discovery") or {}
        meta["first_discovered"] = discovery.get("date")
        meta["discovered_by"] = discovery.get("commander")

    return meta


def fetch_missing_meta(
    progress: Optional[Callable[[int, int], bool]] = None,
    force: bool = False,
    current_pos: Optional[tuple] = None,
) -> dict:
    """
    Fetch metadata for every system in the working set that has none cached,
    saving to SQLite as each system completes (partial progress is kept).

    ``progress(done, total)`` is called after each system; returning False
    stops the fetch early.

    When ``current_pos`` (x, y, z in LY) is given, systems are fetched
    closest-first so nearby systems get fresh data immediately; systems
    without cached coordinates go last.

    Returns {"requested": n, "fetched": n, "errors": n}.
    """
    if force:
        targets = sorted({r["system_name"] for r in db.get_all_systems()})
    else:
        rows = db.get_systems_missing_meta_with_coords()
        if current_pos is not None:
            def sort_key(r):
                if r["x"] is None:
                    return (1, 0.0, r["system_name"])
                d = math.sqrt(
                    (r["x"] - current_pos[0]) ** 2
                    + (r["y"] - current_pos[1]) ** 2
                    + (r["z"] - current_pos[2]) ** 2
                )
                return (0, d, r["system_name"])
            rows = sorted(rows, key=sort_key)
        targets = [r["system_name"] for r in rows]

    return _fetch_meta_for(targets, progress)


def fetch_nearby_meta(
    current_pos: tuple,
    limit: Optional[int] = None,
    progress: Optional[Callable[[int, int], bool]] = None,
) -> dict:
    """
    Refresh live EDSM metadata for the ``limit`` systems closest to
    ``current_pos`` that still have open planets and stale / dump-only /
    missing metadata. This is the budget-friendly companion to the nightly
    dump baseline: bulk recency comes from the dump, while the limited live
    API budget keeps traffic + discovery data fresh where it matters — near
    the commander.

    Returns {"requested": n, "fetched": n, "errors": n}.
    """
    if limit is None:
        limit = db.get_setting_int("nearby_refresh_count", 50)
    max_age_h = db.get_setting_int("meta_refresh_hours", 24)

    rows = db.get_systems_for_meta_refresh(max_age_h)

    def dist(r) -> float:
        return math.sqrt(
            (r["x"] - current_pos[0]) ** 2
            + (r["y"] - current_pos[1]) ** 2
            + (r["z"] - current_pos[2]) ** 2
        )

    rows = sorted(rows, key=dist)[:limit]
    targets = [r["system_name"] for r in rows]
    return _fetch_meta_for(targets, progress)


def _fetch_meta_for(
    targets: list[str],
    progress: Optional[Callable[[int, int], bool]] = None,
) -> dict:
    """Fetch + cache metadata for an ordered list of system names."""
    total = len(targets)
    fetched = 0
    errors = 0
    backoff_level = 0

    i = 0
    while i < total:
        name = targets[i]
        try:
            meta = fetch_system_meta(name)
            db.upsert_system_meta(
                name,
                meta["edsm_updated_at"],
                meta["first_discovered"],
                meta["discovered_by"],
                meta["traffic_day"],
                meta["traffic_week"],
                meta["traffic_total"],
            )
            fetched += 1
            backoff_level = 0  # success → reset backoff
        except Exception as exc:
            if edsm_backoff.is_rate_limited(exc):
                backoff_level += 1
                if not edsm_backoff.backoff_wait(
                    backoff_level, "edsm_meta", progress, i, total
                ):
                    break  # cancelled during backoff
                continue   # retry the same system
            errors += 1
            print(f"[edsm_meta] WARNING: {name} failed: {exc}")

        i += 1

        if progress is not None:
            if progress(i, total) is False:
                break

        if i < total:
            time.sleep(REQUEST_DELAY_S)

    return {"requested": total, "fetched": fetched, "errors": errors}

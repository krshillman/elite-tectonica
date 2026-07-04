"""
autoskip.py — The "probably already footfallen" rule + FF-chance tiers.

First-footfall chance model
---------------------------
Odyssey (and therefore footfall) launched 2021-05-19. Traffic and body
update recency are weighted into a tier estimating the chance any first
footfalls remain in a system:

  Tier 0  Very Low   traffic in the last 24 h — someone is there right now
  Tier 1  Low        busy weekly traffic AND EDSM bodies updated recently
  Tier 2  Moderate   some weekly traffic, or updated since Odyssey launch
  Tier 3  High       traffic recorded at some point, but body data is old
  Tier 4  Very High  no recorded traffic and body data pre-Odyssey / absent
                     (nobody has plausibly walked here since footfall existed)

Auto-skip candidates are systems in Tier 0–1 (configurable thresholds feed
Tier 1). A system is a candidate only when it is not pinned and still has
open (Pending / In Progress) planets. Nothing is ever skipped without the
preview dialog's explicit confirmation.
"""

from __future__ import annotations

from typing import Optional

import db

# Elite Dangerous: Odyssey release date — first footfalls exist only after this
ODYSSEY_LAUNCH = "2021-05-19"

FF_CHANCE_LABELS = {
    0: "Very Low",
    1: "Low",
    2: "Moderate",
    3: "High",
    4: "Very High",
}


def get_config() -> dict:
    """Return the current auto-skip configuration."""
    return {
        "enabled": db.get_setting_bool("autoskip_enabled", True),
        "max_updated_days": db.get_setting_int("autoskip_max_updated_days", 365),
        "min_traffic": db.get_setting_int("autoskip_min_traffic", 10),
    }


def save_config(enabled: bool, max_updated_days: int, min_traffic: int) -> None:
    db.set_setting("autoskip_enabled", 1 if enabled else 0)
    db.set_setting("autoskip_max_updated_days", max_updated_days)
    db.set_setting("autoskip_min_traffic", min_traffic)


def ff_chance(meta: Optional[dict],
              max_updated_days: Optional[int] = None,
              min_traffic: Optional[int] = None) -> Optional[int]:
    """
    Return the FF-chance tier (0–4, see module docstring) for a system's
    cached EDSM metadata, or None when no metadata is cached yet.

    Threshold arguments default to the saved configuration.
    """
    if meta is None:
        return None

    if max_updated_days is None or min_traffic is None:
        cfg = get_config()
        max_updated_days = max_updated_days or cfg["max_updated_days"]
        min_traffic = min_traffic or cfg["min_traffic"]

    day = meta.get("traffic_day") or 0
    week = meta.get("traffic_week") or 0
    total = meta.get("traffic_total") or 0
    updated = meta.get("edsm_updated_at")  # "YYYY-MM-DD hh:mm:ss" or None

    # Tier 0 — someone was here within 24 hours
    if day > 0:
        return 0

    updated_recent = _within_days(updated, max_updated_days)
    updated_since_odyssey = bool(updated and updated >= ODYSSEY_LAUNCH)

    # Tier 1 — busy AND recently surveyed
    if week >= min_traffic and updated_recent:
        return 1

    # Tier 2 — some current activity or post-Odyssey survey data
    if week > 0 or (updated_since_odyssey and updated_recent):
        return 2

    # Tier 3 — visited at some point since records began, but data is stale
    if total > 0 and updated_since_odyssey:
        return 3

    # Tier 4 — no recorded traffic, body data pre-Odyssey or absent
    return 4


def _within_days(timestamp: Optional[str], days: int) -> bool:
    """True if an EDSM 'YYYY-MM-DD hh:mm:ss' timestamp is within N days."""
    if not timestamp:
        return False
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.strptime(timestamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(timestamp[:10], "%Y-%m-%d")
        except ValueError:
            return False
    return datetime.utcnow() - dt <= timedelta(days=days)


def get_candidates() -> list:
    """
    Systems recommended for skipping: FF chance Very Low / Low (tiers 0–1).
    Never includes pinned systems; only systems with open planets.
    """
    cfg = get_config()
    rows = db.get_autoskip_candidates(cfg["max_updated_days"], cfg["min_traffic"])
    return [
        r for r in rows
        if ff_chance(dict(r), cfg["max_updated_days"], cfg["min_traffic"]) in (0, 1)
    ]


def make_note(meta: dict) -> str:
    """Human-readable reason string appended to planet notes."""
    tier = ff_chance(meta)
    label = FF_CHANCE_LABELS.get(tier, "?") if tier is not None else "?"
    updated = (meta.get("edsm_updated_at") or "?")[:10]
    day = meta.get("traffic_day")
    week = meta.get("traffic_week")
    day_s = day if day is not None else "?"
    week_s = week if week is not None else "?"
    return (f"recommend skip: FF chance {label} "
            f"(EDSM upd {updated}, traffic {day_s}/24h {week_s}/wk)")


def annotate_candidates() -> int:
    """
    Append a "recommend skip" note to every open planet in systems matching
    the rule, WITHOUT changing any status. Returns number of systems noted.
    Runs only when auto-skip is enabled in settings.
    """
    if not get_config()["enabled"]:
        return 0
    count = 0
    for c in get_candidates():
        db.append_system_note(c["system_name"], make_note(dict(c)))
        count += 1
    return count


def apply_skip(selected: list[dict]) -> int:
    """
    Skip the confirmed systems from the preview dialog.
    ``selected`` is a list of candidate dicts. Returns planets skipped.
    """
    planets = 0
    for c in selected:
        note = "auto-skip: " + make_note(c).removeprefix("recommend skip: ")
        planets += db.skip_system_planets(c["system_name"], note)
    return planets

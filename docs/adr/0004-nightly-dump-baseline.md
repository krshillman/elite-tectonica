# 0004 — EDSM nightly dump as bulk baseline; live API reserved for nearby systems

## Status

Accepted

## Context

EDSM's live API is leaky-bucket rate limited (~360 requests/hour, headers
`X-Rate-Limit-Limit/Remaining/Reset`). The per-system metadata fetch costs
2 requests per system (bodies + traffic), so a bulk import of ~17,000
systems would need ~34,000 requests — roughly four days of continuous
API budget. Rate limiting historically returned an empty 200 body; 429s
are also observed now.

EDSM publishes full nightly JSON dumps at
https://www.edsm.net/en/nightly-dumps, including
`systemsWithCoordinates7days.json.gz` — every system updated in the last
7 days, with coordinates (tens of MB, vs multi-GB for the full dump).

## Decision

1. **Nightly dump baseline** (`edsm_nightly.py`): at most once per UTC day
   (auto-run on startup, or forced via 🌙 toolbar button), download the
   7-days dump, stream-scan it, and match against the working set:
   - matched systems get their `edsm_updated_at` advanced (never regressed)
     with rows tagged `meta_source='dump'`;
   - coordinates from matched entries are cached for free.
   The dump file is deleted after processing.

2. **Live API reserved for nearby systems** (`fetch_nearby_meta` + 📍
   Refresh Nearby): only the N closest systems (default 50, configurable)
   with open planets and missing/dump-only/stale (>24 h, configurable)
   metadata are refreshed via the API — traffic + discovery data the dump
   cannot provide.

3. **Rate-limit detection handles both signals**: HTTP 429 and empty 200
   bodies (raised as `RateLimitError`) both feed the existing escalating
   backoff (5/10/15… min, capped at 60).

Dump-seeded rows (`meta_source='dump'`) still count as "missing" for the
full 📡 Fetch System Info action, since they carry no traffic stats; an API
fetch upgrades them to `meta_source='api'`.

## Alternatives considered

- **EDAstro star-system API** (100 req/15 min, 10 systems per request ≈
  1,000 systems/15 min): much better bulk throughput than EDSM's
  per-system endpoints, but does not provide EDSM's traffic stats, which
  drive the FF-chance tiers. Kept in reserve as a possible supplement.
- **Full galaxy dump**: multi-GB download; unnecessary since absence from
  the 7-days dump already means "not updated recently", which is the
  signal the auto-skip rule needs.

## Consequences

- Bulk imports no longer exhaust the API budget; recency data arrives in
  one download instead of thousands of calls.
- Traffic/discovery data is only guaranteed fresh near the commander —
  acceptable, as skip decisions matter most for the next few jumps.
- A `cache/` directory (git-ignored) briefly holds the dump during
  processing.

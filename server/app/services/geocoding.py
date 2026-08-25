"""
Reverse geocoding service using OpenStreetMap Nominatim.

Used for generating human-readable trip names from GPS coordinates, and for
labeling period-route driving segments. Called only when trips are
created/finalized or a period-route is requested, never per GPS point.

Nominatim usage policy: max 1 request per second, and avoid re-querying the
same location repeatedly. Resolved place names are cached both in-process
(fast path within a single call/request) and in the geocode_cache DB table
(durable across restarts and shared across worker processes), keyed by
lat/lon rounded to 3 decimals (~100m) -- see GeocodeCache.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.geocode_cache import GeocodeCache

logger = logging.getLogger(__name__)

# In-memory cache: (rounded_lat, rounded_lon) -> place_name. Fast path on
# top of the DB cache -- avoids a DB round-trip for repeats within the same
# process/request. Cleared on restart; the DB cache (GeocodeCache) is what
# survives restarts and is shared across worker processes.
_CACHE: dict[tuple[float, float], Optional[str]] = {}
_CACHE_PRECISION = 3


def _round_coord(coord: float, precision: int = _CACHE_PRECISION) -> float:
    """Round coordinate for cache key."""
    return round(coord, precision)


def _extract_place_name(address: dict) -> Optional[str]:
    """
    Extract a meaningful short place name from Nominatim address dict.

    Tries: road, suburb/sector, district/city, province.
    Handles missing fields gracefully.
    """
    parts = []

    # Road (e.g. "RN4", "Main Street")
    road = address.get("road") or address.get("street") or address.get("path")
    if road:
        parts.append(road)

    # Suburb / sector / neighbourhood
    suburb = (
        address.get("suburb")
        or address.get("neighbourhood")
        or address.get("quarter")
        or address.get("borough")
        or address.get("sector")
    )
    if suburb and suburb not in parts:
        parts.append(suburb)

    # District / city / town / village
    district = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("city_district")
        or address.get("district")
    )
    if district and district not in parts:
        parts.append(district)

    # Province / state (optional, for disambiguation)
    province = address.get("state") or address.get("province")
    if province and province not in parts:
        parts.append(province)

    if not parts:
        return None

    return ", ".join(parts)


def _format_fallback(lat: float, lon: float) -> str:
    """Fallback when geocoding fails: use coordinates."""
    return f"{lat:.4f}, {lon:.4f}"


def _db_cache_get(lat_rounded: float, lon_rounded: float) -> Optional[str]:
    """Look up a rounded lat/lon in the persistent geocode cache."""
    db = SessionLocal()
    try:
        row = (
            db.query(GeocodeCache)
            .filter(GeocodeCache.lat == lat_rounded, GeocodeCache.lon == lon_rounded)
            .first()
        )
        return row.place_name if row else None
    except Exception as e:
        logger.warning("Geocode DB cache lookup failed: %s", e)
        return None
    finally:
        db.close()


def _db_cache_put(lat_rounded: float, lon_rounded: float, place_name: str) -> None:
    """Persist a resolved place name to the geocode cache. Safe to call
    concurrently from multiple processes -- a unique-constraint collision
    just means another process already cached this point, which is fine."""
    db = SessionLocal()
    try:
        db.add(GeocodeCache(lat=lat_rounded, lon=lon_rounded, place_name=place_name))
        db.commit()
    except IntegrityError:
        db.rollback()
    except Exception as e:
        db.rollback()
        logger.warning("Failed to persist geocode cache entry: %s", e)
    finally:
        db.close()


def _reverse_geocode_network(lat: float, lon: float) -> Optional[str]:
    """
    Perform the actual Nominatim HTTP call for (lat, lon). Caches the
    result in-memory always, and in the DB cache only on success (a failed
    lookup isn't a stable fact worth persisting forever -- it's retried
    next time instead).
    """
    cache_key = (_round_coord(lat), _round_coord(lon))
    url = f"{settings.NOMINATIM_BASE_URL}/reverse"
    params = {"lat": lat, "lon": lon, "format": "json", "addressdetails": 1}

    try:
        with httpx.Client(
            timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
        ) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("Nominatim reverse geocoding failed: %s", e)
        _CACHE[cache_key] = None
        return None
    except Exception as e:
        logger.warning("Unexpected geocoding error: %s", e)
        _CACHE[cache_key] = None
        return None

    address = data.get("address") or {}
    place_name = _extract_place_name(address)
    _CACHE[cache_key] = place_name
    if place_name:
        _db_cache_put(cache_key[0], cache_key[1], place_name)
    return place_name


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """
    Reverse-geocode a single point via Nominatim (cache-first: in-memory,
    then the DB cache, then a live network call).

    Returns a short place name (e.g. "Muhoza, Musanze") or None on error.
    Does NOT raise; errors are logged and return None.
    """
    return reverse_geocode_many([(lat, lon)])[(lat, lon)]


def reverse_geocode_many(
    coords: List[Tuple[float, float]],
    results: Optional[Dict[Tuple[float, float], Optional[str]]] = None,
) -> Dict[Tuple[float, float], Optional[str]]:
    """
    Reverse-geocode multiple points, returned keyed by the exact (lat, lon)
    pairs given (duplicates included, so callers don't need to dedupe
    first). Internally dedupes by rounded cache key (~100m cell -- see
    _CACHE_PRECISION), so distinct inputs landing in the same cell share one
    lookup / one Nominatim call.

    Sleeps 1s only immediately before an actual Nominatim network call
    (never for cache hits), so a batch of mostly-cached points isn't
    throttled for repeats -- only genuinely new locations pay the fair-use
    delay, in line with Nominatim's max-1-req/sec policy.

    If `results` is passed in, entries are written into it as each point
    resolves rather than only being visible once the whole batch completes.
    Callers that run this in a background thread under an overall deadline
    (e.g. asyncio.wait_for around asyncio.to_thread) should pass their own
    dict and read it directly after a timeout, instead of only trusting
    this function's return value -- otherwise points that resolved well
    before the deadline get thrown away just because a later point in the
    same batch was still in flight when the deadline fired.
    """
    if results is None:
        results = {}
    resolved_by_key: Dict[Tuple[float, float], Optional[str]] = {}
    made_network_call = False

    for lat, lon in coords:
        key = (_round_coord(lat), _round_coord(lon))
        if key not in resolved_by_key:
            if key in _CACHE:
                resolved_by_key[key] = _CACHE[key]
            else:
                db_hit = _db_cache_get(*key)
                if db_hit is not None:
                    _CACHE[key] = db_hit
                    resolved_by_key[key] = db_hit
                else:
                    if made_network_call:
                        time.sleep(1)
                    resolved_by_key[key] = _reverse_geocode_network(lat, lon)
                    made_network_call = True
        results[(lat, lon)] = resolved_by_key[key]

    return results


def build_trip_display_name(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> str:
    """
    Build a human-readable trip display name from start and end points.

    Format: "StartPlace → EndPlace"
    Fallback: "lat, lon → lat, lon" if geocoding fails for either point.
    Uses at most 2 Nominatim API calls (start + end).
    Respects Nominatim policy: 1 request per second.
    """
    start_name = reverse_geocode(start_lat, start_lon)
    time.sleep(1)  # Nominatim: max 1 request per second
    end_name = reverse_geocode(end_lat, end_lon)

    start_str = start_name if start_name else _format_fallback(start_lat, start_lon)
    end_str = end_name if end_name else _format_fallback(end_lat, end_lon)

    return f"{start_str} → {end_str}"

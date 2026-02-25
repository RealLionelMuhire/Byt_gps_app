"""
Reverse geocoding service using OpenStreetMap Nominatim.

Used for generating human-readable trip names from GPS coordinates.
Called only when trips are created/finalized, not per GPS point.

Nominatim usage policy: max 1 request per second.
"""

import logging
import time
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# In-memory cache: (rounded_lat, rounded_lon) -> place_name
# Rounded to 3 decimals (~111m) to balance uniqueness vs cache hits
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


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """
    Reverse-geocode a single point via Nominatim.

    Returns a short place name (e.g. "Muhoza, Musanze") or None on error.
    Does NOT raise; errors are logged and return None.
    """
    cache_key = (_round_coord(lat), _round_coord(lon))
    if cache_key in _CACHE:
        return _CACHE[cache_key]

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
    return place_name


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

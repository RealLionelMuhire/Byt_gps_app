"""
Unit tests for reverse_geocode_many's dedup/throttling behavior and the
period-route place-name attachment (_attach_driving_place_names).

Network and DB calls are mocked out -- these test the caching/dedup logic
itself, not Nominatim or Postgres connectivity.
"""

import asyncio
import os
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import geocoding
from app.api.locations import _attach_driving_place_names, _driving_segment_endpoints


@pytest.fixture(autouse=True)
def clear_in_memory_cache():
    geocoding._CACHE.clear()
    yield
    geocoding._CACHE.clear()


# --- reverse_geocode_many: dedup + throttling ---


def test_dedupes_identical_rounded_coords_into_one_network_call():
    with patch.object(geocoding, "_db_cache_get", return_value=None), \
         patch.object(geocoding, "_reverse_geocode_network", return_value="Kigali") as mock_net, \
         patch.object(geocoding.time, "sleep") as mock_sleep:
        results = geocoding.reverse_geocode_many([(-1.9441, 30.0619), (-1.9441, 30.0619)])

    assert mock_net.call_count == 1
    assert results[(-1.9441, 30.0619)] == "Kigali"
    mock_sleep.assert_not_called()  # only one network call -- nothing to throttle before


def test_sleeps_once_between_two_distinct_new_locations_not_before_first():
    with patch.object(geocoding, "_db_cache_get", return_value=None), \
         patch.object(geocoding, "_reverse_geocode_network", side_effect=["Kigali", "Musanze"]) as mock_net, \
         patch.object(geocoding.time, "sleep") as mock_sleep:
        results = geocoding.reverse_geocode_many([(-1.9441, 30.0619), (-1.4995, 29.6350)])

    assert mock_net.call_count == 2
    assert mock_sleep.call_count == 1  # throttle only before the 2nd real network call
    mock_sleep.assert_called_with(1)
    assert results[(-1.9441, 30.0619)] == "Kigali"
    assert results[(-1.4995, 29.6350)] == "Musanze"


def test_db_cache_hit_skips_network_call_and_sleep():
    with patch.object(geocoding, "_db_cache_get", return_value="Nyirangarama"), \
         patch.object(geocoding, "_reverse_geocode_network") as mock_net, \
         patch.object(geocoding.time, "sleep") as mock_sleep:
        results = geocoding.reverse_geocode_many([(-1.7, 29.9)])

    mock_net.assert_not_called()
    mock_sleep.assert_not_called()
    assert results[(-1.7, 29.9)] == "Nyirangarama"
    # DB hit should warm the in-memory cache too
    assert geocoding._CACHE[(-1.7, 29.9)] == "Nyirangarama"


def test_in_memory_cache_hit_skips_db_and_network():
    geocoding._CACHE[(-1.7, 29.9)] = "Nyirangarama"
    with patch.object(geocoding, "_db_cache_get") as mock_db, \
         patch.object(geocoding, "_reverse_geocode_network") as mock_net:
        results = geocoding.reverse_geocode_many([(-1.7, 29.9)])

    mock_db.assert_not_called()
    mock_net.assert_not_called()
    assert results[(-1.7, 29.9)] == "Nyirangarama"


def test_inputs_rounding_to_same_cell_share_one_lookup_but_each_returned():
    """Two distinct raw coords that round to the same ~100m cell must
    resolve to the same place name via a single lookup, but the returned
    dict must still have an entry for each exact input key."""
    a = (-1.94412, 30.06193)
    b = (-1.94413, 30.06194)  # rounds to the same 3-decimal cell as a
    with patch.object(geocoding, "_db_cache_get", return_value=None), \
         patch.object(geocoding, "_reverse_geocode_network", return_value="Kigali") as mock_net:
        results = geocoding.reverse_geocode_many([a, b])

    assert mock_net.call_count == 1
    assert results[a] == "Kigali"
    assert results[b] == "Kigali"


def test_partial_results_survive_a_deadline_that_fires_mid_batch():
    """Regression test for the period-route timeout path: if a caller runs
    reverse_geocode_many in a background thread under an asyncio.wait_for
    deadline and passes its own `results` dict, a point that resolved
    before the deadline (here: a DB-cache hit, near-instant) must still be
    visible in that dict even though the whole batch (run in a real
    thread, exactly like get_period_route does via asyncio.to_thread)
    hasn't finished because another point is still a slow, in-flight
    network call -- not discarded wholesale just because wait_for raised.
    """
    coord_fast = (-1.0, 30.0)   # resolves via a DB-cache hit -- no sleep at all
    coord_slow = (-1.2, 30.2)   # resolves via a slow "network" call
    fast_key = (geocoding._round_coord(coord_fast[0]), geocoding._round_coord(coord_fast[1]))

    def fake_db_cache_get(lat_r, lon_r):
        return "CachedPlace" if (lat_r, lon_r) == fast_key else None

    def fake_network(lat, lon):
        time.sleep(1.0)  # still in flight when the 0.15s deadline below fires
        return "SlowPlace"

    shared: dict = {}

    async def run():
        # Snapshot `shared` immediately after the deadline fires, in the
        # same running loop -- mirrors exactly what get_period_route does
        # (read `places` right after catching wait_for's exception). Using
        # asyncio.run()'s own return value instead would be misleading:
        # asyncio.run() drains the default executor on the way out, which
        # blocks until the slow background thread actually finishes,
        # masking the "still in flight" state this test exists to check.
        with patch.object(geocoding, "_db_cache_get", side_effect=fake_db_cache_get), \
             patch.object(geocoding, "_reverse_geocode_network", side_effect=fake_network):
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(geocoding.reverse_geocode_many, [coord_fast, coord_slow], shared),
                    timeout=0.15,
                )
            except asyncio.TimeoutError:
                pass
            return dict(shared)

    snapshot = asyncio.run(run())
    assert snapshot.get(coord_fast) == "CachedPlace"
    assert coord_slow not in snapshot  # still in flight when the deadline fired


def test_reverse_geocode_single_delegates_to_batch():
    with patch.object(geocoding, "_db_cache_get", return_value=None), \
         patch.object(geocoding, "_reverse_geocode_network", return_value="Kigali") as mock_net:
        result = geocoding.reverse_geocode(-1.9441, 30.0619)

    assert result == "Kigali"
    mock_net.assert_called_once()


# --- _attach_driving_place_names ---


def _driving_seg(start, end):
    return {
        "type": "driving",
        "points": [
            {"latitude": start[0], "longitude": start[1]},
            {"latitude": end[0], "longitude": end[1]},
        ],
    }


def test_attach_place_names_uses_resolved_places():
    segments = [_driving_seg((-1.9441, 30.0619), (-1.4995, 29.6350))]
    places = {(-1.9441, 30.0619): "Kigali", (-1.4995, 29.6350): "Nyirangarama"}
    _attach_driving_place_names(segments, places)

    assert segments[0]["start_place"] == "Kigali"
    assert segments[0]["end_place"] == "Nyirangarama"
    assert segments[0]["display_name"] == "Kigali → Nyirangarama"


def test_attach_place_names_falls_back_to_coordinates_when_unresolved():
    segments = [_driving_seg((-1.9441, 30.0619), (-1.4995, 29.6350))]
    _attach_driving_place_names(segments, {})  # nothing resolved (e.g. timeout)

    assert segments[0]["start_place"] == "-1.9441, 30.0619"
    assert segments[0]["end_place"] == "-1.4995, 29.6350"


def test_attach_place_names_skips_non_driving_segments():
    segments = [{"type": "stopped", "latitude": -1.9, "longitude": 30.0}]
    _attach_driving_place_names(segments, {})
    assert "start_place" not in segments[0]


def test_driving_segment_endpoints_collects_start_and_end_only():
    segments = [
        _driving_seg((-1.9441, 30.0619), (-1.5, 29.8)),
        {"type": "stopped", "latitude": -1.7, "longitude": 29.9},
        {"type": "offline", "duration_seconds": 100},
    ]
    coords = _driving_segment_endpoints(segments)
    assert coords == [(-1.9441, 30.0619), (-1.5, 29.8)]

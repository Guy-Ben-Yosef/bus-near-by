#!/usr/bin/env python3
"""Regression tests for bus-near-by.

    python3 -m unittest test_server -v

Two layers:
- Offline unit tests (mocked upstreams) locking down the API contract,
  filtering rules, ETA math, live/scheduled logic and the fallback path.
- Live integration tests asserting the freshness and speed that the app
  currently delivers; they skip (loudly) when the network is unavailable.
"""

import json
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import server

NOW = datetime(2026, 8, 23, 15, 0, 0, tzinfo=timezone.utc)
HEBREW = re.compile(r"[֐-׿]")

ARRIVAL_KEYS = {"line", "destination", "agency", "eta", "minutes", "status", "vehicle_ref"}
PAYLOAD_KEYS = {"stop", "source", "updated_at", "data_at", "arrivals"}


def visit(line="9", eta_min=5, departed_min=-30, has_location=True):
    return {
        "line_name": line,
        "eta": (NOW + timedelta(minutes=eta_min)).isoformat(),
        "departed": (NOW + timedelta(minutes=departed_min)).isoformat()
                    if departed_min is not None else None,
        "vehicle_ref": "1234567",
        "location": {"lat": "32.09", "lon": "34.78"} if has_location else None,
        "timestamp": NOW.isoformat(),
        "static_info": {"route": {
            "headsign": {"HE": "מסוף עתידים", "EN": "Atidim Terminal"},
            "destination": {"name": {"HE": "קריית עתידים", "EN": "Atidim"}},
            "agency": {"name": {"HE": "דן", "EN": "Dan"}},
        }},
    }


def curlbus_payload(visits):
    return {"errors": None, "timestamp": NOW.isoformat(),
            "visits": {str(server.STOP_CODE): visits}}


def run_curlbus(visits):
    with patch.object(server, "http_json", return_value=curlbus_payload(visits)):
        return server.curlbus_arrivals(NOW)


class CurlbusArrivalsTest(unittest.TestCase):
    def test_only_next_20_minutes(self):
        arrivals, _ = run_curlbus([visit(eta_min=m) for m in (-3, 2, 19, 21, 45)])
        self.assertEqual([a["minutes"] for a in arrivals], [2, 19])

    def test_sorted_by_eta(self):
        arrivals, _ = run_curlbus([visit(eta_min=m) for m in (12, 3, 8)])
        self.assertEqual([a["eta"] for a in arrivals], sorted(a["eta"] for a in arrivals))

    def test_minutes_floor_not_round(self):
        # 2m30s away must display as 2, not 3
        arrivals, _ = run_curlbus([visit(eta_min=2.5)])
        self.assertEqual(arrivals[0]["minutes"], 2)

    def test_english_only(self):
        arrivals, _ = run_curlbus([visit()])
        text = json.dumps(arrivals, ensure_ascii=False)
        self.assertIsNone(HEBREW.search(text), f"Hebrew leaked into payload: {text}")
        self.assertEqual(arrivals[0]["destination"], "Atidim Terminal")
        self.assertEqual(arrivals[0]["agency"], "Dan")

    def test_live_requires_location_and_departure(self):
        arrivals, _ = run_curlbus([
            visit(line="A", has_location=True, departed_min=-10),   # en route
            visit(line="B", has_location=False, departed_min=-10),  # no GPS report
            visit(line="C", has_location=True, departed_min=+5),    # not departed yet
        ])
        status = {a["line"]: a["status"] for a in arrivals}
        self.assertEqual(status, {"A": "live", "B": "scheduled", "C": "scheduled"})

    def test_no_location_data_in_payload(self):
        arrivals, _ = run_curlbus([visit()])
        self.assertEqual(set(arrivals[0]), ARRIVAL_KEYS)

    def test_missing_visits_raises_for_fallback(self):
        with patch.object(server, "http_json",
                          return_value={"errors": "boom", "visits": None, "timestamp": NOW.isoformat()}):
            with self.assertRaises(RuntimeError):
                server.curlbus_arrivals(NOW)

    def test_data_timestamp_passed_through(self):
        _, data_at = run_curlbus([visit()])
        self.assertEqual(server.parse_dt(data_at), NOW)


class EstimateEtaTest(unittest.TestCase):
    def loc(self, dist, recorded_min_ago=1, started_min_ago=30):
        return {
            "distance_from_journey_start": dist,
            "recorded_at_time": (NOW - timedelta(minutes=recorded_min_ago)).isoformat(),
            "siri_ride__scheduled_start_time": (NOW - timedelta(minutes=started_min_ago)).isoformat(),
        }

    def test_bus_past_stop_dropped(self):
        self.assertIsNone(server.estimate_eta(self.loc(dist=10500), 10000, NOW))

    def test_bus_not_departed_dropped(self):
        self.assertIsNone(server.estimate_eta(self.loc(dist=0), 10000, NOW))

    def test_stale_projection_dropped(self):
        # seen 25 min ago, 500 m short of the stop -> long gone
        self.assertIsNone(server.estimate_eta(self.loc(dist=9500, recorded_min_ago=25), 10000, NOW))

    def test_eta_never_in_the_past(self):
        eta = server.estimate_eta(self.loc(dist=9990), 10000, NOW)
        self.assertGreaterEqual(eta, NOW + timedelta(seconds=30))

    def test_pace_is_clamped(self):
        # absurdly slow observed pace must be clamped to PACE_MAX
        eta = server.estimate_eta(self.loc(dist=1000, started_min_ago=120), 10000, NOW)
        recorded = NOW - timedelta(minutes=1)
        self.assertEqual(eta, recorded + timedelta(seconds=9000 * server.PACE_MAX))


class StrideFallbackTest(unittest.TestCase):
    STOP = {"id": 1, "code": server.STOP_CODE, "name": "x", "city": "y",
            "lat": 32.09, "lon": 34.78}

    def planned_row(self, ride_id, arrive_min, line="9", line_ref=111, dist=10000):
        return {
            "gtfs_ride_id": ride_id,
            "arrival_time": (NOW + timedelta(minutes=arrive_min)).isoformat(),
            "gtfs_route__route_short_name": line,
            "gtfs_route__route_long_name": "ignored",
            "gtfs_route__agency_name": "ignored",
            "gtfs_route__line_ref": line_ref,
            "shape_dist_traveled": dist,
        }

    def live_row(self, ride_id, dist, line_ref=111, started_min_ago=30):
        return {
            "siri_ride__id": ride_id,
            "recorded_at_time": (NOW - timedelta(minutes=1)).isoformat(),
            "distance_from_journey_start": dist,
            "siri_ride__scheduled_start_time": (NOW - timedelta(minutes=started_min_ago)).isoformat(),
            "siri_route__line_ref": line_ref,
            "siri_ride__vehicle_ref": "v",
        }

    def run_stride(self, planned, live):
        def fake_api_get(path, **params):
            if path == "/gtfs_ride_stops/list":
                return planned
            if path == "/siri_vehicle_locations/list":
                return [r for r in live
                        if r["siri_route__line_ref"] == params["siri_routes__line_ref"]]
            raise AssertionError(f"unexpected api call: {path}")
        with patch.object(server, "api_get", side_effect=fake_api_get):
            return server.stride_arrivals(self.STOP, NOW)

    def test_matching_is_chronological_no_crossing(self):
        planned = [self.planned_row(1, 10), self.planned_row(2, 20)]
        live = [self.live_row(101, dist=7000), self.live_row(102, dist=9000)]
        arrivals = self.run_stride(planned, live)
        self.assertEqual([a["status"] for a in arrivals], ["live", "live"])
        # nearer bus (9000/10000) claims the earlier planned slot
        planned_order = [a["planned"] for a in arrivals]
        eta_order = [a["eta"] for a in arrivals]
        self.assertEqual(planned_order, sorted(planned_order))
        self.assertEqual(eta_order, sorted(eta_order))

    def test_unmatched_planned_stays_scheduled(self):
        arrivals = self.run_stride([self.planned_row(1, 10)], [])
        self.assertEqual(arrivals[0]["status"], "scheduled")
        self.assertEqual(arrivals[0]["minutes"], 10)

    def test_passed_planned_without_live_dropped(self):
        arrivals = self.run_stride([self.planned_row(1, -5), self.planned_row(2, 10)], [])
        self.assertEqual(len(arrivals), 1)
        self.assertEqual(arrivals[0]["minutes"], 10)

    def test_no_hebrew_on_fallback_path(self):
        arrivals = self.run_stride([self.planned_row(1, 10)], [])
        self.assertIsNone(HEBREW.search(json.dumps(arrivals, ensure_ascii=False)))


class BuildArrivalsTest(unittest.TestCase):
    def test_payload_contract(self):
        with patch.object(server, "utcnow", return_value=NOW), \
             patch.object(server, "http_json", return_value=curlbus_payload([visit()])):
            payload = server.build_arrivals()
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(payload["stop"],
                         {"code": server.STOP_CODE, "name": server.STOP_NAME,
                          "city": server.STOP_CITY})
        self.assertTrue(payload["source"].startswith("curlbus"))

    def test_falls_back_to_stride_when_curlbus_down(self):
        with patch.object(server, "utcnow", return_value=NOW), \
             patch.object(server, "curlbus_arrivals", side_effect=RuntimeError("down")), \
             patch.object(server, "get_stop", return_value=StrideFallbackTest.STOP), \
             patch.object(server, "stride_arrivals", return_value=[]) as stride:
            payload = server.build_arrivals()
        stride.assert_called_once()
        self.assertIn("stride fallback", payload["source"])


class CacheTest(unittest.TestCase):
    def setUp(self):
        server._cache.update({"at": 0.0, "payload": None, "error": None})

    def tearDown(self):
        server._cache.update({"at": 0.0, "payload": None, "error": None})

    def test_upstream_called_once_within_ttl(self):
        with patch.object(server, "build_arrivals", return_value={"arrivals": []}) as build:
            server.cached_arrivals()
            server.cached_arrivals()
        self.assertEqual(build.call_count, 1)

    def test_serves_stale_payload_with_error_on_failure(self):
        good = {"arrivals": [1]}
        with patch.object(server, "build_arrivals", return_value=good):
            server.cached_arrivals()
        server._cache["at"] = time.time() - server.CACHE_TTL_SEC - 1
        with patch.object(server, "build_arrivals", side_effect=RuntimeError("api down")):
            payload = server.cached_arrivals()
        self.assertEqual(payload["arrivals"], [1])
        self.assertIn("api down", payload["error"])


class FreshnessConfigTest(unittest.TestCase):
    """Freeze the knobs that make the board feel realtime."""

    def test_server_cache_at_most_10s(self):
        self.assertLessEqual(server.CACHE_TTL_SEC, 10)

    def test_horizon_is_20_minutes(self):
        self.assertEqual(server.HORIZON_MIN, 20)


class FrontendContractTest(unittest.TestCase):
    """The page is a passive single-screen English-only board."""

    @classmethod
    def setUpClass(cls):
        cls.html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_at_most_8_rows(self):
        self.assertIn("slice(0, 8)", self.html)

    def test_polls_every_15_seconds(self):
        self.assertIn("setInterval(refresh, 15000)", self.html)

    def test_no_scrolling_possible(self):
        self.assertIn("overflow:hidden", self.html)

    def test_minutes_counted_down_client_side(self):
        self.assertIn("Math.floor((new Date(a.eta) - Date.now()) / 60000)", self.html)

    def test_english_only(self):
        self.assertIsNone(HEBREW.search(self.html))

    def test_no_attribution_or_source_names(self):
        for word in ("hasadna", "curlbus", "stride", "open bus"):
            self.assertNotIn(word, self.html.lower())


def _live_curlbus():
    try:
        return server.http_json(f"{server.CURLBUS}/{server.STOP_CODE}",
                                headers={"Accept": "application/json"})
    except Exception:
        return None


class LiveIntegrationTest(unittest.TestCase):
    """Asserts the freshness/speed the app currently delivers. Requires network."""

    @classmethod
    def setUpClass(cls):
        cls.curlbus = _live_curlbus()

    def setUp(self):
        if self.curlbus is None:
            self.skipTest("network/curlbus unavailable — freshness not verified!")

    def test_upstream_data_is_fresh(self):
        age = (datetime.now(timezone.utc)
               - server.parse_dt(self.curlbus["timestamp"])).total_seconds()
        self.assertLess(age, 180, f"curlbus data is {age:.0f}s old — realtime regressed")

    def test_full_refresh_under_10_seconds_and_fresh(self):
        t0 = time.monotonic()
        payload = server.build_arrivals()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 10, f"build_arrivals took {elapsed:.1f}s")
        if payload["source"].startswith("curlbus"):
            age = (datetime.now(timezone.utc)
                   - server.parse_dt(payload["data_at"])).total_seconds()
            self.assertLess(age, 180, f"served data is {age:.0f}s old")
        now = datetime.now(timezone.utc)
        limit = now + timedelta(minutes=server.HORIZON_MIN, seconds=90)
        for a in payload["arrivals"]:
            self.assertLessEqual(server.parse_dt(a["eta"]), limit)
            self.assertGreaterEqual(a["minutes"], 0)
            self.assertIn(a["status"], ("live", "scheduled"))

    def test_cached_refresh_is_instant(self):
        server._cache.update({"at": 0.0, "payload": None, "error": None})
        server.cached_arrivals()               # warm (network)
        t0 = time.monotonic()
        server.cached_arrivals()               # must be served from cache
        self.assertLess(time.monotonic() - t0, 0.05)


if __name__ == "__main__":
    unittest.main()

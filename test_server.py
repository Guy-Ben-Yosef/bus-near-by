#!/usr/bin/env python3
"""Regression tests for bus-near-by (4-stop departure board).

    python3 -m unittest test_server -v

Two layers:
- Offline unit tests (mocked upstream) locking down the API contract,
  filtering rules, live/scheduled logic and the board's design contract.
- Live integration tests asserting current freshness and speed; they skip
  (loudly) when the network is unavailable.
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
CODE = server.STOP_CODES[0]

ARRIVAL_KEYS = {"line", "destination", "operator", "eta", "live", "updated_at"}
PAYLOAD_KEYS = {"stops", "updated_at"}


def visit(line="9", eta_min=5.0, departed_min=-30, has_location=True,
          headsign_en="Atidim Terminal", ts_sec_ago=10):
    return {
        "line_name": line,
        "eta": (NOW + timedelta(minutes=eta_min)).isoformat(),
        "departed": (NOW + timedelta(minutes=departed_min)).isoformat()
                    if departed_min is not None else None,
        "location": {"lat": "32.09", "lon": "34.78"} if has_location else None,
        "timestamp": (NOW - timedelta(seconds=ts_sec_ago)).isoformat(),
        "static_info": {"route": {
            "headsign": {"HE": "מסוף עתידים", **({"EN": headsign_en} if headsign_en else {})},
            "destination": {"name": {"HE": "קריית עתידים", "EN": "Atidim Fallback"}},
            "agency": {"name": {"HE": "דן", "EN": "Dan"}},
        }},
    }


def curlbus_payload(code, visits):
    return {"errors": None, "timestamp": NOW.isoformat(), "visits": {str(code): visits}}


def run_stop(visits):
    with patch.object(server, "http_json", return_value=curlbus_payload(CODE, visits)):
        return server.stop_arrivals(CODE, NOW)


class StopArrivalsTest(unittest.TestCase):
    def test_window_horizon_and_dropoff(self):
        # keep: due 30s ago (within 45s drop-off) and up to HORIZON_MIN ahead
        arrivals = run_stop([visit(line=l, eta_min=m) for l, m in
                             [("A", -1.0), ("B", -0.5), ("C", 5), ("D", 14), ("E", 16)]])
        self.assertEqual([a["line"] for a in arrivals], ["B", "C", "D"])

    def test_sorted_soonest_first(self):
        arrivals = run_stop([visit(eta_min=m) for m in (9, 1, 5)])
        self.assertEqual([a["eta"] for a in arrivals], sorted(a["eta"] for a in arrivals))

    def test_uppercase_english_with_fallback(self):
        arrivals = run_stop([visit(headsign_en="Levinsky College"),
                             visit(headsign_en=None)])  # no EN headsign -> destination EN
        self.assertEqual(arrivals[0]["destination"], "LEVINSKY COLLEGE")
        self.assertEqual(arrivals[1]["destination"], "ATIDIM FALLBACK")
        self.assertEqual(arrivals[0]["operator"], "DAN")
        text = json.dumps(arrivals, ensure_ascii=False)
        self.assertIsNone(HEBREW.search(text), f"Hebrew leaked into payload: {text}")

    def test_live_requires_location_and_departure(self):
        arrivals = run_stop([
            visit(line="A", has_location=True, departed_min=-10),   # en route
            visit(line="B", has_location=False, departed_min=-10),  # no GPS report
            visit(line="C", has_location=True, departed_min=+5),    # not departed yet
        ])
        self.assertEqual({a["line"]: a["live"] for a in arrivals},
                         {"A": True, "B": False, "C": False})

    def test_item_contract(self):
        arrivals = run_stop([visit()])
        self.assertEqual(set(arrivals[0]), ARRIVAL_KEYS)

    def test_per_vehicle_update_timestamp(self):
        arrivals = run_stop([visit(ts_sec_ago=80)])
        self.assertEqual(server.parse_dt(arrivals[0]["updated_at"]),
                         NOW - timedelta(seconds=80))

    def test_missing_visits_raises(self):
        with patch.object(server, "http_json",
                          return_value={"errors": "boom", "visits": None}):
            with self.assertRaises(RuntimeError):
                server.stop_arrivals(CODE, NOW)


class BuildPayloadTest(unittest.TestCase):
    @staticmethod
    def fake_http_json(url):
        code = int(url.rsplit("/", 1)[1])
        return curlbus_payload(code, [visit()])

    def test_payload_covers_all_four_stops(self):
        with patch.object(server, "utcnow", return_value=NOW), \
             patch.object(server, "http_json", side_effect=self.fake_http_json):
            payload = server.build_payload()
        self.assertEqual(set(payload), PAYLOAD_KEYS)
        self.assertEqual(set(payload["stops"]), {str(c) for c in server.STOP_CODES})
        for arrivals in payload["stops"].values():
            self.assertEqual(len(arrivals), 1)

    def test_any_stop_failure_fails_whole_payload(self):
        def flaky(url):
            if url.endswith(str(server.STOP_CODES[2])):
                raise OSError("connection refused")
            return self.fake_http_json(url)
        with patch.object(server, "http_json", side_effect=flaky):
            with self.assertRaises(Exception):
                server.build_payload()


class CacheTest(unittest.TestCase):
    def setUp(self):
        server._cache.update({"at": 0.0, "payload": None})

    tearDown = setUp

    def test_upstream_called_once_within_ttl(self):
        with patch.object(server, "build_payload", return_value={"stops": {}}) as build:
            server.cached_payload()
            server.cached_payload()
        self.assertEqual(build.call_count, 1)

    def test_failure_propagates_and_is_not_cached(self):
        with patch.object(server, "build_payload", side_effect=RuntimeError("down")) as build:
            with self.assertRaises(RuntimeError):
                server.cached_payload()
            with self.assertRaises(RuntimeError):
                server.cached_payload()
        self.assertEqual(build.call_count, 2)  # a failure must not poison the cache


class DesignConfigTest(unittest.TestCase):
    """Freeze the knobs the design specifies."""

    def test_stop_codes_match_design(self):
        self.assertEqual(server.STOP_CODES, (25893, 23012, 20676, 25894))

    def test_dropoff_45_seconds(self):
        self.assertEqual(server.DROPOFF_SEC, 45)

    def test_server_horizon_covers_board_window(self):
        self.assertGreaterEqual(server.HORIZON_MIN, 10)

    def test_server_cache_at_most_10s(self):
        self.assertLessEqual(server.CACHE_TTL_SEC, 10)


class FrontendContractTest(unittest.TestCase):
    """The board must keep the handed-off design's key behaviors."""

    @classmethod
    def setUpClass(cls):
        cls.html = (server.STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_fixed_1024x768_canvas_scaled_to_fit(self):
        self.assertIn("width:1024px", self.html)
        self.assertIn("height:768px", self.html)
        self.assertIn("window.innerWidth / 1024", self.html)
        self.assertIn("window.innerHeight / 768", self.html)

    def test_timings_15s_fetch_10s_pages_1s_tick(self):
        self.assertIn("setInterval(fetchData, 15000)", self.html)
        self.assertIn("setInterval(startTransition, 10000)", self.html)
        self.assertIn("setInterval(render, 1000)", self.html)

    def test_ten_minute_window_client_side(self):
        self.assertIn("b.mins > -0.75 && b.mins <= 10", self.html)

    def test_three_row_viewport_with_autoscroll(self):
        self.assertIn("height:270px", self.html)
        self.assertIn("0%,8% { transform:translateY(0); } 92%,100% { transform:translateY(-50%); }",
                      self.html)
        self.assertIn("list.length * 5", self.html)

    def test_split_flap_settles_left_to_right(self):
        self.assertIn("0.15 + (i / Math.max(1, str.length)) * 0.8", self.html)
        self.assertIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", self.html)
        self.assertIn("1500", self.html)  # 1.5s transition
        self.assertIn("if (transitioning) return", self.html)

    def test_staleness_colors(self):
        self.assertIn("#8fbc8f", self.html)   # fresh live
        self.assertIn("#d99a4e", self.html)   # stale live
        self.assertIn("updAge <= 45", self.html)

    def test_now_state_and_states_text(self):
        self.assertIn('"NOW"', self.html)
        self.assertIn("NO BUSES EXPECTED IN THE NEXT 10 MINUTES", self.html)
        self.assertIn("ARRIVAL DATA UNAVAILABLE", self.html)

    def test_page_labels_exact(self):
        self.assertIn("[ N/S ]  E/W ", self.html)
        self.assertIn("  N/S  [ E/W ]", self.html)

    def test_footer_and_font(self):
        self.assertIn("NEXT 10 MIN · REFRESH 15S", self.html)
        self.assertIn("IBM+Plex+Mono", self.html)

    def test_english_only_no_scroll(self):
        self.assertIsNone(HEBREW.search(self.html))
        self.assertIn("overflow:hidden", self.html)


class LiveIntegrationTest(unittest.TestCase):
    """Asserts the freshness/speed the board currently delivers. Needs network."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.raw = server.http_json(f"{server.CURLBUS}/{server.STOP_CODES[0]}")
        except Exception:
            cls.raw = None

    def setUp(self):
        if self.raw is None:
            self.skipTest("network/curlbus unavailable — freshness not verified!")

    def test_upstream_data_is_fresh(self):
        age = (datetime.now(timezone.utc)
               - server.parse_dt(self.raw["timestamp"])).total_seconds()
        self.assertLess(age, 180, f"curlbus data is {age:.0f}s old — realtime regressed")

    def test_full_refresh_under_10s_with_all_stops(self):
        t0 = time.monotonic()
        payload = server.build_payload()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 10, f"build_payload took {elapsed:.1f}s")
        self.assertEqual(set(payload["stops"]), {str(c) for c in server.STOP_CODES})
        now = datetime.now(timezone.utc)
        for arrivals in payload["stops"].values():
            for a in arrivals:
                self.assertLessEqual(
                    server.parse_dt(a["eta"]),
                    now + timedelta(minutes=server.HORIZON_MIN, seconds=90))
                self.assertIsInstance(a["live"], bool)

    def test_cached_refresh_is_instant(self):
        server._cache.update({"at": 0.0, "payload": None})
        server.cached_payload()               # warm (network)
        t0 = time.monotonic()
        server.cached_payload()               # must be served from cache
        self.assertLess(time.monotonic() - t0, 0.05)


if __name__ == "__main__":
    unittest.main()

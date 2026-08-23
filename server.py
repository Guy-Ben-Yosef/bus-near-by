#!/usr/bin/env python3
"""Realtime bus arrivals for a single stop.

Zero dependencies — Python 3 stdlib only.

    python3 server.py            # then open http://localhost:8000

Data sources, in order of preference:

1. Curlbus (https://curlbus.app, open source: github.com/elad661/curlbus) —
   a public JSON proxy for the Ministry of Transport's SIRI SM realtime feed.
   Gives the MOT's own predicted arrival time per bus, seconds-fresh.
2. Open Bus Stride API (https://github.com/hasadna/open-bus-stride-api, by
   Hasadna) — used as fallback when Curlbus is down, and for the stop's
   name/location. Stride's SIRI ingestion can lag realtime by 2-20 minutes,
   so ETAs on this path are projections from the last known bus position.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CURLBUS = "https://curlbus.app"
STRIDE = "https://open-bus-stride-api.hasadna.org.il"
STOP_CODE = 23012
STOP_NAME = "Milano Square / Ibn Gvirol"
STOP_CITY = "Tel Aviv"
PORT = 8000

HORIZON_MIN = 20            # only show arrivals within the next 20 minutes
PLANNED_LOOKBACK_MIN = 20   # keep recently-missed planned times so delayed buses still match
LIVE_LOOKBACK_MIN = 30      # Stride's SIRI ETL can lag realtime by 15+ minutes
MATCH_WINDOW_MIN = 25       # max |planned - eta| when pairing a live bus to a planned arrival
CACHE_TTL_SEC = 10

# Observed bus pace clamp, seconds per meter (0.10 = 36 km/h, 0.50 = 7.2 km/h)
PACE_MIN, PACE_MAX, PACE_DEFAULT = 0.10, 0.50, 0.22

STATIC_DIR = Path(__file__).parent


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s):
    return datetime.fromisoformat(s)


def http_json(url, headers=None):
    req = Request(url, headers={"User-Agent": "bus-near-by/1.0 (local hobby app)",
                                **(headers or {})})
    with urlopen(req, timeout=15) as resp:
        return json.load(resp)


# ------------------------------------------------------------- stride helpers

def api_get(path, **params):
    data = http_json(f"{STRIDE}{path}?{urlencode(params)}")
    if isinstance(data, dict):  # list endpoints return dicts only on error
        raise RuntimeError(f"Stride API error for {path}: {data}")
    return data


_stop_cache = {}


def get_stop():
    """Today's GTFS stop record for STOP_CODE (falls back to yesterday's
    record early in the morning, before the daily GTFS load)."""
    for days_back in (0, 1):
        date = (utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        if date in _stop_cache:
            return _stop_cache[date]
        rows = api_get("/gtfs_stops/list", code=STOP_CODE, date=date, limit=1)
        if rows:
            _stop_cache[date] = rows[0]
            return rows[0]
    raise RuntimeError(f"stop code {STOP_CODE} not found in GTFS")


# ------------------------------------------------- primary source: curlbus

def curlbus_arrivals(now):
    data = http_json(f"{CURLBUS}/{STOP_CODE}", headers={"Accept": "application/json"})
    visits = (data.get("visits") or {}).get(str(STOP_CODE))
    if visits is None:
        raise RuntimeError(f"curlbus returned no visits (errors: {data.get('errors')})")

    arrivals = []
    for v in visits:
        eta = parse_dt(v["eta"])
        if eta < now - timedelta(minutes=1) or eta > now + timedelta(minutes=HORIZON_MIN):
            continue
        route = (v.get("static_info") or {}).get("route") or {}
        headsign = (route.get("headsign") or {}).get("EN")
        dest = ((route.get("destination") or {}).get("name") or {}).get("EN")
        agency = (route.get("agency") or {}).get("name", {}).get("EN")
        departed = parse_dt(v["departed"]) if v.get("departed") else None
        en_route = bool(v.get("location")) and (departed is None or departed <= now)
        arrivals.append({
            "line": v.get("line_name"),
            "destination": headsign or dest or "",
            "agency": agency or "",
            "eta": eta.isoformat(),
            "minutes": max(0, int((eta - now).total_seconds() // 60)),
            "status": "live" if en_route else "scheduled",
            "vehicle_ref": v.get("vehicle_ref"),
        })
    arrivals.sort(key=lambda a: a["eta"])
    return arrivals, parse_dt(data["timestamp"]).isoformat()


# ------------------------------------------------ fallback source: stride

def fetch_planned(stop, now):
    rows = api_get(
        "/gtfs_ride_stops/list",
        gtfs_stop_ids=stop["id"],
        arrival_time_from=iso(now - timedelta(minutes=PLANNED_LOOKBACK_MIN)),
        arrival_time_to=iso(now + timedelta(minutes=HORIZON_MIN)),
        limit=200,
    )
    planned, seen = [], set()
    for r in rows:
        key = (r["gtfs_ride_id"], r["arrival_time"])
        if key in seen:
            continue
        seen.add(key)
        planned.append({
            "line": r["gtfs_route__route_short_name"],
            "destination": "",  # GTFS names are Hebrew-only; the UI is English-only
            "agency": "",
            "planned": parse_dt(r["arrival_time"]),
            "line_ref": r["gtfs_route__line_ref"],
            "stop_dist_m": r["shape_dist_traveled"],
            "live": None,
        })
    return planned


def fetch_live_for_line(line_ref, now):
    return api_get(
        "/siri_vehicle_locations/list",
        siri_routes__line_ref=line_ref,
        recorded_at_time_from=iso(now - timedelta(minutes=LIVE_LOOKBACK_MIN)),
        recorded_at_time_to=iso(now + timedelta(minutes=2)),  # drops bogus-clock reports
        limit=100,
        order_by="recorded_at_time desc",
    )


def fetch_live(line_refs, now):
    """Latest location per active ride, for every given line_ref."""
    latest = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(lambda lr: fetch_live_for_line(lr, now), line_refs):
            for r in rows:
                ride = r["siri_ride__id"]
                if ride not in latest or r["recorded_at_time"] > latest[ride]["recorded_at_time"]:
                    latest[ride] = r
    return list(latest.values())


def estimate_eta(loc, stop_dist_m, now):
    """ETA from remaining route distance and the bus's own average pace."""
    dist = loc["distance_from_journey_start"]
    if dist is None or stop_dist_m is None or dist < 50:
        return None  # not departed / no usable progress data
    remaining = stop_dist_m - dist
    if remaining < -150:
        return None  # already passed the stop
    recorded = parse_dt(loc["recorded_at_time"])
    started = parse_dt(loc["siri_ride__scheduled_start_time"])
    elapsed = (recorded - started).total_seconds()
    pace = elapsed / dist if elapsed > 60 else PACE_DEFAULT
    pace = min(max(pace, PACE_MIN), PACE_MAX)
    eta = recorded + timedelta(seconds=max(remaining, 0) * pace)
    if eta < now - timedelta(minutes=2):
        return None  # projection says it already passed the stop
    return max(eta, now + timedelta(seconds=30))


def stride_arrivals(stop, now):
    planned = fetch_planned(stop, now)

    stop_dist_by_line = {p["line_ref"]: p["stop_dist_m"] for p in planned}
    live = fetch_live(sorted(stop_dist_by_line), now)

    with_eta = []
    for loc in live:
        eta = estimate_eta(loc, stop_dist_by_line.get(loc["siri_route__line_ref"]), now)
        if eta is not None:
            with_eta.append((eta, loc))
    with_eta.sort(key=lambda pair: pair[0])  # nearest bus claims the earliest planned slot

    for eta, loc in with_eta:
        line_ref = loc["siri_route__line_ref"]
        candidates = [p for p in planned if p["line_ref"] == line_ref and p["live"] is None
                      and abs((p["planned"] - eta).total_seconds()) <= MATCH_WINDOW_MIN * 60]
        if not candidates:
            continue
        best = min(candidates, key=lambda p: p["planned"])
        best["live"] = {"eta": eta}

    arrivals = []
    for p in planned:
        when = p["live"]["eta"] if p["live"] else p["planned"]
        if when < now - timedelta(minutes=1):
            continue  # planned time passed and no live bus matched it
        item = {
            "line": p["line"],
            "destination": p["destination"],
            "agency": p["agency"],
            "planned": p["planned"].isoformat(),
            "eta": when.isoformat(),
            "minutes": max(0, round((when - now).total_seconds() / 60)),
            "status": "live" if p["live"] else "scheduled",
        }
        if p["live"]:
            item["delay_min"] = round((p["live"]["eta"] - p["planned"]).total_seconds() / 60)
        arrivals.append(item)
    arrivals.sort(key=lambda a: a["eta"])
    return arrivals


# --------------------------------------------------------------------- merge

def build_arrivals():
    now = utcnow()
    try:
        (arrivals, data_at), source = curlbus_arrivals(now), "curlbus (MOT SIRI realtime)"
    except Exception as curlbus_err:
        arrivals, data_at, source = stride_arrivals(get_stop(), now), now.isoformat(), \
            f"stride fallback (curlbus unavailable: {curlbus_err})"

    return {
        "stop": {"code": STOP_CODE, "name": STOP_NAME, "city": STOP_CITY},
        "source": source,
        "updated_at": now.isoformat(),
        "data_at": data_at,
        "arrivals": arrivals,
    }


# --------------------------------------------------------------------- serve

_cache = {"at": 0.0, "payload": None, "error": None}
_cache_lock = threading.Lock()


def cached_arrivals():
    with _cache_lock:
        if time.time() - _cache["at"] < CACHE_TTL_SEC and _cache["payload"]:
            return _cache["payload"]
        try:
            _cache["payload"] = build_arrivals()
            _cache["error"] = None
        except Exception as e:  # keep serving the last good payload
            _cache["error"] = str(e)
            if _cache["payload"] is None:
                raise
            _cache["payload"]["error"] = str(e)
        _cache["at"] = time.time()
        return _cache["payload"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/api/arrivals":
            try:
                body = json.dumps(cached_arrivals(), ensure_ascii=False).encode()
                self._send(200, "application/json; charset=utf-8", body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self._send(502, "application/json; charset=utf-8", body)
        elif self.path in ("/", "/index.html"):
            body = (STATIC_DIR / "index.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


if __name__ == "__main__":
    print(f"bus-near-by: serving stop {STOP_CODE} on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

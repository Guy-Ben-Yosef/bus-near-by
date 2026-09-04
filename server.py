#!/usr/bin/env python3
"""Bus Near By — realtime corner departure board.

Serves arrivals for the 4 stops around Milano Square / Ibn Gvirol, Tel Aviv.
Zero dependencies — Python 3 stdlib only; built to run on a Raspberry Pi
driving a 4:3 monitor in browser kiosk mode.

    python3 server.py            # then open http://localhost:8000

Data: https://curlbus.app (open source: github.com/elad661/curlbus) — a
public JSON proxy for the Ministry of Transport's SIRI SM realtime feed,
returning the MOT's own predicted arrival per bus, seconds-fresh. When the
feed is unreachable this API returns 502 and the board shows its error
state ("ARRIVAL DATA UNAVAILABLE") until the next successful refresh.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen

CURLBUS = "https://curlbus.app"
STOP_CODES = (25893, 23012, 20676, 25894)  # southbound, northbound, westbound, eastbound
PORT = 8000

HORIZON_MIN = 15     # serve a bit past the board's 10-min window; the client trims each second
DROPOFF_SEC = 45     # keep buses briefly after their due time, per design
CACHE_TTL_SEC = 10

# Weather screen: Open-Meteo (free, no key) for the corner of Tel Aviv the
# board watches. Weather moves slowly, so cache far longer than arrivals.
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
TEL_AVIV = (32.0853, 34.7818)         # lat, lon — Milano Square / Ibn Gvirol
WEATHER_CACHE_TTL_SEC = 300           # 5 min
RAIN_HORIZON_H = 2                    # rain timeline window on the board

# Bathroom climate sensor: a read-only Postgres that gets a temperature/humidity
# row every ~30 s. The DSN carries a password, so it comes from the environment
# (see deploy/README) and is never checked in. Unset -> /api/climate returns 502
# and the board just hides the humidity readout.
CLIMATE_DSN = os.environ.get("BNB_CLIMATE_DSN", "")
CLIMATE_CACHE_TTL_SEC = 20            # sensor samples every ~30 s
CLIMATE_WINDOW_MIN = 60               # the board plots the last hour
CLIMATE_STALE_SEC = 600               # no reading in 10 min -> sensor offline

# WMO weather codes -> the board's uppercase condition text.
WMO = {
    0: "CLEAR", 1: "MAINLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST",
    45: "FOG", 48: "FOG",
    51: "DRIZZLE", 53: "DRIZZLE", 55: "DRIZZLE",
    56: "FREEZING DRIZZLE", 57: "FREEZING DRIZZLE",
    61: "RAIN", 63: "RAIN", 65: "HEAVY RAIN",
    66: "FREEZING RAIN", 67: "FREEZING RAIN",
    71: "SNOW", 73: "SNOW", 75: "HEAVY SNOW", 77: "SNOW GRAINS",
    80: "SHOWERS", 81: "SHOWERS", 82: "VIOLENT SHOWERS",
    85: "SNOW SHOWERS", 86: "SNOW SHOWERS",
    95: "THUNDERSTORM", 96: "THUNDERSTORM", 99: "THUNDERSTORM",
}

STATIC_DIR = Path(__file__).parent


def utcnow():
    return datetime.now(timezone.utc)


def parse_dt(s):
    return datetime.fromisoformat(s)


def http_json(url):
    req = Request(url, headers={"User-Agent": "bus-near-by/2.0 (local hobby app)",
                                "Accept": "application/json"})
    with urlopen(req, timeout=15) as resp:
        return json.load(resp)


def stop_arrivals(code, now):
    data = http_json(f"{CURLBUS}/{code}")
    visits = (data.get("visits") or {}).get(str(code))
    if visits is None:
        raise RuntimeError(f"curlbus returned no visits for stop {code} "
                           f"(errors: {data.get('errors')})")

    arrivals = []
    for v in visits:
        eta = parse_dt(v["eta"])
        if (eta < now - timedelta(seconds=DROPOFF_SEC)
                or eta > now + timedelta(minutes=HORIZON_MIN)):
            continue
        route = (v.get("static_info") or {}).get("route") or {}
        headsign = (route.get("headsign") or {}).get("EN")
        dest = ((route.get("destination") or {}).get("name") or {}).get("EN")
        agency = ((route.get("agency") or {}).get("name") or {}).get("EN")
        departed = parse_dt(v["departed"]) if v.get("departed") else None
        arrivals.append({
            "line": (v.get("line_name") or "").upper(),
            "destination": (headsign or dest or "").upper(),
            "operator": (agency or "").upper(),
            "eta": eta.isoformat(),
            "live": bool(v.get("location")) and (departed is None or departed <= now),
            "updated_at": parse_dt(v["timestamp"]).isoformat() if v.get("timestamp") else None,
        })
    arrivals.sort(key=lambda a: a["eta"])
    return arrivals


def build_payload():
    now = utcnow()
    with ThreadPoolExecutor(max_workers=len(STOP_CODES)) as pool:
        results = list(pool.map(lambda c: stop_arrivals(c, now), STOP_CODES))
    return {
        "stops": {str(c): r for c, r in zip(STOP_CODES, results)},
        "updated_at": now.isoformat(),
    }


def _round(x):
    return round(x) if isinstance(x, (int, float)) else None


def build_weather():
    now = utcnow()
    params = ("?latitude=%s&longitude=%s"
              "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
              "wind_speed_10m,weather_code"
              "&daily=sunrise,sunset"
              "&minutely_15=precipitation,precipitation_probability"
              "&forecast_days=1&timezone=UTC") % TEL_AVIV
    data = http_json(OPEN_METEO + params)

    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    m = data.get("minutely_15") or {}
    times = m.get("time") or []
    precip = m.get("precipitation") or []
    prob = m.get("precipitation_probability") or []

    rain = []
    for t, mm, pp in zip(times, precip, prob):
        bt = parse_dt(t)
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=timezone.utc)
        if bt < now - timedelta(minutes=15) or bt > now + timedelta(hours=RAIN_HORIZON_H):
            continue
        rain.append({"t": bt.isoformat(), "mm": mm or 0, "prob": pp or 0})

    def first_utc_iso(key):
        arr = daily.get(key) or []
        if not arr or not arr[0]:
            return None
        d = parse_dt(arr[0])
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.isoformat()

    code = int(cur.get("weather_code") or 0)
    return {
        "temp": _round(cur.get("temperature_2m")),
        "feels": _round(cur.get("apparent_temperature")),
        "humidity": _round(cur.get("relative_humidity_2m")),
        "wind": _round(cur.get("wind_speed_10m")),
        "code": code,
        "condition": WMO.get(code, "—"),
        "sunrise": first_utc_iso("sunrise"),
        "sunset": first_utc_iso("sunset"),
        "rain": rain,
        "updated_at": now.isoformat(),
    }


def build_climate():
    """Latest bathroom reading plus one point per minute for the last hour."""
    if not CLIMATE_DSN:
        raise RuntimeError("BNB_CLIMATE_DSN is not set")
    import psycopg2  # only this endpoint needs it; the rest stays stdlib-only

    now = utcnow()
    conn = psycopg2.connect(CLIMATE_DSN, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts, humidity_pct, temperature_c"
                " FROM readings ORDER BY ts DESC LIMIT 1")
            latest = cur.fetchone()
            cur.execute(
                "SELECT date_trunc('minute', ts) AS m,"
                "       avg(humidity_pct), avg(temperature_c)"
                "  FROM readings"
                " WHERE ts > now() - make_interval(mins => %s)"
                " GROUP BY 1 ORDER BY 1", (CLIMATE_WINDOW_MIN,))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not latest:
        raise RuntimeError("no readings in the climate database")

    at, hum, temp = latest
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    age = (now - at).total_seconds()

    series = []
    for m, h, t in rows:
        if m.tzinfo is None:
            m = m.replace(tzinfo=timezone.utc)
        series.append({"t": m.isoformat(), "h": round(float(h), 1)})

    return {
        "humidity": None if hum is None else round(float(hum), 1),
        "temperature": None if temp is None else round(float(temp), 1),
        "at": at.isoformat(),
        "age_sec": int(age),
        "stale": age > CLIMATE_STALE_SEC,
        "window_min": CLIMATE_WINDOW_MIN,
        "series": series,
        "updated_at": now.isoformat(),
    }


_cache = {"at": 0.0, "payload": None}
_cache_lock = threading.Lock()
_wcache = {"at": 0.0, "payload": None}
_wcache_lock = threading.Lock()
_ccache = {"at": 0.0, "payload": None}
_ccache_lock = threading.Lock()


def cached_payload():
    with _cache_lock:
        if time.time() - _cache["at"] < CACHE_TTL_SEC and _cache["payload"]:
            return _cache["payload"]
        _cache["payload"] = build_payload()  # raises on failure -> 502 -> board error state
        _cache["at"] = time.time()
        return _cache["payload"]


def cached_weather():
    with _wcache_lock:
        if time.time() - _wcache["at"] < WEATHER_CACHE_TTL_SEC and _wcache["payload"]:
            return _wcache["payload"]
        _wcache["payload"] = build_weather()  # raises on failure -> 502 -> board error state
        _wcache["at"] = time.time()
        return _wcache["payload"]


def cached_climate():
    with _ccache_lock:
        if time.time() - _ccache["at"] < CLIMATE_CACHE_TTL_SEC and _ccache["payload"]:
            return _ccache["payload"]
        _ccache["payload"] = build_climate()  # raises -> 502 -> board hides the readout
        _ccache["at"] = time.time()
        return _ccache["payload"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/api/arrivals", "/api/weather", "/api/climate"):
            fetch = {"/api/arrivals": cached_payload,
                     "/api/weather": cached_weather,
                     "/api/climate": cached_climate}[route]
            try:
                body = json.dumps(fetch(), ensure_ascii=False).encode()
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
    print(f"bus-near-by: serving stops {STOP_CODES} on http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

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


_cache = {"at": 0.0, "payload": None}
_cache_lock = threading.Lock()


def cached_payload():
    with _cache_lock:
        if time.time() - _cache["at"] < CACHE_TTL_SEC and _cache["payload"]:
            return _cache["payload"]
        _cache["payload"] = build_payload()  # raises on failure -> 502 -> board error state
        _cache["at"] = time.time()
        return _cache["payload"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/api/arrivals":
            try:
                body = json.dumps(cached_payload(), ensure_ascii=False).encode()
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

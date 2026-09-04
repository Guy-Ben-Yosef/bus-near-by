# Doorboard

An ambient display that hangs above the front door and reports on both sides
of it: the 4 bus stops around **Milano Square / Ibn Gvirol, Tel Aviv**, the
weather and sky overhead, and the bathroom's humidity. Runs full-screen on a
Raspberry Pi driving a 4:3 monitor in browser kiosk mode. No interaction of
any kind — no scroll, no taps; the board is watch-only.

## Run

```bash
python3 server.py
```

Then open <http://localhost:8000>. Python 3 stdlib only, with one optional
extra: the bathroom humidity readout needs `psycopg2` (`sudo apt install
python3-psycopg2`) and a `DOORBOARD_CLIMATE_DSN` environment variable holding the
sensor database URL. Without either, everything else runs unchanged and the
board simply hides that readout. (For a display on another device, change the
bind address at the bottom of `server.py` to `0.0.0.0`.)

## The board

Key behaviors:

- Fixed 1024×768 canvas, uniformly scaled to fit the screen; IBM Plex Mono,
  all uppercase, dark Solari-board aesthetic.
- Three screens rotating every 15 s with a split-flap character-shuffle
  transition: **N/S** (stop 25893 southbound | stop 23012 northbound),
  **E/W** (stop 20676 westbound | stop 25894 eastbound), and **WX** — a
  weather screen with a large clock and date, current conditions, a sun arc
  (dot riding sunrise→sunset) with sunrise/sunset times, the moon phase and
  illumination, the bathroom humidity (current %, a status light, and the last
  60 min plotted), and a next-2h rain timeline (orange bars = rain expected,
  gray = dry).
- Per stop: buses due in the next 10 minutes, soonest first, 6 rows visible.
  When more qualify, the list scrolls down exactly once (2 s per hidden
  row), timed to reach the end 1 s before the screen transition, and holds
  there until the flip. `NOW` when under a minute; buses linger ~45 s past
  due.
- MIN value color: green = live and updated ≤45 s ago, orange = live but
  update delayed, gray = scheduled (not yet departed).
- Data refreshes every 15 s; countdown re-renders every second. Feed outage
  shows `ARRIVAL DATA UNAVAILABLE` with the age of the last good update.

## Backend

`GET /api/arrivals` (cached 10 s) fans out to https://curlbus.app — a
public JSON proxy for the Ministry of Transport's SIRI SM realtime feed —
one request per stop, in parallel. Each arrival: line, English destination,
operator, ETA, live flag, and the vehicle's last-update timestamp. Upstream
failure returns 502, which the board renders as its error state.

`GET /api/weather` (cached 5 min) proxies https://open-meteo.com — free, no
API key — for the corner's coordinates: current temperature, feels-like,
humidity, wind, and condition, today's sunrise/sunset, plus 15-minute
precipitation buckets for the next 2 h. Upstream failure returns 502 (the
weather screen shows its own error state). Moon phase is computed client-side
from the date (no API).

`GET /api/climate` (cached 20 s) reads the bathroom sensor's Postgres — a
row every ~30 s — returning the latest temperature/humidity plus one
per-minute average for the last hour. The connection string comes from
`DOORBOARD_CLIMATE_DSN` (it contains a password, so it is never committed; on the
Pi it lives in root-owned `0600` `/etc/doorboard.env`, which the systemd
unit loads). Unset, unreachable, or driver missing → 502, and the board
hides the readout rather than showing an error. Readings older than 10 min
render the status light gray with `SENSOR OFFLINE`.

### The humidity status light

Answers one question: *did someone shower and leave the window shut?*
See `docs/humidity-algorithm.html` for the flowchart.

- **Green** — nothing happened, or it has come back to within 2.5 pts of
  where it started. A slow damp drift never arms the light; only a
  shower-shaped jump does.
- **Amber** — it spiked and is on its way back: inside the 30-minute grace
  period, still visibly falling, or settled only a little above the start.
- **Red** — it spiked and went flat *well* above where it started. The board
  switches the caption to `OPEN THE WINDOW`.

Everything is judged against **the level the room sat at just before the
rise**, taken from the trace each time, so there are no absolute percentages
to retune between summer and winter, and sensor downtime cannot skew the
reference. "Current" is a 5-minute median so one noisy sample cannot flip the
light.

The non-obvious part: an aired-out bathroom does *not* return to its previous
level promptly — wet surfaces keep evaporating, so it plateaus a couple of
points above and stays there. **Flat does not mean stuck.** What marks a shut
window is going flat a long way up, which is why red keys off `HUM_STUCK_EXCESS`
(how far above the start it settled) rather than off the trend alone.

`humidity_status()` in `server.py` decides it; the knobs are the `HUM_*`
constants and `HumidityStatusTest` covers the cases, including both
ventilated-shower shapes that an earlier trend-only version got wrong.
Thresholds came from the sensor's own history and are provisional — worth
revisiting once there are a few weeks of showers to check them against.

## Tests

```bash
python3 -m unittest test_server -v
```

Stdlib `unittest` only. Offline unit tests (mocked upstream) freeze the API
contract, the arrival window and drop-off rules, live/scheduled logic, the
cache, and the frontend's design contract (canvas, timings, split-flap,
colors, states); live integration tests assert upstream freshness (< 3 min)
and refresh speed (< 10 s cold, instant cached), skipping if offline.

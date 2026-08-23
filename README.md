# bus-near-by

A passive realtime departure board for the 4 bus stops around **Milano
Square / Ibn Gvirol, Tel Aviv**, built to run full-screen on a Raspberry Pi
driving a 4:3 monitor in browser kiosk mode. No interaction of any kind —
no scroll, no taps; the board is watch-only.

## Run

```bash
python3 server.py
```

Then open <http://localhost:8000>. No dependencies — Python 3 stdlib only.
(For a display on another device, change the bind address at the bottom of
`server.py` to `0.0.0.0`.)

## The board

Implements the `design_handoff_bus_near_by/` spec exactly:

- Fixed 1024×768 canvas, uniformly scaled to fit the screen; IBM Plex Mono,
  all uppercase, dark Solari-board aesthetic.
- Two screens alternating every 15 s with a split-flap character-shuffle
  transition: **N/S** (stop 25893 southbound | stop 23012 northbound) and
  **E/W** (stop 20676 westbound | stop 25894 eastbound).
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

## Tests

```bash
python3 -m unittest test_server -v
```

Stdlib `unittest` only. Offline unit tests (mocked upstream) freeze the API
contract, the arrival window and drop-off rules, live/scheduled logic, the
cache, and the frontend's design contract (canvas, timings, split-flap,
colors, states); live integration tests assert upstream freshness (< 3 min)
and refresh speed (< 10 s cold, instant cached), skipping if offline.

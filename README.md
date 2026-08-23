# bus-near-by

Realtime "next buses" board for stop **23012** — כיכר מילאנו/אבן גבירול,
Tel Aviv.

## Run

```bash
python3 server.py
```

Then open <http://localhost:8000>. No dependencies — Python 3 stdlib only.

## Data sources

1. **Primary: [Curlbus](https://curlbus.app)**
   ([open source](https://github.com/elad661/curlbus)) — a public JSON proxy
   for the Ministry of Transport's **SIRI SM** realtime feed. Returns the
   MOT's own predicted arrival time per bus (the same predictions transit
   apps like Bus Nearby show), typically seconds-fresh.
2. **Fallback: [Open Bus Stride API](https://github.com/hasadna/open-bus-stride-api)**
   (Hasadna / open-bus project) — used automatically when Curlbus is down,
   and always for the stop's name/location. Stride's SIRI ingestion can lag
   realtime by 2-20 minutes, so on this path ETAs are projections from each
   bus's last known position along its route, matched to the GTFS timetable.

The footer of the page shows which source is active.

## What it shows

For each bus arriving within the next 20 minutes: line number, destination,
operator, ETA (live countdown in minutes plus clock time). All text is
English. The board is a passive single-screen display — no scrolling or any
other interaction; it always fits one viewport and shows at most the next 8
arrivals. Rows marked **live** (green pulsing dot) are buses actually
reporting en route; **scheduled** rows are predictions for buses that
haven't departed yet (or pure timetable entries on the Stride fallback
path).

## Tests

```bash
python3 -m unittest test_server -v
```

Stdlib `unittest` only, no test dependencies. Offline unit tests (mocked
upstreams) lock down the API contract, the 20-minute window, English-only
output, live/scheduled logic, ETA math and the fallback path; live
integration tests assert data freshness (< 3 min) and refresh speed
(< 10 s cold, instant cached), and skip if the network is down.

## Notes

- `GET /api/arrivals` is cached for 20 s server-side to be polite to the
  upstream services; the page polls every 30 s.
- To use a different stop, change `STOP_CODE` in `server.py` (the code on the
  physical stop sign / in Google Maps).
- If you ever want to cut out the middleman entirely: the MOT offers direct
  SIRI SM access — sign the request form and email it to ptsupport@mot.gov.il
  (see gov.il "מידע בזמן אמת – ממשק למפתחים"), and you get a personal access
  key to query their server directly.

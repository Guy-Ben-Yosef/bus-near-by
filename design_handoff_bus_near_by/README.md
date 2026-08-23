# Handoff: Bus Near By — Stop Display Board

## Overview
A passive, non-interactive, single-viewport departure board pinned to one street corner in Tel Aviv (Milano Square / Ibn Gvirol). It answers one question at a glance from a distance: "which buses are about to reach my stop?" There is NO user input of any kind — no scroll, no taps. It runs full-screen on an early-2000s 4:3 monitor.

The display covers **4 physical stops** (the user lives on a corner), shown as **2 alternating screens**:
- Screen 1 — N/S: stop **25893 SOUTHBOUND (↓)** on the left, stop **23012 NORTHBOUND (↑)** on the right
- Screen 2 — E/W: stop **20676 WESTBOUND (←)** on the left, stop **25894 EASTBOUND (→)** on the right

Screens alternate **every 10 seconds** with a "split-flap" (Solari board) character-shuffle transition.

## About the Design Files
`Bus Near By.dc.html` is a **design reference / working prototype built in HTML** — it shows the intended look and behavior with simulated data, not production code to ship. The task is to recreate this design in your target environment (any framework, or even a static page + JS), wired to the real arrival-data API.

## Fidelity
**High-fidelity.** Colors, typography, spacing, and all behaviors below are final. Recreate pixel-perfectly.

## Canvas & Scaling
- Design canvas: fixed **1024×768** (4:3), padding 30px top/bottom, 44px left/right.
- The canvas is centered in the physical viewport and uniformly scaled: `scale = min(vw/1024, vh/768)`.
- Everything must always fit; nothing ever overflows the viewport.

## Design Tokens
Colors:
- Background: #0d0d0d
- Primary text: #e8e8e8
- Secondary text / headers-muted: #8a8a8a
- Tertiary / faded (SCHD rows, footer): #9a9a9a, #6a6a6a
- Hairlines: #3a3a3a (strong), #222222 (medium), #1c1c1c (row separators)
- MIN value colors: fresh live = **#8fbc8f** (gentle green), stale live = **#d99a4e** (orange), scheduled = **#9a9a9a**
- Transition glyph gray: #5a5a5a

Typography — one family only: **IBM Plex Mono** (Google Fonts, weights 400–700). All text UPPERCASE. `font-variant-numeric: tabular-nums` on all numbers/clock.
- Title + clock: 26px / 600
- Section direction header: 22px / 600, letter-spacing 0.04em
- Stop code: 16px, #8a8a8a
- Column headers: 14px, #8a8a8a, letter-spacing 0.08em
- Line number: 30px / 700
- Destination: 22px
- Meta line (ETA · STAT · OPERATOR): 15px
- MIN value: 34px / 700
- Footer: 14px

## Layout (top to bottom)
1. **Header row** (flex, space-between, baseline): left `BUS NEAR BY · MILANO SQUARE`, right live clock `HH:MM:SS` (24h).
2. **Content area**: two equal columns with 40px gap (one per stop of the current screen). Each column:
   - Direction header: `{arrow} {DIRECTION}` left, `STOP #{code}` right; 1px #3a3a3a bottom border, 8px padding-bottom, 22px margin-top.
   - Column header row, grid `72px 1fr 96px`: LINE / DESTINATION / MIN (right-aligned); 1px #222 bottom border.
   - **Rows viewport: fixed height 270px, overflow hidden** (= exactly 3 rows of 90px).
   - Row, grid `72px 1fr 96px`, height 90px, align center, 1px #1c1c1c bottom border:
     - LINE: line number, 30px/700
     - middle cell, 2 lines: destination (22px, ellipsis on overflow; #e8e8e8 live / #9a9a9a scheduled) then meta `{ETA HH:MM} · {LIVE|SCHD} · {OPERATOR}` (15px; LIVE in #c9c9c9, SCHD in #6a6a6a, rest #6a6a6a)
     - MIN: minutes (34px/700, right) or `NOW` when <1 min — static, no blinking
3. **Footer** (flex, space-between; 1px #3a3a3a top border, 10px padding-top, 16px margin-top):
   - Left legend: `■ LIVE · ■ LIVE, UPDATE DELAYED · ■ SCHEDULED` (squares in green/orange/gray from tokens)
   - Center: page indicator `[ N/S ]  E/W` / `  N/S  [ E/W ]` (brackets mark active screen; monospace, whitespace preserved)
   - Right: `NEXT 10 MIN · REFRESH 15S`

## Data & Business Rules
- Fetch arrivals **every 15 seconds**; countdown re-renders every 1 second.
- Show only buses due within the **next 10 minutes** per stop; sorted soonest first.
- **3 rows visible max.** If more than 3 qualify: auto-scroll vertically inside the 270px viewport — list duplicated once, CSS keyframes translateY(0 → -50%), duration = rowCount × 5s, linear, infinite, with a hold at start/end (keyframes: 0%,8% at 0; 92%,100% at -50%).
- If none qualify: centered `NO BUSES EXPECTED IN THE NEXT 10 MINUTES` (17px, #6a6a6a) in the 270px area.
- `MIN` shows `NOW` when < 1 minute; buses drop off ~45s after due time.
- Each arrival carries: line number, English destination, operator name, due time, live/scheduled flag, and **per-vehicle last-update timestamp**.
- MIN color logic (live buses only): update age ≤ 45s → green #8fbc8f; > 45s → orange #d99a4e. Scheduled → #9a9a9a.
- **Error state** (feed unavailable): full content area replaced with centered `ARRIVAL DATA UNAVAILABLE` (30px/600) + `LAST UPDATE {X} AGO · ATTEMPTING RECONNECT` (18px, #8a8a8a). Header/footer remain.

## Screen Alternation & Transition
- Every **10s**, toggle N/S ↔ E/W.
- Default transition: **split-flap** — swap data instantly, then for **1.5s** every character of dynamic text (line, destination, operator, ETA, status, MIN, direction label, stop code) flickers through random A–Z/0–9 glyphs and settles **left-to-right** (char i settles when progress ≥ 0.15 + (i/len)×0.8; re-randomize unsettled chars every ~70ms; spaces never scramble). Monospace keeps widths stable.
- Guard against overlapping transitions (ignore a trigger while one is running).
- Alternate transitions exist in the prototype (dissolve = ASCII ░▒▓ noise overlay; wipe = block-character sweep; crt = vertical collapse; plain fade) behind a `transition` prop — split-flap is the chosen default; others optional.

## State Management
- `now` (1s tick), `buses[]` (refreshed 15s), `page` (0=N/S, 1=E/W, 10s), `flap` progress (null when idle), `fitScale`, error flag.

## Mock Data in the Prototype
Stops keyed N/S/E/W; each bus: stop, line, destination, operator, due time, live flag, updatedAt. Simulated refresh nudges live ETAs ±20s, promotes near-term scheduled buses to live, and randomly refreshes updatedAt (70%) so green/orange states occur.

## Assets
None — no images or icons. Arrows are Unicode ↑ ↓ ← →; legend squares are the ■ character. Font from Google Fonts (IBM Plex Mono 400/500/600/700).

## Files
- `Bus Near By.dc.html` — the full prototype (markup, styles inline, logic class with all timing/color rules). Read it as the source of truth for any measurement not listed above.

## Screenshots
In `screenshots/` (captured from the running prototype):
- `01-arrivals-east-west.png` — E/W screen, arrivals
- `02-arrivals-north-south.png` — N/S screen, arrivals (incl. NOW state and SCHD row)
- `03-split-flap-transition-mid.png` — mid split-flap transition (characters scrambled, settling left-to-right)
- `04-empty-state.png` — per-stop empty state
- `05-error-state.png` — feed-unavailable error state

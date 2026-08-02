# Proactive Assistant

Proactive Assistant is a FastAPI application that predicts likely ride and food actions before the user explicitly requests them. It combines:

- Persistent behavioral memory in SQLite
- Authenticated browser automation with Playwright
- A multi-agent trigger pipeline: a central orchestrator dispatching to four sub-agents over asyncio message queues
- Live data hydration for Uber and Swiggy
- A deterministic offline evaluation harness over a labelled 200-session hold-out set
- A single-page frontend for confirmation, feedback, and inspection

## The hard part is not prediction

Prediction here is a `GROUP BY`, not a model. The engineering is in two places:

**Making a habit countable.** Raw history does not group: 08:58 and 09:04 are the
same habit but different timestamps, and GPS drift makes one office look like
forty distinct places. So departure times are binned into 15-minute windows
(`(hour*60 + minute) // 15`, 96 bins/day) and destinations are clustered by
haversine proximity. Only then is `(weekday, bin, cluster, platform, ride_type) →
frequency` a meaningful count.

**Earning the right to interrupt.** A learned window is ±20 minutes wide and the
watcher polls every 60 seconds, so a matching pattern keeps matching for ~40
consecutive ticks. A system that notifies on each one is *correct* 40 times and
unusable — measured, that is **1,047 notifications for 100 genuine habits, 7.2%
precision.** Turning "correct" into "useful" is what the cooldown policy and the
evaluation harness in [`eval/`](eval/) exist for:

| | no cooldown | tuned (60/45 min) |
|---|---|---|
| Precision | 7.16% | **96.15%** |
| Recall | 75.00% | **75.00%** (unchanged) |
| Notifications / session | 5.235 | **0.390** |
| Duplicate spam | 939 | **0** |

Measured over 200 labelled sessions, 18,200 replayed ticks per configuration. Full
method, the saturation argument for choosing 60/45, and the limits of what this can
claim: [eval/RESULTS.md](eval/RESULTS.md).

The system is designed around two product surfaces:

- Ride Assistant: predicts likely next rides, validates them against live pricing and ETA data, and exposes a one-tap confirmation flow
- Food Assistant: predicts likely next orders, checks live restaurant availability and delivery timing, and presents alternatives when the preferred option is weak or unavailable

## Local Setup

### Prerequisites

- Python 3.10 or newer
- Chromium installed through Playwright
- Network access for third-party pages and public APIs

### Install

```bash
cd proactive_assistant
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Environment variables

Create `.env` in the project root:

```env
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b
GEOCODING_API=
PORT=8000
```

Notes:

- `GROQ_API_KEY` is optional. Without it, the app falls back to deterministic explanations and ranking.
- `GEOCODING_API` is used for geocoding support.
- `PORT` is currently not consumed by `run.py`; local dev starts on `8001` unless you run uvicorn manually.

### Run

Using the provided entry point:

```bash
python run.py
```

This starts the server on:

```text
http://localhost:8001
```

Equivalent uvicorn invocation:

```bash
uvicorn proactive_assistant_app.app:app --host 0.0.0.0 --port 8001 --reload
```

### First-run sequence

1. Open `http://localhost:8001/login`
2. Authenticate into the application shell
3. Connect Uber from the rides flow
4. Connect Swiggy from the food flow
5. Run history sync
6. Let pattern extraction populate behavioral memory
7. Reload the main UI and inspect `/rides`, `/food`, and `/api/trigger/log`

## Walkthrough

Loom: https://www.loom.com/share/e2c98321cf0f48b69455ce2a091ebe58

## What the System Actually Does

### Account connection

The application does not ask the user to export cookies or manually inject credentials. Instead, account connection is performed inside a remote Playwright browser session:

- Uber login is opened in a mobile-emulated Chromium context
- Swiggy login is opened in a desktop Chromium context
- The frontend browser viewer streams screenshots from the headless page
- User clicks and keystrokes are relayed back into Playwright over API calls
- Once login is detected, the authenticated session is persisted locally and reused for subsequent scraping

Uber persists cookies to `proactive_assistant_app/browser_session/cookies.json`.

Swiggy persists Playwright storage state to `proactive_assistant_app/browser_session/swiggy_storage_state.json`.

### Background prediction loop — the agent pipeline

At application startup:

1. SQLite schema initialization runs
2. Pattern extraction is launched in a background thread
3. `TriggerWatcher` starts, which starts the `Orchestrator` and its four agents

`TriggerWatcher` owns only *when* evaluation happens (every 60 seconds).
`Orchestrator` owns *what it decides*. Every 60 seconds, one `tick()`:

```
Orchestrator ──TICK──► ① RidePatternAgent ─┐
             ──TICK──► ② FoodPatternAgent ─┴─► ③ LiveContextAgent ──► ④ DecisionAgent
                       (run concurrently)        (enrich, may fail)     (policy only)
```

| Agent | Responsibility | Deadline |
|---|---|---|
| ① `RidePatternAgent` | Does a learned departure window match now? Pure DB read. | 5 s |
| ② `FoodPatternAgent` | Does a learned ordering window match now? Pure DB read. | 5 s |
| ③ `LiveContextAgent` | Traffic / delivery-ETA enrichment. The **only** agent that touches the network. | 12 s |
| ④ `DecisionAgent` | Cooldowns, dismissal thresholds, suppression, duplicate-action checks. The **only** place policy lives. | 8 s |

Agents never call each other — they only put messages on `asyncio.Queue` inboxes,
and the orchestrator is the only component that knows the pipeline's shape.

**Why queues instead of direct awaits.** The previous implementation awaited ride
evaluation, then food evaluation, then a Playwright scrape, in one sequence. That
meant (a) two independent evaluations ran serially, (b) one hung third-party
scrape stalled the entire loop including the cheap deterministic work, and (c) any
exception killed the cycle. Each agent now runs under its own deadline and catches
its own failures, so a timeout degrades one stage instead of the tick. A failed
agent still *replies* (with `NO_CANDIDATE`), because a silent agent would make the
orchestrator burn its whole join deadline waiting for a reply that never comes.

**Why policy is isolated in one agent.** Separating feature extraction ("does a
pattern match?") from policy ("should we interrupt?") is what lets `eval/` hold
extraction fixed and sweep only `TriggerPolicy` — so a measured precision change
is attributable to the policy and nothing else. All tunable knobs live in one
frozen dataclass, `TriggerPolicy`.

At most **one** notification is emitted per tick. Rides outrank food at equal
confidence: a missed departure costs the user a late arrival, a missed meal prompt
does not.

Every message carries a `trace_id`, so all work produced by one tick can be
reconstructed from logs after a misfire.

### Prediction mechanics

The current implementation is primarily deterministic:

- ride patterns are derived from historical departure windows and destination clusters
- food patterns are derived from historical order windows, cuisine frequency, and restaurant preference scores
- live data is fetched only after the system has a candidate worth showing

Groq is optional and is used in two narrow places:

- concise natural-language reason generation
- ranking current ride candidates when an API key is configured

If no Groq key is provided, the system remains functional and falls back to deterministic ranking.

## Architecture

### Backend

- FastAPI application: `proactive_assistant_app/app.py`
- Entry point: `run.py`
- Default local dev port: `8001`

### Persistence

- SQLite database: `proactive_assistant_app/assistant.db`
- WAL mode enabled
- Stores raw history, extracted patterns, trigger logs, suggestions, feedback, scraper cache, scraper snapshots, platform connection metadata, and app settings

### Core modules

- `app.py`: API surface, startup lifecycle, authentication cookie handling, live context aggregation, page serving
- `database.py`: persistence layer and behavioral memory primitives. `DB_PATH` honours `$ASSISTANT_DB_PATH`, and the clock-dependent queries accept an injectable `now`, so an offline replay can evaluate the policy at a simulated timestamp
- `pattern_engine.py`: extraction of ride and food patterns plus feedback reweighting
- `agents.py`: `Orchestrator`, the four sub-agents, and `TriggerPolicy` — all trigger decision logic
- `trigger_watcher.py`: the timing shell (poll interval + task lifecycle) that drives `Orchestrator`
- `suggestion_builder.py`: converts triggers into actionable suggestions and hydrates them with live data
- `uber_client.py`: Uber Playwright login, history scraping, live estimate scraping, deeplink generation
- `food_client.py`: Swiggy Playwright login, order-history scraping, top-restaurant scraping, live restaurant matching
- `data_fetcher.py`: timeouts, fallback execution, cache reuse, stale-data handling, scraper health tracking
- `suggestion_engine.py`: legacy / auxiliary Groq-backed ride ranking path still used by current ride suggestion APIs
- `reason_generator.py`: optional one-line explanation generation with deterministic fallback

### Frontend

- Static SPA served from `proactive_assistant_app/static/`
- Main files:
  - `index.html`
  - `app.js`
  - `styles.css`
  - `login.html`

The frontend is route-driven and exposes:

- `/`
- `/rides`
- `/food`

## Data Model

The SQLite schema is not just a storage layer for scraped history. It is the system's memory.

### Raw history tables

- `rides`
- `orders`

These store normalized Uber ride history and Swiggy order history, including timestamps, pricing, coordinates, labels, and metadata required for downstream inference.

### Learned pattern tables

- `departure_patterns`
- `destination_clusters`
- `order_patterns`
- `restaurant_patterns`
- `cuisine_by_day`

These tables represent the learned behavioral model used by the trigger engine.

### Triggering and feedback tables

- `trigger_log`
- `suggestions`
- `dismissed_suggestions`
- `confirmed_suggestions`
- `pattern_feedback`

These tables allow the system to:

- know when it last triggered
- know what it showed
- know whether the user confirmed, dismissed, edited, or ignored the suggestion
- suppress patterns that are repeatedly rejected

### Runtime resilience tables

- `scraper_cache`
- `scraper_snapshots`
- `platform_connections`
- `app_settings`

These support session persistence, stale fallback data, scraper health reporting, and connection state.

## Trigger Logic

Trigger evaluation lives in `trigger_watcher.py`.

### Poll frequency

- The watcher runs every `60 seconds`

### Ride triggers

Ride suggestions are considered when:

- the current time is within a 20-minute tolerance of a learned departure window
- the pattern confidence passes threshold
- the user has not already booked a ride in that same window today
- the pattern is not suppressed due to repeated dismissals

The watcher also compares live route travel time to historical travel time:

- if live travel time exceeds historical average by 25 percent, the trigger is strengthened
- this adds `traffic_deviation` as a trigger reason
- it can shift the recommended departure earlier

Ride annoyance controls:

- base cooldown: `45 minutes`
- confidence threshold increases after repeated dismissals
- 4 dismissals in the recent window suppress the pattern
- pending suggestions expire after 10 minutes and count as ignored feedback

### Food triggers

Food suggestions are considered when:

- the current time is within a 15-minute tolerance of a learned ordering window
- the pattern confidence passes threshold
- the user has not already ordered from that restaurant in the same window today
- the pattern is not suppressed due to dismissals

The watcher also compares current delivery ETA to historical delivery time:

- if current ETA exceeds historical ETA by 30 percent, the trigger is strengthened
- this adds `delivery_delay` as a trigger reason

Food annoyance controls:

- base cooldown: `30 minutes`
- repeated dismissals increase the threshold
- 4 dismissals suppress the pattern

## Pattern Extraction

Pattern extraction lives in `pattern_engine.py` and runs:

- at startup
- after ride history sync
- after food history sync

### Ride pattern extraction

The engine:

- clusters destinations by label similarity and geographic proximity
- converts departure times into 15-minute bins
- groups rides by weekday, hour bin, destination cluster, platform, and ride type
- stores only patterns with enough support to be meaningful

### Food pattern extraction

The engine:

- groups orders by weekday and 15-minute order windows
- tracks cuisine frequency per day
- builds restaurant preference scores using frequency and recency
- stores recent, high-signal restaurant preferences for live matching

### Feedback reweighting

When the user confirms or dismisses a suggestion, the engine updates pattern state:

- confirmations increase frequency and refresh recency
- dismissals increase dismissal count
- repeated dismissals suppress the pattern
- confirmed edits can create new pattern variants

## Live Data Hydration and Fallback Strategy

The system does not trust scraped live data as always available.

### General fetch strategy

`data_fetcher.py` wraps critical live data calls with:

- an 8-second primary timeout
- a 5-second fallback timeout
- scraper cache writes
- stale-cache reuse
- scraper health tracking

If both primary and fallback fail:

- the most recent cached payload is returned as a `CachedResult` if available
- otherwise the result becomes `DataUnavailable`

### Uber live data

For rides, the system fetches:

- live estimates from `m.uber.com`
- network responses containing fare and product data
- DOM candidates as a secondary extraction channel

If precise live data is missing, the suggestion still survives with:

- historical destination confidence
- deeplink generation
- explicit fallback messaging

### Swiggy live data

For food, the system fetches:

- top restaurants from Swiggy internal listing endpoints
- live restaurant matching for the preferred restaurant and item
- delivery time and deeplink data

If the preferred restaurant is weak or unavailable:

- the builder switches to historical restaurant alternatives

### UI-visible fallback behavior

The UI is designed to degrade gracefully:

- geolocation falls back to IP lookup
- weather, air quality, and reverse geocoding reuse cached values when live lookups fail
- food and ride panels render non-blocking placeholders while background fetches resolve
- cached or stale scraper results are still surfaced when they are better than showing nothing

## Playwright Integration

### Uber

Uber automation in `uber_client.py` has two distinct modes:

- login/browser-viewer mode
- scraping/live-quote mode

Login flow:

- launches headless Chromium with mobile emulation
- streams screenshots to the frontend
- relays click, type, and key events from the UI
- tracks popup windows for OAuth flows
- treats login as complete when redirected away from auth pages or when session cookies such as `sid` / `csid` are present

History sync:

- opens `https://riders.uber.com/trips`
- captures CSRF tokens from the page's own requests
- replays Uber's internal GraphQL `Activities` query in-page
- paginates through `nextPageToken`
- augments records with trip-detail extraction when needed

Live quotes:

- opens `https://m.uber.com/looking?...`
- listens to fare/product network responses
- parses DOM price cards as a secondary extraction channel

### Swiggy

Swiggy automation in `food_client.py` also has two modes:

- login/browser-viewer mode
- authenticated same-origin data fetch mode

Login flow:

- launches headless Chromium in desktop mode
- persists Playwright `storage_state`
- verifies login by checking account URLs or account/profile DOM markers

History sync:

- reuses the authenticated browser context
- loads the orders page
- executes same-origin `fetch` requests inside the page for `/dapi/order/all`
- paginates order history
- falls back to DOM scraping if the internal API path fails

Restaurant discovery:

- fetches `/dapi/restaurants/list/v5` from within the authenticated page context
- falls back to homepage DOM scraping if the endpoint fails

## Authentication

The app uses a simple application-level auth gate for access to the UI:

- manual login via `/api/auth/login`
- optional Google Sign-In via `/api/auth/google`
- session stored in an HTTP-only cookie

Google Sign-In is optional. If `GOOGLE_CLIENT_ID` is not configured, the rest of the application still works.

This auth layer controls access to the SPA. Platform-specific authentication for Uber and Swiggy is separate and managed via Playwright sessions.

## API Overview

### UI and auth

- `GET /login`
- `GET /`
- `GET /rides`
- `GET /food`
- `GET /api/auth/status`
- `POST /api/auth/login`
- `GET /api/auth/google/config`
- `POST /api/auth/google`
- `POST /api/auth/logout`

### Live context

- `GET /api/context/live`
- `GET /api/geocode`

### Uber

- `GET /api/uber/status`
- `POST /api/uber/login`
- `GET /api/uber/screenshot`
- `POST /api/uber/click`
- `POST /api/uber/type`
- `POST /api/uber/key`
- `POST /api/uber/finish-login`
- `POST /api/uber/sync-history`
- `GET /api/uber/history`

### Food

- `GET /api/food/status`
- `POST /api/food/login/{provider}`
- `GET /api/food/screenshot`
- `POST /api/food/click`
- `POST /api/food/type`
- `POST /api/food/key`
- `POST /api/food/finish-login/{provider}`
- `POST /api/food/sync-history`
- `GET /api/food/history`
- `GET /api/food/top-restaurants`

### Patterns and predictions

- `GET /api/history/sync`
- `GET /api/patterns/summary`
- `GET /api/ride/patterns`
- `GET /api/food/patterns`
- `GET /api/suggestions/current`
- `GET /api/ride/suggestion`
- `GET /api/food/suggestion`
- `POST /api/suggestions/{suggestion_id}/confirm`
- `POST /api/suggestions/{suggestion_id}/dismiss`
- `POST /api/ride/confirm`
- `POST /api/ride/dismiss`
- `POST /api/food/confirm`
- `POST /api/food/dismiss`
- `GET /api/trigger/log`

### Health and realtime

- `GET /api/health`
- `GET /health`
- `WS /ws/suggestions`

## Operating Notes

### Session artifacts

Playwright session files are stored under:

- `proactive_assistant_app/browser_session/`

These files contain authenticated browser state and should be treated as sensitive local credentials.

### Local database

SQLite database path:

- `proactive_assistant_app/assistant.db`

To reset local state, remove the database and browser session artifacts, or use the provided cleanup script:

```bash
./clear_data.sh
```

### Debug output

Uber history scrape debug output is written to:

- `proactive_assistant_app/scrape_debug/uber_history_last.json`

### Health inspection

Use:

- `/health`
- `/api/health`
- `/api/trigger/log`
- `/api/patterns/summary`

to inspect scraper health, trigger behavior, and extracted memory state.

## Testing

```bash
pytest                      # 32 tests
```

- `tests/test_agents.py` — 22 tests on the agent pipeline: policy (cooldowns,
  soft-confidence tier, dismissal escalation and lookback expiry, cross-domain
  global cooldown), agent mechanics (a throwing agent still replies; a hung agent
  times out and replies; an agent survives repeated failures; live enrichment
  degrades instead of dropping a candidate), and orchestration (both pattern
  agents dispatched every tick, at most one notification per tick, policy
  hot-swappable between ticks).
- `tests/test_live_uber_estimates.py`, `tests/test_suggestion_builder_geocoding.py`,
  `tests/test_ola_client.py`, `tests/test_uber_history_quality.py` — scraping and
  hydration.

## Evaluation

The trigger policy is measured offline against a labelled hold-out set rather
than eyeballed. See [`eval/`](eval/) and [`eval/RESULTS.md`](eval/RESULTS.md).

```bash
python eval/generate_dataset.py       # 200 labelled sessions, fixed seed
python eval/run_eval.py               # precision / recall / trigger frequency
python eval/tune_cooldown.py          # sweep the cooldown policy (~1 h)
python eval/diagnose_recall.py        # which stage drops each missed habit
python eval/explain_confidence.py     # the count/total arithmetic for one session
python eval/diagnose_dismissals.py    # why dismissal suppression leaks
```

The harness drives the **same `Orchestrator` the app runs**, with the clock, the
database path and the live-data fetcher injected. It is not a reimplementation —
that distinction is why the numbers mean anything.

### Real labels, not just synthetic ones

The dataset above is synthetic, which validates the mechanism but says nothing
about real users. [`labeling/`](labeling/) is the pipeline that produces the real
version: it runs the predictor in shadow mode to capture what it *nearly* sent,
stratify-samples six strata with recorded inclusion probabilities, asks the
participant the one question that is not already in the behaviour log — *would
this have been welcome or annoying?* — and exports into the **same schema**
`eval/run_eval.py` reads.

```bash
python -m labeling.run_pipeline demo    # whole pipeline against a simulated respondent
```

See [`labeling/README.md`](labeling/README.md) for the methodology, the four
validity threats it is built around, and the limitations it cannot fix (chiefly:
it measures perceived usefulness, not incremental conversion — that needs a
randomised holdout).

### Known defects the evaluation surfaced

Recall is capped at 75% and the cause is a single modelling choice with three
symptoms. Fixed 15-minute bins split a habit that straddles a boundary into two
half-strength patterns:

1. **Recall ceiling** — neither half clears the confidence threshold.
2. **Dismissal suppression leaks** — dismissal counts are keyed per pattern ID, so
   dismissing one half leaves its twin unsuppressed and the rejected suggestion
   returns under a different ID.
3. **Every remaining false positive** at the tuned setting traces to that leak.

A second, independent defect: `confidence = count / all events that weekday` scores
every habit lower for users who simply do more things on that weekday, which is
backwards. It should be a recurrence rate (`weeks_occurred / weeks_observed`).

Neither is visible by reading the code — binning is implemented correctly and a ratio
is a reasonable confidence. They are modelling mistakes, found by measuring the
outcome and refusing to accept a number that could not be explained. Both are
diagnosed and neither is fixed yet.

## Current Constraints

- The application is optimized for local single-user operation
- Browser automation depends on third-party site structure and internal endpoints
- Some environment variables documented in `.env` are optional rather than mandatory
- `run.py` pins development startup to port `8001`
- The UI auth cookie is application-level only; it is not a substitute for Uber or Swiggy authentication

## Summary

This codebase is not a static demo. It is a stateful local system with:

- authenticated platform sessions
- persistent behavioral memory
- background trigger evaluation
- live data hydration
- explicit fallback and cache reuse
- feedback-driven suppression and reweighting

That combination is the core of the product: not just prediction, but prediction that remains operational under unreliable external integrations.

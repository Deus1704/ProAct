# Proactive Assistant: Uber-Only Implementation Plan

## Goal

Build an end-to-end Uber-first proactive ride assistant that:

- learns commute behavior from ride history
- decides when to proactively suggest a ride
- fetches live Uber data when possible
- explains why it is suggesting the ride
- lets the user edit details before confirming
- shows a local confirmed state without automating the actual booking flow

---

## Current Product Direction

### What we should build now

- A single-user Uber-only MVP
- FastAPI backend
- SQLite persistence
- Static frontend served by the backend
- Uber OAuth scaffolding
- Uber deep-link handoff instead of true in-app embedding
- Mock fallback mode when Uber API access is incomplete or blocked

### What we should not build right now

- Full multi-platform support
- Actual ride booking automation
- Browser-use or AI-agent-driven booking flows
- A large agentic system as the main happy path
- Production auth/account management

---

## Key Product Decision

### Can Uber be embedded in our app?

No, not as a real iframe-style embedded Uber web app.

Reason:

- Uber’s old embeddable widget is deprecated
- `m.uber.com` returns `X-Frame-Options: SAMEORIGIN`
- browsers will block loading it inside our product iframe

### Best supported UX

We should:

- keep the proactive suggestion inside our UI
- let the user review and edit pickup/dropoff
- generate an Uber deep link
- open Uber in a new tab or app handoff
- show a local confirmed state inside our platform

---

## Uber Dashboard Setup

## What to fill in next

### Authentication

- Keep `Authenticate with Client Secret` for now

### Redirect URI

Add:

```text
http://localhost:8000/auth/uber/callback
```

If we use a different local port later, update this accordingly.

### Origin URI

Add:

```text
http://localhost:8000
```

### Privacy Policy URL

Use a temporary placeholder for local development, such as:

```text
http://localhost:8000/privacy
```

We can serve a tiny local page there.

### Public Display Name

Use something like:

```text
Pokus Commute Assistant
```

Do not include the word `Uber` in the public app name.

### Public Description

Use something like:

```text
A proactive commute assistant that helps users review and launch rides based on learned routines and live conditions.
```

### Access Token TTL

- `30 days` is fine for the MVP

---

## Architecture To Build

## 1. Backend

### Stack

- FastAPI
- SQLite via Python `sqlite3`
- `requests`
- `python-dotenv`
- `uvicorn`

### Responsibilities

- serve frontend files
- manage Uber OAuth
- persist settings/tokens/history/interactions
- compute ride suggestions
- fetch Uber live data when possible
- fall back to demo data when Uber access fails

---

## 2. Persistence Layer

### Database

Use one local SQLite database file.

### Tables

#### `app_settings`

Store:

- Uber client ID
- Uber client secret reference if needed locally
- OAuth access token
- refresh token if returned
- token expiry
- user display name
- demo mode flag
- last sync time
- OAuth state nonce

#### `ride_history`

Store:

- external ride ID
- source platform
- pickup address
- dropoff address
- request timestamp
- weekday
- hour of day
- ride type
- price
- duration minutes
- pickup ETA if available
- raw payload snapshot

#### `ride_interactions`

Store:

- action type: `confirm`, `dismiss`, `edit`
- timestamp
- suggestion payload
- edited fields

---

## 3. Uber Integration

## Phase A: OAuth and Deep Links

Implement:

- build Uber authorize URL
- handle callback
- exchange authorization code for token
- store token
- generate Uber rider deep links

## Phase B: Live Data

Implement if token/scopes allow:

- fetch rider profile
- fetch ride history
- fetch product list
- fetch ETA estimates
- fetch price estimates

## Phase C: Graceful Fallback

If Uber API access is blocked or incomplete:

- seed demo ride history
- simulate a realistic weekday commute pattern
- surface clear UI messaging that live sync is unavailable
- keep the product usable

---

## 4. Suggestion / Trigger Engine

### Inputs

- current timestamp
- weekday
- historical ride frequency
- historical average departure time
- route frequency
- confirmation history
- dismissal history
- live Uber ETA/fare if available
- current route travel time versus historical average

### Logic

#### Step 1: Find likely route

- group past rides by weekday and time bucket
- identify the most common route for the current context
- prefer recent behavior when frequencies are close

#### Step 2: Estimate confidence

Combine:

- route frequency
- recency
- day-of-week match
- time-window match
- whether user previously confirmed similar suggestions

#### Step 3: Avoid annoyance

Apply penalties for:

- recent dismissals
- already confirmed today
- low confidence
- suggestion shown too recently

#### Step 4: Build suggestion

Return:

- pickup
- destination
- suggested departure time
- ride type
- estimated fare
- estimated pickup time
- traffic delta versus usual
- explanation string

### Example explanation

```text
You usually leave for work around 9:15 AM on weekdays. Traffic is about 20 minutes slower than your normal route, so leaving now is recommended.
```

---

## 5. Frontend

### Main screen

One clean Uber-first dashboard:

- greeting
- connect Uber button
- sync history button
- suggestion reason
- ride detail cards
- editable pickup and destination
- live estimate block
- confirm button
- launch Uber button
- fallback notice if live data is unavailable

### Important UI states

#### Disconnected

- show `Connect Uber Account`
- show that demo mode can still be used

#### Connected, not synced

- show `Sync Ride History`

#### Suggestion ready

- show why this was suggested
- show editable fields
- show live or fallback estimates

#### Confirmed

- show local confirmed state
- keep launch-Uber button available

#### Live data failure

- show a non-scary fallback message
- keep suggestion usable with cached/demo values

---

## 6. File Structure To Create

```text
proactive_assistant_app/
├── __init__.py
├── app.py
├── database.py
├── uber_client.py
├── suggestion_engine.py
└── static/
    ├── index.html
    ├── app.js
    └── styles.css
```

### Other files to update

- `main.py`
- `pyproject.toml`
- `README.md` or a new project-specific README
- `.env.example` if needed

---

## Backend Endpoints To Implement

### App / utility

- `GET /`
- `GET /privacy`
- `GET /api/health`

### Uber auth

- `GET /api/uber/status`
- `GET /api/uber/auth-url`
- `GET /auth/uber/callback`

### Uber sync

- `POST /api/uber/sync-history`
- `GET /api/uber/history`
- `GET /api/uber/deeplink`

### Suggestion flow

- `GET /api/ride/suggestion`
- `POST /api/ride/confirm`
- `POST /api/ride/dismiss`
- `POST /api/ride/edit`

---

## Data Strategy

## Best case

Use actual Uber account data:

- OAuth login
- ride history
- live estimates

## Mid case

Use:

- real OAuth and deep links
- mock or seeded history
- partial live estimate support

## Worst case but still demoable

Use:

- local demo seed data
- local trigger logic
- deep-link handoff
- clear explanation that live sync is temporarily unavailable

This is still acceptable for the assignment if documented honestly.

---

## Browser Automation / AI Agent Decision

### What we should do

- Keep the main implementation API-first where possible
- Consider browser automation only as a fallback for history import if Uber API access is blocked

### What we should not do right now

- Make Browser Use the main system architecture
- Let an AI agent handle live ride booking
- Build a fragile autonomous booking bot

### Why

- more brittle
- harder to demo reliably
- unnecessary for the assignment
- higher risk around login/session handling

---

## Build Order

## Phase 1

- create FastAPI app
- add static frontend
- add SQLite database
- add demo ride history seed
- add suggestion engine using mock data

## Phase 2

- add Uber OAuth
- add token storage
- add deep-link generation

## Phase 3

- add Uber history sync attempt
- add live estimate fetch attempt
- normalize responses into local data

## Phase 4

- polish UI
- add local confirmed/dismissed/edit flows
- add clear fallback messages

## Phase 5

- update docs
- record Loom walkthrough

---

## What the Loom Should Show

1. Connect Uber
2. Sync ride history or show fallback demo seed
3. Display the learned commute pattern
4. Show current suggestion and explanation
5. Edit pickup or destination
6. Confirm ride locally
7. Launch Uber through deep link
8. Show fallback behavior when live data is unavailable

---

## Risks To Watch

- Uber scopes may be unavailable under the current app setup
- OAuth may work before history endpoints do
- live estimates may require additional data normalization
- browser embedding of Uber will remain blocked
- ride booking automation should not be attempted for the MVP

---

## Immediate Next Action

If we continue implementation, the next step should be:

1. create the FastAPI app structure
2. wire localhost OAuth settings
3. build the SQLite-backed demo-first suggestion flow
4. layer Uber OAuth and live data support on top


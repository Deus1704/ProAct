# Assignment: Proactive Assistant

## Overview

Most AI tools are reactive — they wait for the user to ask. This assignment asks you to build a system that **acts first**: it learns from user behavior, identifies the right moment to intervene, and helps complete everyday tasks end-to-end.

You will build two assistants — one for rides, one for food — that share a common architecture for behavior tracking, proactive triggering, and real-world data integration.

---

## Scope

- **Deliverables:** Working codebase (deployed or locally runnable) + Loom video walkthrough of the full product
- **Tech stack:** No restrictions. You may use scraping, browser automation, agentic browsers, or APIs.

---

## Task 1 — Ride Assistant

### Scenario

A user commutes daily. They usually leave home at 9:15 AM, take the same route on weekdays, and occasionally visit a gym on Saturdays. Today it's 9:05 AM and traffic on their usual route is 20 minutes above average.

### Requirements

**Learn** from the user's past ride history across Uber, Ola, and Rapido:

- Frequent destinations by day of week and time of day
- Usual departure times
- Preferred platform and ride type (auto, bike, cab)

**Detect** when a ride is likely needed:

- Time-of-day patterns (e.g., weekday mornings)
- Calendar events or recurring schedules
- Deviations from routine (e.g., no ride booked yet but departure window is approaching)

**Suggest** a complete action:

- Origin and destination (pre-filled from learned patterns)
- Recommended departure time (adjusted for current traffic)
- Price and ETA comparison across platforms
- One-tap confirmation (displays a "Confirmed" state — no actual booking required)

### Data sources

Automation is only required for **fetching history and live data** (ETAs, prices, surge). You do not need to automate the actual booking flow.

Integrate **at least one** platform. Additional platforms are optional.

| Platform | Data to extract | Required |
| --- | --- | --- |
| Uber | Ride history, current surge pricing, ETA | Any one |
| Ola | Ride history, fare estimate, ETA | Optional |
| Rapido | Ride history, fare estimate, ETA | Optional |

---

## Task 2 — Food Assistant

### Scenario

A user orders dinner most weeknights around 8:30 PM. They tend to order biryani on Fridays and lighter meals on Mondays. Today is Friday at 8:15 PM, and delivery times on Swiggy are 15 minutes longer than usual.

### Requirements

**Learn** from the user's past order history across Swiggy and Zomato:

- Frequently ordered items and cuisines
- Ordering patterns by day of week and time
- Preferred restaurants and price range

**Detect** when an order is likely:

- Approaching the user's usual ordering window
- No order placed yet within that window
- Unusual delivery delays that warrant ordering earlier

**Suggest** a complete action:

- Restaurant and item(s) (pre-filled from learned preferences)
- Current delivery ETA and price
- Alternatives if the preferred option is slow or unavailable
- One-tap confirmation (displays a "Confirmed" state — no actual order placement required)

### Data sources

Automation is only required for **fetching history and live data** (menus, ETAs, prices). You do not need to automate the actual ordering flow.

Integrate **at least one** platform. Additional platforms are optional.

| Platform | Data to extract | Required |
| --- | --- | --- |
| Swiggy | Order history, restaurant listings, live ETAs, prices | Any one |
| Zomato | Order history, restaurant listings, live ETAs, prices | Optional |

---

## Architecture Requirements

Your code must address the following. How you structure it is up to you.

### 1. Trigger logic — When should the system act?

The system must decide on its own when to surface a suggestion. Explain (in code and in your Loom video) how this works:

- What signals does it watch? (time, patterns, external conditions)
- How does it avoid being annoying? (cooldowns, confidence thresholds, dismissal tracking)

### 2. Memory — What does the system remember?

The system must persist and use user behavior data:

- What is stored (trips, orders, timestamps, preferences)
- How the data improves suggestions over time (frequency weighting, recency decay, feedback loops)
- How past dismissals or edits feed back into future suggestions

### 3. Real-world data integration — Handling failure gracefully

You are pulling live data from third-party platforms. Things will break. Your code must handle:

- Scrapers failing or returning partial data
- Pages changing structure
- Platforms being temporarily unreachable
- Fallback behavior: what does the user see when data is incomplete?

### 4. UI — The user-facing experience

This is weighted heavily. The interface must:

- Show **why** something is being suggested (e.g., "You usually leave for work around now, and traffic is high")
- Let the user **confirm** with one tap
- Let the user **edit** any part of the suggestion (destination, time, items) before confirming
- Be clean, fast, and not require explanation to use

---

## Deliverables

| # | Item | Required |
| --- | --- | --- |
| 1 | Code — Full working codebase, pushed to a private Git repo. Must be runnable locally with a README. Add darkshredder (github.com/darkshredder) as a collaborator. | Yes |
| 2 | Loom video — End-to-end walkthrough of the product. Show the UI, demonstrate trigger logic, walk through memory and fallback behavior. No slides — show the running product. | Yes |
| 3 | README — Setup instructions, architecture overview, and any assumptions or mock data used. | Yes |
| 4 | Deployment — Live URL where the product can be tested. | Optional |

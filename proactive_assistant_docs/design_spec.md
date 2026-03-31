# Proactive Assistant: System Architecture & Flow

## 1. Overview
The Proactive Assistant is a "system of intervention" designed to transition AI from reactive (waiting for prompts) to proactive (acting on patterns). It consists of a **Python FastAPI** backend for intelligence and data coordination, and a **Next.js** frontend for a high-polish user experience.

---

## 2. Developer Perspective: System Architecture

The technical stack is chosen for scalability and ease of local setup.

### High-Level Architecture
- **API (FastAPI):** Python handles background scheduling, data processing, and Playwright automation with high efficiency.
- **Worker (APScheduler):** A lightweight intra-process scheduler that monitors triggers every minute.
- **Scraper Engine (Playwright):** Automates the extraction of ride and food history/live data from Uber and Swiggy.
- **Intelligent Memory (SQLite + Logic):** Categorizes history by time/day, calculating confidence scores and recency weights.
- **Frontend (Next.js/Tailwind):** A mobile-first UI connected via WebSockets or polling to show real-time suggestions.

### Data Model
- `History`: Raw logs from external platforms.
- `BehaviorProfile`: Aggregated patterns (e.g., "Monday, 9:15 AM -> Office").
- `Triggers`: Current conditions (Time == 9:05 AM AND Traffic > 20m).
- `Interactions`: User feedback loops (Confirm / Dismiss / Edit).

---

## 3. User Perspective: Experience Flow

The user does not "use" the app; the app "notifies" the user.

1.  **Passive Observation**: The user goes about their day. The system silently syncs their Uber and Swiggy history in the background.
2.  **Pattern Recognition**: The system identifies that today is a workday and it's almost departure time.
3.  **Condition Assessment**: The system checks live Uber ETAs and Google Maps traffic. It sees a 20-minute delay.
4.  **Proactive Intervention**: The user receives a suggestion: *"Commute to Office? Traffic is high, usual ride is 22 mins away. Start now?"*
5.  **One-Tap Action**:
    - **Confirm**: High-confidence confirmation UI.
    - **Edit**: Adjust destination or time if the routine changed.
    - **Dismiss**: The system learns to back off for similar patterns.

---

## 4. Resilience & Fallback Logic

To handle the fragility of scrapers:
- **Mock Cache**: If a scraper fails (e.g., UI change on Uber), the system serves the last known valid "Behavior Profile" using simulated data.
- **Confidence Decay**: If live data cannot be fetched, the "Confidence Score" for a suggestion automatically drops, preventing incorrect or stale suggestions.

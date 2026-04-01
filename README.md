# Proactive Assistant: Engineering & Architecture

A state-of-the-art proactive assistant that learns from your ride and food history to anticipate your needs. Built with FastAPI, Groq (LLM), and Playwright-driven browser automation.

## 🚀 Overview

The Proactive Assistant is designed to solve the "intent gap" by predicting user needs before they are explicitly requested. It integrates directly with third-party platforms (Uber, Swiggy, Zomato) to extract historical patterns and real-time environmental context, delivering curated suggestions for daily logistics and nutrition.

## 🏗️ System Architecture

### 1. Backend Orchestration (FastAPI)
The core server handles API routing, authentication, and background task management.
- **REST API**: Provides endpoints for the frontend SPA to fetch suggestions, sync history, and manage platform connections.
- **Background Workers**: Periodic tasks (via `asyncio`) manage cross-platform data synchronization without blocking the UI.
- **Provider Clients**: Modular client architecture for interacting with external platforms.

### 2. Persistence Layer (SQLite)
A structured persistence layer manages not just historical data, but also the assistant's "memory."
- **History Tables**: Normalized storage for Uber rides and Swiggy/Zomato orders.
- **Pattern Memory**: Stores extracted frequent destinations and restaurant preferences.
- **Feedback Loop**: Tracks every suggestion dismissal or confirmation to refine future LLM prompting.

### 3. Proactive Suggestion Engines
The system employs a hybrid intelligence model for triggering suggestions:
- **Groq LLM Engine**: Analyzes the raw history dump using a high-context LLM to identify non-obvious patterns (e.g., "The user always orders Italian when they work late on Tuesdays").
- **Rule-Based Fallbacks**: Ensures utility even for new users by defaulting to frequent destinations or first-order preferences when pattern confidence is low.
- **Annoyance Avoidance**: Implements cooldown timers and frequency capping to prevent "notification fatigue."

### 4. Integration & Scraping (Playwright + Hidden APIs)
The assistant uses advanced browser-assisted techniques to maintain authenticated access:
- **Session Persistence**: Uses Playwright's `storage_state` to reuse browser cookies across server restarts, avoiding frequent re-authentication.
- **GraphQL Interception**: Hooks into internal platform APIs (like Uber's GraphQL) to extract structured history that isn't available via public APIs.
- **DOM Fallback**: Heuristic scrapers parse page content if internal APIs are blocked or changed.

## 🧠 Engineering Decisions

- **Hybrid UI/Logistics Context**: The assistant doesn't just guess; it checks live ETAs and prices from Swiggy/Uber before showing a suggestion to ensure it's actionable *now*.
- **Local SQLite vs. Remote DB**: Chosen for zero-latency local lookups and simplified deployment for personal assistant use cases.
- **Browser Automation as a Bridge**: Leverages Playwright to bypass restrictive third-party API limits while keeping the user in the loop via the Browser Viewer interface.
- **Privacy-First Sync**: All data synthesis happens locally on the user's backend instance; only anonymized historical snapshots are sent to the LLM for pattern extraction.

## 🛠️ Setup & Installation

### Prerequisites
- Python 3.10+
- Playwright (`playwright install chromium`)
- Groq API Key (for LLM suggestions)

### Environment Variables (.env)
```env
GROQ_API_KEY=your_key_here
GROQ_MODEL=llama-3.1-70b-versatile
GOOGLE_CLIENT_ID=your_id_here
PORT=8000
```

### Running Locally
```bash
pip install -r requirements.txt
python run.py
```

## 🗺️ Roadmap
- [x] Multi-provider food integration (Swiggy/Zomato)
- [x] LLM-powered pattern detection
- [x] Real-time traffic enrichment for rides
- [ ] Multi-user support with isolated database instances
- [ ] Expansion to grocery and flight assistants

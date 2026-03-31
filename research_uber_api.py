"""Research script: Intercept all Uber network traffic to understand trip history API.

This script:
1. Loads saved session cookies
2. Navigates to various Uber pages that might show trip history
3. Captures ALL network requests and responses
4. Dumps everything to a JSON file for analysis
"""

import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

SESSION_DIR = os.path.join(os.path.dirname(__file__), "proactive_assistant_app", "browser_session")
COOKIES_FILE = os.path.join(SESSION_DIR, "cookies.json")
OUTPUT_FILE = os.path.join(SESSION_DIR, "uber_api_research.json")


async def main():
    if not os.path.exists(COOKIES_FILE):
        print("No cookies file found. Log in first via the app.")
        sys.exit(1)

    with open(COOKIES_FILE) as f:
        cookies = json.load(f)
    print(f"Loaded {len(cookies)} cookies")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        viewport={"width": 420, "height": 820},
        user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
        is_mobile=True,
        has_touch=True,
    )
    await context.add_cookies(cookies)

    captured = []

    page = await context.new_page()

    # Capture ALL network traffic
    async def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        status = response.status

        entry = {
            "url": url,
            "status": status,
            "content_type": ct,
            "method": response.request.method,
            "body": None,
        }

        # Only capture JSON responses from uber domains
        if "uber.com" in url and "json" in ct:
            try:
                body = await response.json()
                entry["body"] = body
            except Exception:
                pass

        # Also capture request body for POST requests
        if response.request.method == "POST":
            try:
                entry["request_body"] = response.request.post_data
            except Exception:
                pass

        captured.append(entry)

    page.on("response", on_response)

    # ── Test multiple URLs ────────────────────────────────────────────────

    urls_to_try = [
        ("m.uber.com home", "https://m.uber.com/go/home"),
        ("riders.uber.com trips", "https://riders.uber.com/trips"),
        ("m.uber.com activity", "https://m.uber.com/go/activity"),
    ]

    for label, url in urls_to_try:
        print(f"\n{'='*60}")
        print(f"Navigating to: {label} ({url})")
        print(f"{'='*60}")

        before_count = len(captured)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            # Scroll down to trigger lazy loading
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)

            final_url = page.url
            page_text = await page.evaluate("() => document.body.innerText")

            print(f"  Final URL: {final_url}")
            print(f"  Page text ({len(page_text)} chars): {page_text[:400]}")
            print(f"  New network requests: {len(captured) - before_count}")

            # Save screenshot
            await page.screenshot(path=os.path.join(SESSION_DIR, f"research_{label.replace(' ', '_')}.png"), full_page=True)

        except Exception as e:
            print(f"  ERROR: {e}")

    # ── Analyze captured data ─────────────────────────────────────────────

    print(f"\n{'='*60}")
    print(f"ANALYSIS: {len(captured)} total network responses captured")
    print(f"{'='*60}")

    json_responses = [c for c in captured if c.get("body")]
    print(f"JSON responses: {len(json_responses)}")

    for i, entry in enumerate(json_responses):
        url = entry["url"]
        body = entry["body"]
        req_body = entry.get("request_body", "")

        # Identify the GraphQL operation name
        op_name = None
        if req_body:
            try:
                req_json = json.loads(req_body) if isinstance(req_body, str) else req_body
                op_name = req_json.get("operationName")
            except Exception:
                pass

        print(f"\n--- Response #{i+1} ---")
        print(f"  URL: {url[:100]}")
        print(f"  Method: {entry['method']}")
        if op_name:
            print(f"  GraphQL Operation: {op_name}")
        if req_body:
            req_preview = req_body[:300] if isinstance(req_body, str) else json.dumps(req_body)[:300]
            print(f"  Request body: {req_preview}")

        # Show response structure
        if isinstance(body, dict):
            print(f"  Response keys: {list(body.keys())}")
            for key, val in body.items():
                if isinstance(val, dict):
                    print(f"    {key}: dict with keys {list(val.keys())[:10]}")
                    # Go one level deeper
                    for subkey, subval in val.items():
                        if isinstance(subval, dict):
                            print(f"      {subkey}: dict with keys {list(subval.keys())[:10]}")
                        elif isinstance(subval, list):
                            print(f"      {subkey}: list of {len(subval)} items")
                            if subval and isinstance(subval[0], dict):
                                print(f"        First item keys: {list(subval[0].keys())[:15]}")
                                # Print first item preview
                                preview = json.dumps(subval[0], default=str)[:300]
                                print(f"        First item: {preview}")
                        else:
                            val_str = str(subval)[:100]
                            print(f"      {subkey}: {val_str}")
                elif isinstance(val, list):
                    print(f"    {key}: list of {len(val)} items")
                    if val and isinstance(val[0], dict):
                        print(f"      First item keys: {list(val[0].keys())[:15]}")
                else:
                    print(f"    {key}: {str(val)[:100]}")

    # Save full dump
    with open(OUTPUT_FILE, "w") as f:
        json.dump(captured, f, indent=2, default=str)
    print(f"\nFull data saved to: {OUTPUT_FILE}")

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())

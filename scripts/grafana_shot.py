"""Log in to the local Grafana and save a screenshot of the netops dashboard."""
import os
import sys

from playwright.sync_api import sync_playwright

out = sys.argv[1] if len(sys.argv) > 1 else "docs/dashboard.png"
base = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3300")

with sync_playwright() as p:
    page = p.chromium.launch().new_page(viewport={"width": 1600, "height": 1000})
    page.goto(f"{base}/login")
    page.fill("input[name=user]", "admin")
    page.fill("input[name=password]", os.environ["GRAFANA_PASSWORD"])
    page.click("button[type=submit]")
    page.wait_for_url(lambda u: "/login" not in u)
    page.goto(f"{base}/d/netops?from=now-6m&to=now", wait_until="networkidle")
    try:
        page.wait_for_selector("text=BGP down", timeout=30000)
    except Exception:
        print(page.inner_text("body")[:800], file=sys.stderr)
        raise
    page.wait_for_timeout(5000)
    page.screenshot(path=out, full_page=True)

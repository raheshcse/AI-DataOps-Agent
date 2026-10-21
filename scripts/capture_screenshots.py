"""Capture READ-ONLY screenshots of the running dashboards (optional helper; needs Playwright).

    pip install playwright
    python -m playwright install chromium
    python -m scripts.capture_screenshots --out docs/screenshots/captured

It only opens pages and clicks navigation controls. It never presses Import, Investigate, Request or Submit, never types a
token, and never sends anything to the API itself, so it cannot change any data. Start the stack first (``python
scripts/dev_stack.py``) and make sure the screenshots show nothing you would not want to publish.

Environment: OPS_DASHBOARD_URL (default http://127.0.0.1:8501), LEDGER_EXPLORER_URL (default http://127.0.0.1:8502).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def settle(page, seconds: float = 1.2) -> None:
    page.wait_for_timeout(int(seconds * 1000))
    for _ in range(60):  # wait until Streamlit shows no running indicator
        if page.locator('[data-testid="stStatusWidget"]').count() == 0:
            break
        page.wait_for_timeout(250)
    page.wait_for_timeout(500)


def goto_page(page, label: str) -> None:
    page.locator('[data-testid="stSidebar"]').get_by_text(label, exact=True).first.click()
    settle(page, 1.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="docs/screenshots/captured", help="folder for the PNG files (created if missing)")
    args = parser.parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run: pip install playwright   then   python -m playwright install chromium")
        return 2
    ops = os.environ.get("OPS_DASHBOARD_URL", "http://127.0.0.1:8501")
    ledger = os.environ.get("LEDGER_EXPLORER_URL", "http://127.0.0.1:8502")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            page.goto(ops)
            page.wait_for_selector('[data-testid="stSidebar"]', timeout=30000)
            settle(page, 2)
            goto_page(page, "Incidents")
            page.get_by_text("SQL Server simulator", exact=True).first.click()
            settle(page, 2)
            for name, label in (("explorer-simulator-incidents.png", None), ("human-review.png", "Human Review"),
                                ("recovery-and-approvals.png", "Recovery & approvals")):
                if label:
                    goto_page(page, label)
                page.screenshot(path=str(out / name))
                saved.append(name)
            page.close()
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            page.goto(ledger)
            page.wait_for_selector('[data-testid="stSidebar"]', timeout=30000)
            settle(page, 2)
            page.screenshot(path=str(out / "verbaledger-summary.png"))
            saved.append("verbaledger-summary.png")
            goto_page(page, "Events")
            page.screenshot(path=str(out / "verbaledger-events.png"))
            saved.append("verbaledger-events.png")
        finally:
            browser.close()
    print(f"Saved {len(saved)} screenshot(s) to {out}: {', '.join(saved)}")
    print("Check them before publishing: they show whatever data is in your environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

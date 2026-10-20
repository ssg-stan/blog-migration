"""Optional browser smoke test against the isolated demo server on port 8766."""
from pathlib import Path
import os
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)
BASE = os.getenv("BROWSER_TEST_URL", "http://127.0.0.1:8766")

with sync_playwright() as p:
    chrome = os.getenv("CHROME_PATH")
    if not chrome:
        candidates = [Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                      Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe")]
        chrome = next((str(candidate) for candidate in candidates if candidate.exists()), None)
    browser = p.chromium.launch(executable_path=chrome, headless=True) if chrome else p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.goto(BASE, wait_until="networkidle")
    assert page.get_by_text("Demo workspace", exact=True).count() >= 1
    assert page.locator(".posts-table tbody tr").count() == 5
    assert all(button.is_disabled() for button in page.locator(".sync-button").all())
    page.screenshot(path=str(ARTIFACTS / "dashboard-desktop.png"), full_page=True)
    page.goto(BASE + "/posts", wait_until="networkidle")
    assert page.locator(".posts-table tbody tr").count() == 6
    page.screenshot(path=str(ARTIFACTS / "posts-desktop.png"), full_page=True)
    page.get_by_role("searchbox").fill("museum")
    page.get_by_role("searchbox").press("Enter")
    assert page.locator(".posts-table tbody tr").count() == 1
    page.get_by_role("link", name="Clear", exact=True).click()
    page.get_by_label("Filter by source status").select_option("DRAFT")
    page.get_by_role("button", name="Filter", exact=True).click()
    assert page.locator(".posts-table tbody tr").count() == 2
    page.goto(BASE)
    page.get_by_role("link", name="Preview", exact=False).first.click()
    page.locator("#article-preview").wait_for()
    frame = page.frame_locator("#article-preview")
    assert frame.get_by_role("heading", name="Start with what matters").is_visible()
    page.screenshot(path=str(ARTIFACTS / "preview-desktop.png"), full_page=True)
    page.get_by_role("tab", name="Source HTML", exact=False).click()
    expect(page.locator("#source-panel")).to_be_visible()
    expect(page.locator("#preview-panel")).to_be_hidden()
    page.screenshot(path=str(ARTIFACTS / "source-desktop.png"), full_page=True)
    page.get_by_role("tab", name="Source HTML", exact=False).press("ArrowRight")
    expect(page.locator("#preview-panel")).to_be_visible()
    page.get_by_role("button", name="Mobile", exact=True).click()
    # Width is animated; wait for its final measured size, not a fixed delay.
    expect(page.locator("#article-preview")).to_have_css("width", "375px")
    assert page.get_by_role("button", name="Mobile", exact=True).get_attribute("aria-pressed") == "true"
    page.screenshot(path=str(ARTIFACTS / "preview-mobile-width.png"), full_page=True)
    page.goto(BASE + "/field-mapping", wait_until="networkidle")
    assert page.locator(".mapping-table tbody tr").count() == 11
    assert page.get_by_text("HubSpot Blog API", exact=True).is_visible()
    page.screenshot(path=str(ARTIFACTS / "mapping-desktop.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(BASE)
    assert page.locator("html").evaluate("e => e.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(ARTIFACTS / "dashboard-mobile.png"), full_page=True)
    page.get_by_role("link", name="Preview", exact=False).first.click()
    assert page.locator("html").evaluate("e => e.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(ARTIFACTS / "review-mobile.png"), full_page=True)
    page.goto(BASE + "/posts", wait_until="networkidle")
    assert page.locator("html").evaluate("e => e.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(ARTIFACTS / "posts-mobile.png"), full_page=True)
    page.goto(BASE + "/field-mapping", wait_until="networkidle")
    assert page.locator("html").evaluate("e => e.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(ARTIFACTS / "mapping-mobile.png"), full_page=True)
    assert not errors, errors
    browser.close()
print("Browser smoke checks passed: all four views, desktop/mobile layouts, search, filters, preview/source tabs, and font/CSP checks.")

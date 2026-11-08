"""Renders social-preview.html to ../social-preview.png (2560x1280, GitHub's 1280x640 at 2x).

    uv run --no-project --with playwright python docs/images/source/render.py

Uses Brave; for another Chromium browser change BROWSER, or remove executable_path and
run `uvx playwright install chromium` once.
"""
import pathlib

from playwright.sync_api import sync_playwright

BROWSER = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
here = pathlib.Path(__file__).resolve().parent

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=BROWSER, headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 640}, device_scale_factor=2)
    page.goto((here / "social-preview.html").as_uri())
    page.wait_for_timeout(500)  # let the images load
    page.screenshot(path=here.parent / "social-preview.png")
    browser.close()

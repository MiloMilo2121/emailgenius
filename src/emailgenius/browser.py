from __future__ import annotations

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .types import BrowserSnapshot


def _clean_text(raw: str) -> str:
    normalized = " ".join(raw.split())
    return normalized.strip()


async def fetch_website_snapshot(
    url: str,
    *,
    timeout_ms: int = 45000,
    headless: bool = True,
) -> BrowserSnapshot:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                # Network-idle is best-effort and often noisy on analytics-heavy pages.
                pass

            title = await page.title()
            body_text = await page.locator("body").inner_text(timeout=7000)
            raw_links = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.href).filter(Boolean)",
            )
        finally:
            await context.close()
            await browser.close()

    cleaned_text = _clean_text(body_text)
    excerpt = cleaned_text[:1500]
    links = list(dict.fromkeys(raw_links))[:120]

    return BrowserSnapshot(
        url=url,
        title=title.strip(),
        text_excerpt=excerpt,
        full_text=cleaned_text,
        links=links,
    )

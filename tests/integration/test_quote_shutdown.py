"""A completed Dell quote must not leave a task waiting for browser shutdown."""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from pricewatch.fetching.dell_options import settled_offer


@pytest.mark.anyio
async def test_same_price_quote_leaves_no_task_when_real_browser_closes():
    html = (Path(__file__).parents[1] / "fixtures" / "dell" / "options.html").read_text()
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as error:
            if "error while loading shared libraries" in str(error):
                pytest.skip("Local Chromium system libraries are unavailable")
            raise
        leftover = set()
        try:
            page = await browser.new_page()
            await page.route(
                "http://quote.test/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                ),
            )
            response = await page.goto("http://quote.test/")
            assert response is not None
            initial_tasks = asyncio.all_tasks()
            quote = await settled_offer(
                page,
                {"Graphics Card": "NVIDIA® GeForce RTX™ 5070 8 GB GDDR7"},
                baseline_price=399999,
                changed=True,
                quote_responses=[response],
            )
            assert quote.price_minor == 399999
            leftover = asyncio.all_tasks() - initial_tasks
            assert not leftover, "Completed quote must not leave a browser-close watcher"
        finally:
            for task in leftover:
                task.cancel()
            await asyncio.gather(*leftover, return_exceptions=True)
            await browser.close()

# traffic-service/src/traffic_service/browser.py
"""The only module that drives Playwright.

Reuses the scraper's primitives — the maps URL, the tile-activity counter, the
traffic-layer readiness gate and its timeouts — so the capture rules have one
definition, and `google_maps_scraper.py` needs no change for this service to
exist.
"""
from __future__ import annotations

import logging
import pathlib
import random
import time

from traffic_service import pipeline

_scraper = pipeline.load("google_maps_scraper")

CaptureAborted = _scraper.CaptureAborted


def zone_url(zone: dict) -> str:
    return _scraper.build_maps_url(
        _scraper.Location(city=zone["city"], lat=float(zone["lat"]),
                          lng=float(zone["lng"]), scale=float(zone["scale"]))
    )


def capturer(zone: dict, dest: pathlib.Path) -> None:
    """Capture one zone's traffic map to `dest`. Raises CaptureAborted/OSError."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = zone_url(zone)
    user_agent = random.choice(_scraper.USER_AGENTS)
    started = time.monotonic()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            # the container's /dev/shm is 64 MB and silently kills tabs without this
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=1,
                locale="en-US",
                ignore_https_errors=True,
            )
            context.set_default_timeout(_scraper.CAPTURE_OP_TIMEOUT_MS)
            page = context.new_page()
            activity = _scraper.TileActivity()
            page.on("response", activity.on_response)
            try:
                page.goto(url, wait_until="domcontentloaded",
                          timeout=_scraper.GOTO_TIMEOUT_MS)
                ready = _scraper.wait_for_traffic_layer(page, activity)
                if ready["traffic_required"] and ready["traffic_tiles"] < ready["traffic_required"]:
                    raise CaptureAborted(
                        f"traffic layer not confirmed after {ready['ready_ms']} ms "
                        f"(tiles={ready['tiles']}, traffic_tiles={ready['traffic_tiles']}, "
                        f"need={ready['traffic_required']})"
                    )
                page.wait_for_timeout(_scraper.SETTLE_MS)
                page.screenshot(path=str(dest), full_page=True,
                                timeout=_scraper.SCREENSHOT_TIMEOUT_MS)
                logging.info("captured %s to %s in %.1f s (tiles=%d traffic_tiles=%d)",
                             zone["zone_id"], dest, time.monotonic() - started,
                             ready["tiles"], ready["traffic_tiles"])
            except PlaywrightTimeoutError as error:
                raise CaptureAborted(f"navigation failed for {zone['zone_id']}: {error}") from error
            finally:
                page.close()
                context.close()
        finally:
            browser.close()

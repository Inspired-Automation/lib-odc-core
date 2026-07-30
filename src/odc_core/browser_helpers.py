"""Shared browser-automation helpers: human-like pacing, error screenshots,
cookie-banner dismissal. Supplier-agnostic - takes page/config explicitly
rather than living on a base class, since each supplier is its own project.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def human_delay(config: dict) -> None:
    """Sleep for a random duration to simulate human behaviour between portal actions."""
    lo = config.get("delays", {}).get("action_min_s", 1.0)
    hi = config.get("delays", {}).get("action_max_s", 3.0)
    duration = random.uniform(lo, hi)
    logger.debug("BROWSER_HELPERS - human_delay sleeping %.2fs", duration)
    time.sleep(duration)


def move_mouse_toward(page, locator) -> None:
    """Move the mouse toward `locator` in a few jittered waypoints (no click).

    A click that fires with no prior mouse movement is itself a bot signal
    (real pointers arrive from wherever they last were, not teleport onto the
    target). Safe to call before a click that needs special handling (e.g.
    `force=True` inside a cross-origin iframe) where `human_click` doesn't fit.
    """
    target = page.locator(locator).first if isinstance(locator, str) else locator
    try:
        target.scroll_into_view_if_needed(timeout=3_000)
    except Exception:
        logger.debug("BROWSER_HELPERS - scroll_into_view_if_needed failed", exc_info=True)

    try:
        box = target.bounding_box()
    except Exception:
        logger.debug("BROWSER_HELPERS - bounding_box unavailable", exc_info=True)
        box = None
    if not box:
        return

    end_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    end_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    try:
        start_x = end_x + random.uniform(-120, 120)
        start_y = end_y + random.uniform(-80, 80)
        steps = random.randint(2, 4)
        for i in range(1, steps + 1):
            frac = i / steps
            jitter_x = random.uniform(-4, 4)
            jitter_y = random.uniform(-4, 4)
            page.mouse.move(
                start_x + (end_x - start_x) * frac + jitter_x,
                start_y + (end_y - start_y) * frac + jitter_y,
            )
            time.sleep(random.uniform(0.03, 0.09))
    except Exception:
        logger.debug("BROWSER_HELPERS - mouse move path interrupted", exc_info=True)
    time.sleep(random.uniform(0.15, 0.4))


def human_click(page, locator) -> None:
    """Move the mouse toward `locator` like a real user, pause, then click.

    `locator` may be a Locator or a CSS/text selector string.
    """
    target = page.locator(locator).first if isinstance(locator, str) else locator
    move_mouse_toward(page, target)
    target.click(timeout=10_000)


def human_type(page, selector: str, text: str, config: dict) -> None:
    """
    Type text into a field character-by-character with random keystroke delays
    to simulate human typing speed. Uses page.type() rather than page.fill()
    so each keystroke fires individually.
    """
    lo = config.get("delays", {}).get("keystroke_min_ms", 50)
    hi = config.get("delays", {}).get("keystroke_max_ms", 150)
    delay_ms = random.randint(lo, hi)
    page.type(selector, text, delay=delay_ms)


def take_error_screenshot(page, label: str, config: dict) -> None:
    """
    Capture a full-page screenshot on a portal error and save it to the
    supplier's logs directory. Failure to screenshot is swallowed so it
    never interrupts the main process.
    """
    try:
        logs_dir = Path(config.get("_logs_dir", "."))
        logs_dir.mkdir(parents=True, exist_ok=True)
        process_id = config.get("_process_id", "unknown")
        supplier = config.get("_supplier_name", "unknown").upper()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = logs_dir / f"{process_id}_{supplier}_{label}_{ts}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.debug("BROWSER_HELPERS - error screenshot saved: %s", path)
    except Exception:
        logger.debug("BROWSER_HELPERS - could not capture error screenshot", exc_info=True)


def dismiss_cookie_banner(page) -> bool:
    """Click a cookie/consent banner if visible. Returns True when dismissed."""
    for selector in (
        "#hs-eu-confirmation-button",
        "button:has-text('Dismiss')",
        "button:has-text('Accept')",
        "button:has-text('Allow All')",
        "a:has-text('Dismiss')",
    ):
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=500):
                btn.click()
                logger.debug("BROWSER_HELPERS - cookie banner dismissed via %s", selector)
                return True
        except Exception:
            continue
    return False


def dismiss_cookie_banners(page) -> None:
    """Dismiss cookie banners on the active page and any other open browser tabs."""
    seen: set[int] = set()
    for target in (page, *page.context.pages):
        if target.is_closed() or id(target) in seen:
            continue
        seen.add(id(target))
        dismiss_cookie_banner(target)

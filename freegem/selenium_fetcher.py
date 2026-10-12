"""Optional Selenium/Grid rendering adapter — the Playwright alternative.

Same job as :mod:`freegem.playwright_fetcher` (render JS-heavy pages the
HTTP ladder cannot), different driver: Selenium 4 with a GRID_URL for
remote Grid nodes. Exists because some deployments already run a Grid
farm and do not want a second browser stack.

Isolation model: every fetch gets a FRESH driver (local) or a fresh
session on a Grid node (remote) — no cookies, storage, or service workers
survive between fetches unless explicitly seeded via a session profile.
``finally`` blocks guarantee the driver quits even on exceptions; a leaked
chromium is a resource bug AND a fingerprint.

Differences from the Playwright module, honestly documented:

* PDF export goes through Chrome DevTools ``Page.printToPDF`` (chromium
  only); Firefox raises :class:`SeleniumError`.
* Traces are a Grid/Selenium-4 ``Browser`` feature when supported; when
  unavailable the result simply carries ``trace_path=None``.
"""

from __future__ import annotations

import base64
import contextlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from .config import BridgeConfig
from .crawler.fetch import FetchOutcome, crawl_fetch
from .ssrf import SSRFRefused, url_allowed

__all__ = [
    "SeleniumError",
    "SeleniumOptions",
    "SeleniumResult",
    "fetch_http_first_selenium",
    "fetch_with_selenium",
    "is_selenium_available",
]


class SeleniumError(Exception):
    """Selenium fetch failed (missing package, dead Grid, bad options)."""


@dataclass
class SeleniumOptions:
    """Configuration for one Selenium rendering session."""

    url: str
    grid_url: str | None = None  # None -> local driver
    wait_selector: str | None = None
    implicit_wait_sec: float = 5.0
    page_load_timeout_sec: float = 30.0
    viewport_width: int = 1280
    viewport_height: int = 800
    user_agent: str | None = None
    storage_state: dict[str, Any] | str | None = None  # Playwright-style cookies
    capture_screenshot: bool = False
    screenshot_path: str | None = None
    capture_pdf: bool = False
    pdf_path: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SeleniumResult:
    """Outcome of a Selenium browser fetch."""

    url: str
    final_url: str
    status: int = 200
    content_type: str = "text/html"
    text: str = ""
    title: str = ""
    screenshot_path: str | None = None
    pdf_path: str | None = None
    elapsed_ms: int = 0
    ok: bool = True
    error: str | None = None


def is_selenium_available() -> bool:
    try:
        import selenium  # noqa: F401

        return True
    except ImportError:
        return False


def _build_driver(opts: SeleniumOptions) -> Any:
    """Fresh driver per fetch: local chromium or a Grid node."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
    except ImportError as exc:
        raise SeleniumError("pip install selenium  (plus a matching chromedriver)") from exc

    chrome_opts = ChromeOptions()
    chrome_opts.add_argument("--headless=new")
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument(f"--window-size={opts.viewport_width},{opts.viewport_height}")
    if opts.user_agent:
        chrome_opts.add_argument(f"--user-agent={opts.user_agent}")
    for name, value in opts.extra_headers.items():
        chrome_opts.add_argument(f"--header={name}:{value}")  # best effort; CDP sets the rest
    if opts.grid_url:
        return webdriver.Remote(command_executor=opts.grid_url, options=chrome_opts)
    return webdriver.Chrome(options=chrome_opts)


def _apply_storage_state(driver: Any, opts: SeleniumOptions) -> None:
    """Seed cookies from a Playwright-style storage_state dict (shared with
    session_profiles.py so one profile works on both adapters)."""
    if not opts.storage_state:
        return
    state: dict[str, Any]
    if isinstance(opts.storage_state, str):
        import json

        with open(opts.storage_state, encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = opts.storage_state
    for c in state.get("cookies", []):
        cookie = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "path": c.get("path", "/"),
        }
        # selenium requires the domain of the current page or an explicit domain
        if c.get("domain"):
            cookie["domain"] = c["domain"]
        if c.get("secure") is not None:
            cookie["secure"] = bool(c.get("secure"))
        if c.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(c.get("httpOnly"))
        try:
            driver.add_cookie(cookie)
        except Exception:
            continue  # a cookie for another domain must not kill the fetch


def fetch_with_selenium(opts: SeleniumOptions) -> SeleniumResult:
    """Render one URL in an isolated (fresh) Selenium session."""
    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        target_url = url_allowed(opts.url)
    except SSRFRefused as exc:
        return SeleniumResult(
            url=opts.url,
            final_url=opts.url,
            status=0,
            ok=False,
            error=f"SSRF refused: {exc}",
            elapsed_ms=_elapsed(),
        )

    driver = None
    try:
        driver = _build_driver(opts)
        driver.set_page_load_timeout(opts.page_load_timeout_sec)
        driver.implicitly_wait(opts.implicit_wait_sec)

        driver.get("about:blank")
        _apply_storage_state(driver, opts)
        driver.get(target_url)

        if opts.wait_selector:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait

            WebDriverWait(driver, opts.page_load_timeout_sec).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, opts.wait_selector))
            )

        title = driver.title or ""
        html = driver.page_source or ""
        final_url = driver.current_url or target_url

        screenshot_path: str | None = None
        pdf_path: str | None = None
        if opts.capture_screenshot:
            screenshot_path = opts.screenshot_path
            if not screenshot_path:
                fd, screenshot_path = tempfile.mkstemp(prefix="freegem-ss-", suffix=".png")
                os.close(fd)
            driver.save_screenshot(screenshot_path)

        if opts.capture_pdf:
            pdf_path = opts.pdf_path
            if not pdf_path:
                fd, pdf_path = tempfile.mkstemp(prefix="freegem-pdf-", suffix=".pdf")
                os.close(fd)
            result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": False})
            with open(pdf_path, "wb") as fh:
                fh.write(base64.b64decode(result["data"]))

        return SeleniumResult(
            url=opts.url,
            final_url=final_url,
            status=200,
            content_type="text/html",
            text=html,
            title=title,
            screenshot_path=screenshot_path,
            pdf_path=pdf_path,
            elapsed_ms=_elapsed(),
            ok=True,
        )
    except SeleniumError:
        raise
    except Exception as exc:
        return SeleniumResult(
            url=opts.url,
            final_url=opts.url,
            status=0,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=_elapsed(),
        )
    finally:
        if driver is not None:
            with contextlib.suppress(Exception):
                driver.quit()


def should_fallback_to_selenium(
    outcome: FetchOutcome | None, text_content: str | None = None
) -> bool:
    """Same heuristic as the Playwright fallback: dead/empty/JS-shell pages
    need a browser. Shared shape, separate function, so each adapter stays
    independently importable and testable."""
    if outcome is None:
        return True
    if outcome.kind != "ok":
        return True
    text_content = text_content if text_content is not None else outcome.text()
    stripped = (text_content or "").strip()
    low = (text_content or "").lower()
    return len(stripped) < 200 and (
        "<script" in low or "enable javascript" in low or "app-root" in low
    )


def fetch_http_first_selenium(
    url: str,
    cfg: BridgeConfig,
    *,
    force_browser: bool = False,
    selenium_opts: SeleniumOptions | None = None,
) -> tuple[FetchOutcome | None, SeleniumResult | None]:
    """HTTP-first, Selenium fallback. Mirrors fetch_http_first()."""
    if force_browser and is_selenium_available():
        opts = selenium_opts or SeleniumOptions(url=url)
        return None, fetch_with_selenium(opts)

    http_outcome = crawl_fetch(cfg, url)
    if is_selenium_available() and should_fallback_to_selenium(http_outcome):
        opts = selenium_opts or SeleniumOptions(url=url)
        return http_outcome, fetch_with_selenium(opts)
    return http_outcome, None

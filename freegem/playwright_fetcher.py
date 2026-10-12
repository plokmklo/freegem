"""Optional Playwright HTTP-first / browser-fallback rendering.

Provides browser-assisted rendering for JS-heavy web pages, with isolated
browser contexts, flexible waits, screenshots, PDFs, traces, and reliable
resource cleanup.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from .config import BridgeConfig
from .crawler.fetch import FetchOutcome, crawl_fetch
from .ssrf import SSRFRefused, url_allowed


@dataclass
class PlaywrightOptions:
    """Configuration options for a Playwright rendering session."""

    url: str
    storage_state: dict[str, Any] | str | None = None
    wait_selector: str | None = None
    wait_until: str = "networkidle"  # "domcontentloaded", "load", "networkidle"
    wait_timeout_ms: int = 30000
    viewport_width: int = 1280
    viewport_height: int = 800
    user_agent: str | None = None
    capture_screenshot: bool = False
    screenshot_path: str | None = None
    screenshot_full_page: bool = True
    capture_pdf: bool = False
    pdf_path: str | None = None
    capture_trace: bool = False
    trace_path: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class PlaywrightResult:
    """Outcome of a Playwright browser fetch."""

    url: str
    final_url: str
    status: int
    content_type: str = "text/html"
    text: str = ""
    title: str = ""
    screenshot_path: str | None = None
    pdf_path: str | None = None
    trace_path: str | None = None
    elapsed_ms: int = 0
    ok: bool = True
    error: str | None = None


def is_playwright_available() -> bool:
    """Check if Playwright is installed in the current environment."""
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


def fetch_with_playwright(opts: PlaywrightOptions) -> PlaywrightResult:
    """Render a page using a headless Playwright browser context.

    Raises RuntimeError if Playwright is not installed.
    """
    if not is_playwright_available():
        raise RuntimeError(
            "Playwright package is not installed. Install with `pip install playwright`."
        )

    started = time.monotonic()

    # SSRF verification on input URL before browser launch
    try:
        target_url = url_allowed(opts.url)
    except SSRFRefused as exc:
        return PlaywrightResult(
            url=opts.url,
            final_url=opts.url,
            status=0,
            ok=False,
            error=f"SSRF refused: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright package is not installed. Install with `pip install playwright`."
        ) from exc

    screenshot_file: str | None = opts.screenshot_path
    pdf_file: str | None = opts.pdf_path
    trace_file: str | None = opts.trace_path

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context_args: dict[str, Any] = {
                "viewport": {"width": opts.viewport_width, "height": opts.viewport_height},
            }
            if opts.user_agent:
                context_args["user_agent"] = opts.user_agent
            if opts.storage_state:
                context_args["storage_state"] = opts.storage_state
            if opts.extra_headers:
                context_args["extra_http_headers"] = opts.extra_headers

            context = browser.new_context(**context_args)
            try:
                if opts.capture_trace and trace_file:
                    context.tracing.start(screenshots=True, snapshots=True, sources=True)

                page = context.new_page()
                response = page.goto(
                    target_url, wait_until=opts.wait_until, timeout=opts.wait_timeout_ms
                )

                status = response.status if response else 200
                final_url = page.url

                if opts.wait_selector:
                    page.wait_for_selector(opts.wait_selector, timeout=opts.wait_timeout_ms)

                title = page.title()
                html_content = page.content()

                if opts.capture_screenshot:
                    if not screenshot_file:
                        fd, screenshot_file = tempfile.mkstemp(prefix="freegem-ss-", suffix=".png")
                        os.close(fd)
                    page.screenshot(path=screenshot_file, full_page=opts.screenshot_full_page)

                if opts.capture_pdf:
                    if not pdf_file:
                        fd, pdf_file = tempfile.mkstemp(prefix="freegem-pdf-", suffix=".pdf")
                        os.close(fd)
                    page.pdf(path=pdf_file)

                if opts.capture_trace and trace_file:
                    context.tracing.stop(path=trace_file)

                elapsed = int((time.monotonic() - started) * 1000)
                return PlaywrightResult(
                    url=opts.url,
                    final_url=final_url,
                    status=status,
                    content_type="text/html",
                    text=html_content,
                    title=title,
                    screenshot_path=screenshot_file if opts.capture_screenshot else None,
                    pdf_path=pdf_file if opts.capture_pdf else None,
                    trace_path=trace_file if opts.capture_trace else None,
                    elapsed_ms=elapsed,
                    ok=status < 400,
                )
            finally:
                context.close()
        finally:
            browser.close()


def should_fallback_to_browser(
    outcome: FetchOutcome | None, text_content: str | None = None
) -> bool:
    """Determine if an HTTP fetch outcome requires Playwright browser fallback."""
    if outcome is None:
        return True
    if outcome.kind != "ok":
        return True
    if not text_content:
        text_content = outcome.text()
    # Check for client-side empty shell or typical SPA JS placeholder
    stripped = text_content.strip()
    return len(stripped) < 200 and (
        "<script" in text_content.lower()
        or "enable javascript" in text_content.lower()
        or "app-root" in text_content.lower()
    )


def fetch_http_first(
    url: str,
    cfg: BridgeConfig,
    *,
    force_browser: bool = False,
    playwright_opts: PlaywrightOptions | None = None,
) -> tuple[FetchOutcome | None, PlaywrightResult | None]:
    """Fetch URL HTTP-first, falling back to Playwright if needed or forced.

    Returns (http_outcome, playwright_result). One or both will be populated.
    """
    if force_browser and is_playwright_available():
        opts = playwright_opts or PlaywrightOptions(url=url)
        res = fetch_with_playwright(opts)
        return None, res

    # 1. Standard guarded HTTP fetch
    http_outcome = crawl_fetch(cfg, url)

    # 2. Check if browser fallback is needed
    if is_playwright_available() and should_fallback_to_browser(http_outcome):
        opts = playwright_opts or PlaywrightOptions(url=url)
        res = fetch_with_playwright(opts)
        return http_outcome, res

    return http_outcome, None

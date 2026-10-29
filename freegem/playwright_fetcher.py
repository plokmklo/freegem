"""Optional Playwright HTTP-first / browser-fallback rendering.

Provides browser-assisted rendering for JS-heavy web pages, with isolated
browser contexts, flexible waits, screenshots, PDFs, traces, and reliable
resource cleanup.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from .browser_guard import browser_request_allowed, install_playwright_route_guard
from .config import BridgeConfig
from .crawler.fetch import FetchOutcome, crawl_fetch
from .ssrf import SSRFRefused


@dataclass
class PlaywrightOptions:
    """Configuration options for a Playwright rendering session."""

    url: str
    storage_state: dict[str, Any] | str | None = None
    wait_selector: str | None = None
    wait_until: str = "networkidle"  # "domcontentloaded", "load", "networkidle"
    wait_timeout_ms: int = 30000
    #: (#61) Extra settle beat AFTER navigation/wait_selector, before
    #: extraction: SPA frameworks hydrate once the DOM event fires, and a
    #: domcontentloaded-based fetch needs one breath of JS time before
    #: page.content() sees the rendered text. 0 keeps the old behavior.
    settle_ms: int = 0
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
    started = time.monotonic()

    # SSRF verification on the input URL BEFORE any availability check or
    # browser launch: the strict policy (public-only resolution, metadata/
    # special names, non-http(s) schemes). A refused URL must be refused
    # regardless of which browser stack is installed.
    try:
        target_url = browser_request_allowed(opts.url)
    except SSRFRefused as exc:
        return PlaywrightResult(
            url=opts.url,
            final_url=opts.url,
            status=0,
            ok=False,
            error=f"SSRF refused: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    if not is_playwright_available():
        raise RuntimeError(
            "Playwright package is not installed. Install with `pip install playwright`."
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
            # Per-request guard: every request this context makes -- initial
            # navigation, redirect hops, subresources -- must pass the same
            # public-only policy. Installed before the first navigation.
            guard = install_playwright_route_guard(context)
            try:
                if opts.capture_trace and trace_file:
                    context.tracing.start(screenshots=True, snapshots=True, sources=True)

                page = context.new_page()
                try:
                    response = page.goto(
                        target_url, wait_until=opts.wait_until, timeout=opts.wait_timeout_ms
                    )
                except Exception:
                    # A blocked main-frame navigation surfaces as a generic
                    # playwright error; translate it when OUR guard blocked it.
                    if guard.blocked_navigations:
                        return PlaywrightResult(
                            url=opts.url,
                            final_url=opts.url,
                            status=0,
                            ok=False,
                            error=f"SSRF refused: {guard.blocked_navigations[-1]}",
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                    raise

                status = response.status if response else 200
                final_url = page.url

                # Belt: re-validate the URL we actually landed on -- a redirect
                # that ended on an internal host must not hand back content.
                # (The route guard normally aborts that hop mid-flight; this
                # catches any API-shape edge that slips past it.)
                try:
                    browser_request_allowed(final_url)
                except SSRFRefused as exc:
                    return PlaywrightResult(
                        url=opts.url,
                        final_url=final_url,
                        status=status,
                        ok=False,
                        error=f"SSRF refused (final URL): {exc}",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                if guard.blocked_navigations:
                    return PlaywrightResult(
                        url=opts.url,
                        final_url=final_url,
                        status=status,
                        ok=False,
                        error=f"SSRF refused: {guard.blocked_navigations[-1]}",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )

                if opts.wait_selector:
                    page.wait_for_selector(opts.wait_selector, timeout=opts.wait_timeout_ms)
                if opts.settle_ms > 0:
                    # (#61) The hydration beat: give the SPA's JS one breath
                    # before the DOM is snapshot. Deliberately INSIDE the
                    # try: a wedged page here still unwinds through the
                    # context/browser close below.
                    page.wait_for_timeout(opts.settle_ms)

                title = page.title()
                html_content = page.content()

                # Captures are BEST-EFFORT: the page is already rendered, so a
                # screenshot/pdf/trace failure must not discard the content
                # (nor leak the placeholder temp file just created).
                if opts.capture_screenshot:
                    if not screenshot_file:
                        fd, screenshot_file = tempfile.mkstemp(prefix="freegem-ss-", suffix=".png")
                        os.close(fd)
                    try:
                        page.screenshot(path=screenshot_file, full_page=opts.screenshot_full_page)
                    except Exception:
                        if screenshot_file != opts.screenshot_path:
                            with contextlib.suppress(OSError):
                                os.unlink(screenshot_file)
                        screenshot_file = None

                if opts.capture_pdf:
                    if not pdf_file:
                        fd, pdf_file = tempfile.mkstemp(prefix="freegem-pdf-", suffix=".pdf")
                        os.close(fd)
                    try:
                        page.pdf(path=pdf_file)
                    except Exception:
                        if pdf_file != opts.pdf_path:
                            with contextlib.suppress(OSError):
                                os.unlink(pdf_file)
                        pdf_file = None

                if opts.capture_trace and trace_file:
                    try:
                        context.tracing.stop(path=trace_file)
                    except Exception:
                        trace_file = None

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

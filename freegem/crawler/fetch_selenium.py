"""Optional Selenium / Grid fetch adapter for JavaScript rendering and dynamic pages.

Supports both local WebDriver (e.g. Chrome/Firefox) and remote Selenium Grid hub.
Enforces SSRF safety checks before navigating and caps rendering timeouts.
"""

from __future__ import annotations

import contextlib
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ..ssrf import SSRFRefused, pick_public_ips, url_allowed
from .fetch import FetchOutcome

logger = logging.getLogger(__name__)


@dataclass
class SeleniumConfig:
    grid_url: str | None = None
    browser_name: str = "chrome"
    headless: bool = True
    page_load_timeout: float = 30.0
    implicit_wait: float = 5.0
    user_agent: str | None = None


class SeleniumFetcher:
    """Fetcher adapter backed by Selenium WebDriver or Selenium Grid hub."""

    def __init__(self, config: SeleniumConfig | None = None) -> None:
        self.config = config or SeleniumConfig()

    def fetch(self, url: str) -> FetchOutcome:
        """Fetch URL using Selenium WebDriver / Grid with SSRF protection."""
        try:
            url_allowed(url)
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname:
                pick_public_ips(parsed.hostname)
        except SSRFRefused as exc:
            return FetchOutcome(
                url=url,
                kind="ssrf",
                error=f"SSRF guard refused: {exc}",
            )

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions
            from selenium.webdriver.firefox.options import Options as FirefoxOptions
        except (ImportError, ModuleNotFoundError):
            return FetchOutcome(
                url=url,
                kind="network",
                error="selenium package is not installed. Install with 'pip install selenium'.",
            )

        driver = None
        try:
            if self.config.browser_name.lower() == "firefox":
                options: Any = FirefoxOptions()
                if self.config.headless:
                    options.add_argument("-headless")
                if self.config.user_agent:
                    options.set_preference("general.useragent.override", self.config.user_agent)
            else:
                options = ChromeOptions()
                if self.config.headless:
                    options.add_argument("--headless=new")
                    options.add_argument("--no-sandbox")
                    options.add_argument("--disable-dev-shm-usage")
                if self.config.user_agent:
                    options.add_argument(f"user-agent={self.config.user_agent}")

            if self.config.grid_url:
                driver = webdriver.Remote(
                    command_executor=self.config.grid_url,
                    options=options,
                )
            else:
                if self.config.browser_name.lower() == "firefox":
                    driver = webdriver.Firefox(options=options)
                else:
                    driver = webdriver.Chrome(options=options)

            driver.set_page_load_timeout(self.config.page_load_timeout)
            driver.implicitly_wait(self.config.implicit_wait)

            driver.get(url)
            body_text = driver.page_source
            current_url = driver.current_url
            body_bytes = body_text.encode("utf-8")

            return FetchOutcome(
                url=url,
                kind="ok",
                status=200,
                final_url=current_url or url,
                content_type="text/html; charset=utf-8",
                body=body_bytes,
                body_size=len(body_bytes),
            )

        except Exception as exc:
            return FetchOutcome(
                url=url,
                kind="network",
                error=f"Selenium fetch error: {exc}",
            )
        finally:
            if driver is not None:
                with contextlib.suppress(Exception):
                    driver.quit()


def fetch_selenium(url: str, config: SeleniumConfig | None = None) -> FetchOutcome:
    """Convenience helper to fetch a single page via Selenium / Grid."""
    fetcher = SeleniumFetcher(config=config)
    return fetcher.fetch(url)

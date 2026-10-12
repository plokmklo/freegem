"""Tests for the Selenium adapter (selenium_fetcher.py).

No browser and no Grid in CI: the real driver paths are exercised through a
fake ``webdriver`` module injected into sys.modules, verifying isolation
(fresh driver per fetch, quit in finally), cookie seeding, waits, CDP PDF,
and the HTTP-first fallback decision.
"""

from __future__ import annotations

import base64
import sys
import types
from typing import Any, ClassVar

import pytest

from freegem.crawler.fetch import FetchOutcome
from freegem.selenium_fetcher import (
    SeleniumError,
    SeleniumOptions,
    fetch_http_first_selenium,
    fetch_with_selenium,
    is_selenium_available,
    should_fallback_to_selenium,
)


class FakeDriver:
    """Records calls; simulates a chromium driver."""

    instances: ClassVar[list[FakeDriver]] = []

    def __init__(self) -> None:
        self.quit_called = False
        self.urls: list[str] = []
        self.cookies: list[dict[str, Any]] = []
        self.page_source = "<html><body>rendered body with plenty of content for the extractor to chew on</body></html>"
        self.title = "Rendered"
        self.current_url = "https://example.org/final"
        self.screenshots: list[str] = []
        self.cdp: list[tuple[str, dict[str, Any]]] = []
        FakeDriver.instances.append(self)

    def set_page_load_timeout(self, v: float) -> None:
        self.page_load_timeout = v

    def implicitly_wait(self, v: float) -> None:
        self.implicit_wait = v

    def get(self, url: str) -> None:
        self.urls.append(url)

    def add_cookie(self, cookie: dict[str, Any]) -> None:
        self.cookies.append(cookie)

    def save_screenshot(self, path: str) -> None:
        self.screenshots.append(path)
        with open(path, "wb") as fh:
            fh.write(b"png")

    def execute_cdp_cmd(self, cmd: str, params: dict[str, Any]) -> dict[str, Any]:
        self.cdp.append((cmd, params))
        return {"data": base64.b64encode(b"%PDF-fake").decode()}

    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture()
def fake_selenium(monkeypatch: pytest.MonkeyPatch) -> type[FakeDriver]:
    """Inject a fake `selenium.webdriver` into sys.modules."""
    FakeDriver.instances = []
    mod = types.ModuleType("selenium")
    wd = types.ModuleType("selenium.webdriver")
    chrome_mod = types.ModuleType("selenium.webdriver.chrome")
    options_mod = types.ModuleType("selenium.webdriver.chrome.options")

    class ChromeOptions:
        def __init__(self) -> None:
            self.args: list[str] = []

        def add_argument(self, arg: str) -> None:
            self.args.append(arg)

    class Remote:
        def __init__(self, command_executor: str, options: Any) -> None:
            self.executor = command_executor
            self._inner = FakeDriver()

            def _quit() -> None:
                self._inner.quit_called = True

            self._inner.quit = _quit  # type: ignore[method-assign]

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    options_mod.ChromeOptions = ChromeOptions  # type: ignore[attr-defined]
    options_mod.Options = ChromeOptions  # type: ignore[attr-defined]  # real import path: ...chrome.options import Options
    chrome_mod.options = options_mod  # type: ignore[attr-defined]
    wd.Chrome = lambda options=None: FakeDriver()  # type: ignore[attr-defined]
    wd.Remote = Remote  # type: ignore[attr-defined]
    chrome_mod.ChromeOptions = ChromeOptions  # type: ignore[attr-defined]

    # minimal waits stack: selenium.webdriver.common.by + support.{ui, expected_conditions}
    common = types.ModuleType("selenium.webdriver.common")
    by_mod = types.ModuleType("selenium.webdriver.common.by")

    class By:
        CSS_SELECTOR = "css selector"

    by_mod.By = By  # type: ignore[attr-defined]
    support = types.ModuleType("selenium.webdriver.support")
    ui_mod = types.ModuleType("selenium.webdriver.support.ui")
    ec_mod = types.ModuleType("selenium.webdriver.support.expected_conditions")

    class WebDriverWait:
        def __init__(self, driver: Any, timeout: float) -> None:
            self._driver = driver
            _ = timeout

        def until(self, method: Any) -> Any:
            return method(self._driver)

    def presence_of_element_located(locator: tuple[str, str]) -> Any:
        return lambda driver: object()  # always "present"

    ui_mod.WebDriverWait = WebDriverWait  # type: ignore[attr-defined]
    ec_mod.presence_of_element_located = presence_of_element_located  # type: ignore[attr-defined]
    support.ui = ui_mod  # type: ignore[attr-defined]
    support.expected_conditions = ec_mod  # type: ignore[attr-defined]
    common.by = by_mod  # type: ignore[attr-defined]
    wd.common = common  # type: ignore[attr-defined]
    wd.support = support  # type: ignore[attr-defined]
    wd.chrome = chrome_mod  # type: ignore[attr-defined]
    mod.webdriver = wd  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "selenium", mod)
    monkeypatch.setitem(sys.modules, "selenium.webdriver", wd)
    monkeypatch.setitem(sys.modules, "selenium.webdriver.common", common)
    monkeypatch.setitem(sys.modules, "selenium.webdriver.common.by", by_mod)
    monkeypatch.setitem(sys.modules, "selenium.webdriver.support", support)
    monkeypatch.setitem(sys.modules, "selenium.webdriver.support.ui", ui_mod)
    monkeypatch.setitem(sys.modules, "selenium.webdriver.support.expected_conditions", ec_mod)
    monkeypatch.setitem(sys.modules, "selenium.webdriver.chrome", chrome_mod)
    monkeypatch.setitem(sys.modules, "selenium.webdriver.chrome.options", options_mod)
    return FakeDriver


def test_is_selenium_available_with_fake_module(fake_selenium: type[FakeDriver]) -> None:
    assert is_selenium_available() is True


def test_fetch_with_selenium_happy_path(fake_selenium: type[FakeDriver]) -> None:
    opts = SeleniumOptions(
        url="https://example.org/page",
        capture_screenshot=True,
        capture_pdf=True,
        wait_selector="#main",
    )
    res = fetch_with_selenium(opts)
    assert res.ok is True
    assert res.title == "Rendered"
    assert res.final_url == "https://example.org/final"
    assert "rendered body" in res.text
    assert res.screenshot_path and res.pdf_path
    assert len(fake_selenium.instances) == 1
    driver = fake_selenium.instances[0]
    assert driver.urls == ["about:blank", "https://example.org/page"]
    assert driver.quit_called is True  # cleanup ran
    assert [c for c, _ in driver.cdp] == ["Page.printToPDF"]


def test_fetch_with_selenium_seeds_cookies_from_storage_state(
    fake_selenium: type[FakeDriver],
) -> None:
    state = {"cookies": [{"name": "sid", "value": "xyz", "domain": ".example.org", "path": "/"}]}
    res = fetch_with_selenium(SeleniumOptions(url="https://example.org/", storage_state=state))
    assert res.ok
    assert fake_selenium.instances[0].cookies[0]["name"] == "sid"
    assert fake_selenium.instances[0].cookies[0]["domain"] == ".example.org"


def test_fetch_with_selenium_bad_cookie_does_not_kill_fetch(
    fake_selenium: type[FakeDriver],
) -> None:
    state = {"cookies": [{"name": "bad", "value": "v"}]}  # no domain: driver may reject
    res = fetch_with_selenium(SeleniumOptions(url="https://example.org/", storage_state=state))
    assert res.ok is True


def test_fetch_with_selenium_quits_on_driver_crash(
    fake_selenium: type[FakeDriver], monkeypatch: pytest.MonkeyPatch
) -> None:
    def exploding_get(self: FakeDriver, url: str) -> None:
        if url.startswith("http"):
            raise RuntimeError("renderer died")

    monkeypatch.setattr(FakeDriver, "get", exploding_get)
    res = fetch_with_selenium(SeleniumOptions(url="https://example.org/"))
    assert res.ok is False
    assert "renderer died" in (res.error or "")
    assert fake_selenium.instances[0].quit_called is True  # finally still ran


def test_fetch_with_selenium_ssrf_refused(fake_selenium: type[FakeDriver]) -> None:
    # url_allowed is the syntactic half of the SSRF guard (scheme, creds,
    # host); a browser does its own DNS so the network half cannot pin IPs.
    res = fetch_with_selenium(SeleniumOptions(url="ftp://example.org/file"))
    assert res.ok is False
    assert "SSRF refused" in (res.error or "")
    assert fake_selenium.instances == []  # never launched a browser


def test_grid_url_uses_remote(fake_selenium: type[FakeDriver]) -> None:
    res = fetch_with_selenium(
        SeleniumOptions(url="https://example.org/", grid_url="http://grid:4444/wd/hub")
    )
    assert res.ok is True


def test_should_fallback_heuristics() -> None:
    ok = FetchOutcome(url="u", kind="ok", status=200, body=b"<html>" + b"x" * 400)
    assert should_fallback_to_selenium(ok) is False
    shell = FetchOutcome(
        url="u", kind="ok", status=200, body=b"<html><script>app-root</script></html>"
    )
    assert should_fallback_to_selenium(shell) is True
    assert should_fallback_to_selenium(None) is True
    err = FetchOutcome(url="u", kind="http_error", status=503, body=b"")
    assert should_fallback_to_selenium(err) is True


def test_fetch_http_first_no_browser_when_http_ok(
    fake_selenium: type[FakeDriver], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = FetchOutcome(url="u", kind="ok", status=200, body=b"<html>" + b"y" * 500 + b"</html>")
    monkeypatch.setattr("freegem.selenium_fetcher.crawl_fetch", lambda url, cfg: good)
    from freegem.config import BridgeConfig

    outcome, sel = fetch_http_first_selenium("https://example.org/", BridgeConfig())
    assert sel is None
    assert outcome is good


def test_selenium_error_without_package(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = sys.modules.pop("selenium", None)
    try:
        assert is_selenium_available() is False
        with pytest.raises(SeleniumError, match="pip install selenium"):
            fetch_with_selenium(SeleniumOptions(url="https://example.org/"))
    finally:
        if saved is not None:
            sys.modules["selenium"] = saved

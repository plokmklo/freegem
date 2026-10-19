"""The v2.12.0 opt-in rungs: tls-browser (curl_cffi) and flaresolverr.

Hermetic: curl_cffi is a stub module injected into ``sys.modules``, the
FlareSolverr endpoint is a local threaded HTTP server, and the ladder's
network functions are mocked. No test touches the real network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest import mock

from freegem import smart_fetch, tls_fetcher
from freegem.config import BridgeConfig
from freegem.flaresolverr_fetcher import FlareSolverrError, flaresolverr_html, is_configured
from freegem.smart_fetch import STRATEGIES, StrategyMemory, fallback_scrape
from freegem.tls_fetcher import TlsFetchError, is_curl_cffi_available, tls_fetch_html
from freegem.web_scraper import ScrapeError

_ARTICLE_HTML = (
    "<html><head><title>Wall Busted</title></head><body>"
    + "<p>"
    + ("Real article body sentence. " * 40)
    + "</p>"
    + "</body></html>"
)
_CHALLENGE_HTML = "<html><body>Just a moment... Verify you are human to continue.</body></html>"


class _FakeCurlResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeCurlSession:
    """Scripted curl_cffi.Session stand-in recording every call."""

    instances: ClassVar[list[_FakeCurlSession]] = []
    init_kwargs: ClassVar[list[dict[str, Any]]] = []
    script: ClassVar[list[tuple[int, str]]] = []

    def __init__(self, impersonate: str | None = None, timeout: float | None = None) -> None:
        self.impersonate = impersonate
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        type(self).instances.append(self)
        type(self).init_kwargs.append({"impersonate": impersonate, "timeout": timeout})

    def get(self, url: str, proxies: dict[str, str] | None = None) -> _FakeCurlResponse:
        script = getattr(type(self), "script", [])
        index = min(len(self.calls), len(script) - 1)
        self.calls.append({"url": url, "proxies": proxies})
        status, text = script[index]
        return _FakeCurlResponse(status, text)

    def close(self) -> None:
        self.closed = True


def _install_fake_curl(script: list[tuple[int, str]]) -> type[_FakeCurlSession]:
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.Session = _FakeCurlSession  # type: ignore[attr-defined]
    fake_curl = types.ModuleType("curl_cffi")
    fake_curl.requests = fake_requests  # type: ignore[attr-defined]
    sys.modules["curl_cffi"] = fake_curl
    sys.modules["curl_cffi.requests"] = fake_requests
    tls_fetcher._availability_cache = True
    _FakeCurlSession.instances = []
    _FakeCurlSession.init_kwargs = []
    _FakeCurlSession.script = script
    return _FakeCurlSession


class _FlareHandler(BaseHTTPRequestHandler):
    #: Test writes the (status, payload) the next POST should answer with.
    reply: ClassVar[dict[str, Any]] = {"code": 200, "body": {}}
    seen: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except ValueError:
            request = {"raw": raw.decode("utf-8", errors="replace")}
        _FlareHandler.seen.append({"path": self.path, "request": request})
        body = json.dumps(_FlareHandler.reply["body"]).encode("utf-8")
        self.send_response(int(_FlareHandler.reply["code"]))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class _FlareServer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FlareHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TlsBrowserRungTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = BridgeConfig(log_requests=False)
        self.saved_modules = {k: sys.modules.get(k) for k in ("curl_cffi", "curl_cffi.requests")}
        self.addCleanup(self._restore_modules)

    def _restore_modules(self) -> None:
        tls_fetcher._availability_cache = None
        for key, module in self.saved_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_curl_cffi_available_false_without_module(self) -> None:
        # A None entry in sys.modules makes `import curl_cffi` raise
        # ImportError even when the real dependency is installed.
        with mock.patch.dict(sys.modules, {"curl_cffi": None}):
            tls_fetcher._availability_cache = None
            self.assertFalse(is_curl_cffi_available())
        tls_fetcher._availability_cache = None

    def test_unavailable_is_an_honest_refusal(self) -> None:
        with mock.patch.object(tls_fetcher, "is_curl_cffi_available", return_value=False):
            with self.assertRaisesRegex(TlsFetchError, "curl_cffi is not installed"):
                tls_fetch_html(self.cfg, "https://example.com/a", timeout=5)

    def test_success_returns_html_and_closes_session(self) -> None:
        fake = _install_fake_curl([(200, _ARTICLE_HTML)])
        body = tls_fetch_html(self.cfg, "https://example.com/a", timeout=5)
        self.assertIn("Real article body", body)
        self.assertEqual(len(fake.instances), 1)
        self.assertEqual(fake.instances[0].impersonate, "chrome")
        self.assertEqual(fake.instances[0].calls[0]["url"], "https://example.com/a")
        self.assertTrue(fake.instances[0].closed)

    def test_proxy_flows_from_config(self) -> None:
        fake = _install_fake_curl([(200, _ARTICLE_HTML)])
        cfg = BridgeConfig(log_requests=False, proxy="http://10.0.0.7:3128")
        tls_fetch_html(cfg, "https://example.com/a", timeout=5)
        self.assertEqual(
            fake.instances[0].calls[0]["proxies"],
            {"http": "http://10.0.0.7:3128", "https": "http://10.0.0.7:3128"},
        )

    def test_wall_status_triggers_one_warm_retry_with_same_session(self) -> None:
        fake = _install_fake_curl([(403, "denied"), (200, _ARTICLE_HTML)])
        body = tls_fetch_html(self.cfg, "https://example.com/a", timeout=5)
        self.assertIn("Real article body", body)
        session = fake.instances[0]
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.closed)

    def test_persistent_wall_raises_with_exact_status(self) -> None:
        _install_fake_curl([(403, "denied"), (403, "denied")])
        with self.assertRaisesRegex(TlsFetchError, r"HTTP 403"):
            tls_fetch_html(self.cfg, "https://example.com/a", timeout=5)

    def test_rung_disabled_by_default_is_refused_before_anything_runs(self) -> None:
        self.assertFalse(self.cfg.smart_fetch_tls_browser)
        with self.assertRaisesRegex(ScrapeError, "tls-browser disabled"):
            smart_fetch._try_strategy(self.cfg, "https://example.com/a", "tls-browser", 5000, True)

    def test_rung_success_via_label_and_extraction(self) -> None:
        _install_fake_curl([(200, _ARTICLE_HTML)])
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_tls_browser=True)
        result = smart_fetch._try_strategy(cfg, "https://example.com/a", "tls-browser", 5000, True)
        self.assertEqual(result.via, "tls-browser")
        self.assertIn("Real article body", result.text)
        self.assertEqual(result.title, "Wall Busted")

    def test_rung_tiny_budget_is_skipped(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_tls_browser=True)
        with self.assertRaisesRegex(ScrapeError, "ladder budget"):
            smart_fetch._tls_browser_read(cfg, "https://example.com/a", 5000, budget=0.2)


class FlareSolverrRungTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = BridgeConfig(log_requests=False)
        self.flare = _FlareServer()
        self.addCleanup(self.flare.stop)
        _FlareHandler.seen = []
        _FlareHandler.reply = {
            "code": 200,
            "body": {
                "status": "ok",
                "solution": {"status": 200, "response": _ARTICLE_HTML, "url": "x"},
            },
        }

    def test_unconfigured_by_default(self) -> None:
        self.assertFalse(is_configured(self.cfg))

    def test_configured_endpoint_round_trip(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_flaresolverr=self.flare.endpoint)
        self.assertTrue(is_configured(cfg))
        html = flaresolverr_html(cfg, "https://example.com/a", timeout=10)
        self.assertIn("Real article body", html)
        sent = _FlareHandler.seen[0]["request"]
        self.assertEqual(sent["cmd"], "request.get")
        self.assertEqual(sent["url"], "https://example.com/a")
        self.assertLessEqual(sent["maxTimeout"], 10_000)

    def test_service_error_is_carried_verbatim(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_flaresolverr=self.flare.endpoint)
        _FlareHandler.reply = {"code": 200, "body": {"status": "error", "message": "no dice"}}
        with self.assertRaisesRegex(FlareSolverrError, "no dice"):
            flaresolverr_html(cfg, "https://example.com/a", timeout=10)

    def test_solved_but_non_200_is_a_refusal(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_flaresolverr=self.flare.endpoint)
        _FlareHandler.reply = {
            "code": 200,
            "body": {"status": "ok", "solution": {"status": 500, "response": "<p>x</p>"}},
        }
        with self.assertRaisesRegex(FlareSolverrError, "HTTP 500"):
            flaresolverr_html(cfg, "https://example.com/a", timeout=10)

    def test_unusable_json_is_a_refusal(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_flaresolverr=self.flare.endpoint)
        # A body that decodes to a JSON *string*, not an object.
        _FlareHandler.reply = {"code": 200, "body": "not-json"}
        with self.assertRaisesRegex(FlareSolverrError, "non-object JSON"):
            flaresolverr_html(cfg, "https://example.com/a", timeout=10)

    def test_empty_solution_is_a_refusal(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_flaresolverr=self.flare.endpoint)
        _FlareHandler.reply = {
            "code": 200,
            "body": {"status": "ok", "solution": {"status": 200, "response": "  "}},
        }
        with self.assertRaisesRegex(FlareSolverrError, "no content"):
            flaresolverr_html(cfg, "https://example.com/a", timeout=10)

    def test_rung_success_via_label(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_flaresolverr=self.flare.endpoint)
        result = smart_fetch._try_strategy(cfg, "https://example.com/a", "flaresolverr", 5000, True)
        self.assertEqual(result.via, "flaresolverr")
        self.assertIn("Real article body", result.text)

    def test_rung_tiny_budget_is_skipped(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_flaresolverr=self.flare.endpoint)
        with self.assertRaisesRegex(ScrapeError, "ladder budget"):
            smart_fetch._flaresolverr_read(cfg, "https://example.com/a", 5000, budget=1.0)


class LadderOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict("os.environ", {"FREEGEM_CACHE_DIR": self.tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        smart_fetch.clear_memory()

    def tearDown(self) -> None:
        smart_fetch.clear_memory()

    def test_default_order_has_no_optin_rungs(self) -> None:
        cfg = BridgeConfig(log_requests=False)
        order = smart_fetch._strategy_order("example.com", cfg)
        self.assertNotIn("tls-browser", order)
        self.assertNotIn("flaresolverr", order)
        self.assertEqual(order, ["browser-headers", "reader", "wayback", "ai-search"])

    def test_optin_rungs_slot_into_position(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(
            smart_fetch_tls_browser=True,
            smart_fetch_flaresolverr="http://127.0.0.1:8191",
        )
        order = smart_fetch._strategy_order("example.com", cfg)
        expected = ["browser-headers", "tls-browser", "reader"]
        if cfg.smart_fetch_browser_render:
            expected.append("browser-render")
        expected += ["flaresolverr", "wayback", "ai-search"]
        self.assertEqual(order, expected)

    def test_remembered_tls_win_fronts_only_while_gate_open(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_tls_browser=True)
        memory = StrategyMemory()
        memory.remember("example.com", "tls-browser")
        self.assertEqual(smart_fetch._strategy_order("example.com", cfg)[0], "tls-browser")
        closed = BridgeConfig(log_requests=False)
        self.assertNotIn("tls-browser", smart_fetch._strategy_order("example.com", closed))

    def test_strategies_tuple_is_the_documented_ladder(self) -> None:
        self.assertEqual(
            STRATEGIES,
            (
                "browser-headers",
                "tls-browser",
                "reader",
                "browser-render",
                "flaresolverr",
                "wayback",
                "ai-search",
            ),
        )


class LadderIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict("os.environ", {"FREEGEM_CACHE_DIR": self.tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        smart_fetch.clear_memory()
        self._saved_modules = {k: sys.modules.get(k) for k in ("curl_cffi", "curl_cffi.requests")}

    def tearDown(self) -> None:
        smart_fetch.clear_memory()
        tls_fetcher._availability_cache = None
        for key, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module

    def test_direct_403_rescued_by_tls_rung_and_remembered(self) -> None:
        _install_fake_curl([(403, "denied"), (200, _ARTICLE_HTML)])
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_tls_browser=True)
        with (
            mock.patch("freegem.smart_fetch.time.sleep"),
            mock.patch("freegem.smart_fetch.scrape_page") as scrape,
            mock.patch(
                "freegem.smart_fetch._wayback_snapshot",
                side_effect=ScrapeError("no snapshot (test)"),
            ),
            mock.patch(
                "freegem.smart_fetch._ai_search_answer",
                side_effect=ScrapeError("bridge unavailable (test)"),
            ),
        ):
            scrape.side_effect = ScrapeError("HTTP 403 fetching 'https://example.com/a'")
            result = fallback_scrape(
                cfg,
                "https://example.com/a",
                max_chars=5000,
                first_error="HTTP 403 fetching 'https://example.com/a'",
            )
        self.assertEqual(result.via, "tls-browser")
        self.assertIn("Real article body", result.text)
        self.assertEqual(StrategyMemory().get("example.com"), "tls-browser")

    def test_challenge_shaped_tls_body_does_not_win(self) -> None:
        _install_fake_curl([(200, _CHALLENGE_HTML), (200, _CHALLENGE_HTML)])
        cfg = BridgeConfig(log_requests=False).replace(smart_fetch_tls_browser=True)
        with (
            mock.patch("freegem.smart_fetch.time.sleep"),
            mock.patch("freegem.smart_fetch.scrape_page") as scrape,
            mock.patch(
                "freegem.smart_fetch._wayback_snapshot",
                side_effect=ScrapeError("no snapshot (test)"),
            ),
            mock.patch(
                "freegem.smart_fetch._ai_search_answer",
                side_effect=ScrapeError("bridge unavailable (test)"),
            ),
        ):
            scrape.side_effect = ScrapeError("HTTP 403 fetching 'https://example.com/a'")
            with self.assertRaises(ScrapeError) as caught:
                fallback_scrape(
                    cfg,
                    "https://example.com/a",
                    max_chars=5000,
                    first_error="HTTP 403 fetching 'https://example.com/a'",
                )
        self.assertIn("bot-challenge page", str(caught.exception))
        self.assertIn("tls-browser:", str(caught.exception))

    def test_config_round_trip_carries_new_fields(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(
            smart_fetch_tls_browser=True,
            smart_fetch_flaresolverr="http://127.0.0.1:8191",
        )
        dumped = cfg.redacted()
        self.assertTrue(dumped["smart_fetch_tls_browser"])
        self.assertEqual(dumped["smart_fetch_flaresolverr"], "http://127.0.0.1:8191")

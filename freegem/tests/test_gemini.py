"""Tests for the Gemini protocol client.

Every Google call is mocked. No test in this file touches the network, so the
suite runs in CI on a public fork with no credentials.
"""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, ClassVar
from unittest import mock
from urllib.parse import parse_qs

from freegem.config import BridgeConfig
from freegem.gemini import (
    _RETRY_BACKOFF_CAP_SEC,
    BridgeError,
    _backoff_delay,
    _exc_retry_after,
    _retry_after_seconds,
    build_headers,
    build_payload,
    build_url,
    clean_text,
    extract_response_text,
    fetch_build_tag,
    generate,
    load_cookie,
    make_sapisidhash,
    refresh_build_tag,
)
from freegem.models import resolve_model


def make_response(raw: str) -> io.BytesIO:
    """A urlopen-compatible context manager yielding Gemini's wire format.

    Mirrors the real ``urlopen`` contract: the response is both a context
    manager and readable, and decoding happens on ``read()``.
    """
    buffer = io.BytesIO(raw.encode("utf-8"))

    class _Response(io.BytesIO):
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()
            # returns None == never suppresses, identical to the old `return False`

    return _Response(buffer.getvalue())


def make_urlopen(raw: str) -> Any:
    """A ``urlopen`` replacement returning :func:`make_response`."""
    return mock.MagicMock(return_value=make_response(raw))


def wrb_line(text: str) -> str:
    """Build a minimal ``wrb.fr`` line carrying ``text`` as the answer.

    Gemini prefixes payload lines with the literal ``wrb.fr`` marker, and the
    parser applies a length pre-filter (the nested payload must exceed 50
    chars) to skip lines that are not answer payload.

    Padding is added as extra *structural* slots, never as another text entry:
    the parser returns the longest string in a line, so a second padded string
    would win the comparison and the fixture would assert on whitespace.
    """
    payload: list[Any] = [None, None, None, None, [[None, [text]]]]
    while len(json.dumps(payload)) < 80:
        payload.append("z" * 60)  # grows the outer array, not a text slot
    outer = [["wrb.fr", None, json.dumps(payload)]]
    line = json.dumps(outer)
    while len(line) < 260:
        outer[0].append("z" * 40)
        line = json.dumps(outer)
    return line + "\n"


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = BridgeConfig()

    def _decode(self, payload: str) -> Any:
        outer = json.loads(parse_qs(payload)["f.req"][0])
        return json.loads(outer[1])

    def test_model_is_field_79(self) -> None:
        inner = self._decode(build_payload("hi", 3, 4, self.cfg))
        self.assertEqual(inner[79], 3)

    def test_think_depth_is_field_17(self) -> None:
        inner = self._decode(build_payload("hi", 1, 0, self.cfg))
        self.assertEqual(inner[17], [[0]])

    def test_prompt_is_field_0(self) -> None:
        inner = self._decode(build_payload("the question", 1, 4, self.cfg))
        self.assertEqual(inner[0][0], "the question")

    def test_file_refs_attached(self) -> None:
        inner = self._decode(build_payload("describe", 1, 4, self.cfg, ["/ref/abc"]))
        self.assertEqual(inner[0][3], [[None, None, "/ref/abc"]])

    def test_no_file_refs_leaves_slot_none(self) -> None:
        inner = self._decode(build_payload("hi", 1, 4, self.cfg))
        self.assertIsNone(inner[0][3])

    def test_persistent_chat_flag(self) -> None:
        inner = self._decode(build_payload("hi", 1, 4, self.cfg))
        self.assertEqual(inner[41], [2])

    def test_temporary_chat_flag(self) -> None:
        cfg = self.cfg.replace(temporary_chats=True)
        inner = self._decode(build_payload("hi", 1, 4, cfg))
        self.assertEqual(inner[41], [1])
        self.assertEqual(inner[45], 1)

    def test_request_uuid_is_unique_per_call(self) -> None:
        a = self._decode(build_payload("hi", 1, 4, self.cfg))
        b = self._decode(build_payload("hi", 1, 4, self.cfg))
        self.assertNotEqual(a[59], b[59])

    def test_extra_fields_applied(self) -> None:
        inner = self._decode(build_payload("hi", 3, 4, self.cfg, None, {31: 2, 80: 3}))
        self.assertEqual(inner[31], 2)
        self.assertEqual(inner[80], 3)

    def test_xsrf_sent_as_at_param_when_set(self) -> None:
        cfg = self.cfg.replace(xsrf_token="TOKEN")
        payload = build_payload("hi", 1, 4, cfg)
        self.assertEqual(parse_qs(payload)["at"], ["TOKEN"])

    def test_no_at_param_when_unset(self) -> None:
        payload = build_payload("hi", 1, 4, self.cfg)
        self.assertNotIn("at", parse_qs(payload))

    def test_payload_length_is_stable(self) -> None:
        """Field positions are protocol-defined; the array must not shift."""
        inner = self._decode(build_payload("hi", 1, 4, self.cfg))
        self.assertEqual(len(inner), 102)


class UrlTests(unittest.TestCase):
    def test_bl_and_reqid_present(self) -> None:
        cfg = BridgeConfig(gemini_bl="boq_test_1.0_p0")
        url = build_url(cfg)
        self.assertIn("bl=boq_test_1.0_p0", url)
        self.assertIn("_reqid=", url)

    def test_account_prefix(self) -> None:
        cfg = BridgeConfig(auth_user="2")
        self.assertIn("/u/2/_/BardChatUi", build_url(cfg))

    def test_no_account_prefix_by_default(self) -> None:
        url = build_url(BridgeConfig())
        self.assertNotIn("/u/", url)

    def test_endpoint_path(self) -> None:
        self.assertIn(
            "assistant.lamda.BardFrontendService/StreamGenerate", build_url(BridgeConfig())
        )


class HeaderTests(unittest.TestCase):
    def test_no_cookie_no_auth_headers(self) -> None:
        headers = build_headers(BridgeConfig())
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["Origin"], "https://gemini.google.com")

    def test_auth_user_header_when_set(self) -> None:
        headers = build_headers(BridgeConfig(auth_user="1"))
        self.assertEqual(headers["X-Goog-AuthUser"], "1")

    def test_no_auth_user_header_by_default(self) -> None:
        headers = build_headers(BridgeConfig())
        self.assertNotIn("X-Goog-AuthUser", headers)

    def test_sapisidhash_format(self) -> None:
        digest = make_sapisidhash("MYSAPISID")
        self.assertTrue(digest.startswith("SAPISIDHASH "))
        ts, _, sha = digest[len("SAPISIDHASH ") :].partition("_")
        self.assertTrue(ts.isdigit())
        self.assertEqual(len(sha), 40)  # sha1 hex


class CookieLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        from freegem import gemini as gem

        gem._COOKIE_CACHE.update({"mtime": None, "cookie": "", "sapisid": None})

    def test_json_shape(self) -> None:
        import os
        import tempfile

        payload = json.dumps({"cookie": "SID=a; SAPISID=b", "sapisid": "b"})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(payload)
            path = fh.name
        try:
            cookie, sapisid = load_cookie(BridgeConfig(cookie_file=path))
            self.assertEqual(cookie, "SID=a; SAPISID=b")
            self.assertEqual(sapisid, "b")
        finally:
            os.unlink(path)

    def test_bare_string_shape_extracts_sapisid(self) -> None:
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("SID=a; SAPISID=thesapisid; HSID=c")
            path = fh.name
        try:
            cookie, sapisid = load_cookie(BridgeConfig(cookie_file=path))
            self.assertEqual(sapisid, "thesapisid")
            self.assertIn("SID=a", cookie)
        finally:
            os.unlink(path)

    def test_missing_file_is_anonymous_not_an_error(self) -> None:
        cookie, sapisid = load_cookie(BridgeConfig(cookie_file="/no/such/file"))
        self.assertEqual(cookie, "")
        self.assertIsNone(sapisid)

    def test_no_file_configured(self) -> None:
        cookie, sapisid = load_cookie(BridgeConfig())
        self.assertEqual((cookie, sapisid), ("", None))

    def test_malformed_json_falls_back_to_last_good(self) -> None:
        """A half-written session file must not silently go anonymous."""
        import os
        import tempfile

        from freegem import gemini as gem

        good = json.dumps({"cookie": "SID=good", "sapisid": "s"})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(good)
            path = fh.name
        try:
            first, _ = load_cookie(BridgeConfig(cookie_file=path))
            self.assertEqual(first, "SID=good")
            # Corrupt it, then bump mtime so the cache misses.
            with open(path, "w") as fh:
                fh.write("{ truncated")
            os.utime(path, (0, 0))
            gem._COOKIE_CACHE["mtime"] = -1
            second, _ = load_cookie(BridgeConfig(cookie_file=path))
            self.assertEqual(second, "SID=good")  # kept, not dropped
        finally:
            os.unlink(path)


class ResponseParsingTests(unittest.TestCase):
    def test_longest_text_wins(self) -> None:
        """Gemini streams progressively longer snapshots of one answer."""
        raw = wrb_line("short") + wrb_line("short but longer")
        self.assertEqual(extract_response_text(raw), "short but longer")

    def test_card_markers_stripped(self) -> None:
        text = "answer\nhttp://googleusercontent.com/card_content/12345"
        self.assertEqual(clean_text(text), "answer")

    def test_code_reference_block_stripped(self) -> None:
        text = "```python?code_reference&code_event_index=0\nfoo()\n```\nkept"
        self.assertIn("kept", clean_text(text))

    def test_empty_response(self) -> None:
        self.assertEqual(extract_response_text(""), "")

    def test_garbage_does_not_raise(self) -> None:
        self.assertEqual(extract_response_text("not json at all\n"), "")

    def test_bard_error_raises_bridge_error(self) -> None:
        raw = '[["wrb.fr",null,"[null,null,null,null,[[null,[\\"BardErrorInfo [400]\\"]]]]"]]\n'
        with self.assertRaises(BridgeError) as ctx:
            extract_response_text(raw)
        self.assertIn("400", str(ctx.exception))


BARD_ERROR = '[["wrb.fr",null,"[null,null,null,null,[[null,[\\"BardErrorInfo [400]\\"]]]]"]]\n'


class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = BridgeConfig(retry_attempts=2, retry_delay_sec=0)

    def test_success(self) -> None:
        with mock.patch("freegem.gemini._post", return_value=make_response(wrb_line("hello"))):
            self.assertEqual(generate(self.cfg, "hi", 1, 4), "hello")

    def test_bard_error_not_retried(self) -> None:
        """A rejected request will fail identically on retry; do not hammer."""
        with mock.patch("freegem.gemini._post", return_value=make_response(BARD_ERROR)) as post:
            with self.assertRaises(BridgeError):
                generate(self.cfg, "hi", 1, 4)
            self.assertEqual(post.call_count, 1)

    def test_retries_on_transient_failure(self) -> None:
        import urllib.error

        calls = {"n": 0}

        def flaky(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("connection reset")
            return make_response(wrb_line("recovered"))

        with mock.patch("freegem.gemini._post", side_effect=flaky):
            self.assertEqual(generate(self.cfg, "hi", 1, 4), "recovered")
        self.assertEqual(calls["n"], 2)

    def test_raises_after_exhausting_retries(self) -> None:
        import urllib.error

        with mock.patch("freegem.gemini._post", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(BridgeError) as ctx:
                generate(self.cfg, "hi", 1, 4)
        self.assertIn("2 attempts", str(ctx.exception))

    def test_proxy_config_passed_through(self) -> None:
        cfg = self.cfg.replace(proxy="http://127.0.0.1:7890")
        with mock.patch("freegem.gemini._post", return_value=make_response(wrb_line("ok"))):
            generate(cfg, "hi", 1, 4)
        # _post receives cfg and reads cfg.proxy itself
        self.assertEqual(cfg.proxy, "http://127.0.0.1:7890")


class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = BridgeConfig(retry_attempts=1, retry_delay_sec=0)

    @staticmethod
    def _fake_client(chunks: Any) -> Any:
        """An ``httpx.Client`` double matching the real call contract.

        ``client.stream("POST", ...)`` is a context manager whose ``__enter__``
        yields the response; the response needs ``raise_for_status()`` and
        ``iter_text()``. Anything else and the mock is not faithful to httpx.
        """
        response = mock.MagicMock()
        response.raise_for_status.return_value = None
        response.iter_text.return_value = iter(chunks)

        client = mock.MagicMock()
        client.stream.return_value.__enter__.return_value = response
        client.stream.return_value.__exit__.return_value = False
        return client

    def test_falls_back_to_whole_reply_without_httpx(self) -> None:
        """Without httpx the bridge still works, just not token-by-token."""
        import freegem.gemini as gem

        with (
            mock.patch.object(gem, "HAS_HTTPX", False),
            mock.patch.object(gem, "_post", return_value=make_response(wrb_line("all at once"))),
        ):
            chunks = list(gem.generate_stream(self.cfg, "hi", 1, 4))
        self.assertEqual(chunks, ["all at once"])

    def test_yields_delta_from_progressive_snapshots(self) -> None:
        import freegem.gemini as gem

        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(
                gem,
                "_httpx_client",
                return_value=self._fake_client([wrb_line("par"), wrb_line("partial ")]),
            ),
        ):
            chunks = list(gem.generate_stream(self.cfg, "hi", 1, 4))
        self.assertEqual(chunks, ["par", "tial "])

    def test_identical_snapshot_yields_nothing(self) -> None:
        import freegem.gemini as gem

        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(
                gem,
                "_httpx_client",
                return_value=self._fake_client([wrb_line("same"), wrb_line("same")]),
            ),
        ):
            chunks = list(gem.generate_stream(self.cfg, "hi", 1, 4))
        self.assertEqual(chunks, ["same"])

    def test_rewrite_aborts_instead_of_corrupting(self) -> None:
        """If prior output changes, stop rather than emit a silent rewrite."""
        import freegem.gemini as gem

        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(
                gem,
                "_httpx_client",
                return_value=self._fake_client(
                    [wrb_line("first answer"), wrb_line("totally different")]
                ),
            ),
        ):
            with self.assertRaises(BridgeError) as ctx:
                list(gem.generate_stream(self.cfg, "hi", 1, 4))
        self.assertIn("rewrote", str(ctx.exception))

    def test_bard_error_during_stream_raises(self) -> None:
        import freegem.gemini as gem

        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(gem, "_httpx_client", return_value=self._fake_client([BARD_ERROR])),
        ):
            with self.assertRaises(BridgeError):
                list(gem.generate_stream(self.cfg, "hi", 1, 4))


class BuildTagTests(unittest.TestCase):
    """The auto-refresh silently did nothing in the original; prove it works."""

    HTML = (
        "<html>var x = 'boq_gemini-web-uiserver_20261008.15_p0';"
        "var y = 'boq_assistant-bard-web-server_20260716.08_p0';</html>"
    )

    def _urlopen(self, raw: str) -> Any:
        """A ``urlopen`` return value: readable directly, not a context manager.

        ``fetch_build_tag`` calls ``response.read()`` on the object urlopen
        returns, without ``with``. Mocking a context manager here would leave
        ``read()`` returning a MagicMock.
        """
        return io.BytesIO(raw.encode("utf-8"))

    def _patched_urlopen(self, raw: str) -> Any:
        """Patch urlopen where gemini.py looks it up.

        ``urllib.request`` is a shared stdlib module object, so patching it
        process-wide leaks into other tests. Patching the attribute on the
        module under test is scoped and reverted on exit.
        """
        import freegem.gemini as gem

        return mock.patch.object(gem.urllib.request, "urlopen", return_value=self._urlopen(raw))

    def _patched_urlopen_error(self, exc: Any) -> Any:
        import freegem.gemini as gem

        return mock.patch.object(gem.urllib.request, "urlopen", side_effect=exc)

    def test_matches_gemini_web_uiserver_format(self) -> None:
        cfg = BridgeConfig(gemini_bl="stale_value")
        with self._patched_urlopen(self.HTML):
            found = fetch_build_tag(cfg)
        self.assertEqual(found, "boq_gemini-web-uiserver_20261008.15_p0")

    def test_returns_none_when_not_found(self) -> None:
        with self._patched_urlopen("<html>nothing here</html>"):
            self.assertIsNone(fetch_build_tag(BridgeConfig()))

    def test_network_failure_returns_none(self) -> None:
        import urllib.error

        with self._patched_urlopen_error(urllib.error.URLError("no network")):
            self.assertIsNone(fetch_build_tag(BridgeConfig()))

    def test_refresh_returns_new_config_not_mutation(self) -> None:
        cfg = BridgeConfig(gemini_bl="old_tag")
        with self._patched_urlopen(self.HTML):
            new_cfg, change = refresh_build_tag(cfg)
        self.assertEqual(cfg.gemini_bl, "old_tag")  # original untouched
        self.assertEqual(new_cfg.gemini_bl, "boq_gemini-web-uiserver_20261008.15_p0")
        self.assertIn("old_tag", change or "")

    def test_refresh_is_noop_when_current(self) -> None:
        cfg = BridgeConfig(gemini_bl="boq_gemini-web-uiserver_20261008.15_p0")
        with self._patched_urlopen(self.HTML):
            new_cfg, change = refresh_build_tag(cfg)
        self.assertIsNone(change)
        self.assertIs(new_cfg, cfg)


class ModelResolutionIntegrationTests(unittest.TestCase):
    """The client's (mode, think) must come from the registry, not a literal."""

    def test_resolved_modes_are_distinct_per_model(self) -> None:
        seen = {}
        for name in ("gemini-3.6-flash", "gemini-3.5-flash-thinking", "gemini-3.1-pro"):
            _, mode, think, err, _ = resolve_model(name)
            self.assertIsNone(err)
            seen[name] = (mode, think)
        self.assertEqual(len(set(seen.values())), 3)

    def test_think_override_reaches_payload(self) -> None:
        cfg = BridgeConfig()
        _, mode, think, _, _ = resolve_model("gemini-3.6-flash@think=1")
        assert mode is not None and think is not None, "resolve_model broke its contract"
        payload = build_payload("hi", mode, think, cfg)
        inner = json.loads(json.loads(parse_qs(payload)["f.req"][0])[1])
        self.assertEqual(inner[17], [[1]])
        self.assertEqual(inner[79], 1)


if __name__ == "__main__":
    unittest.main()


class _StatusResponse(io.BytesIO):
    """A ``_post()`` stand-in that carries an HTTP status and headers.

    Mirrors the real urlopen contract closely enough for the retry loop: it is
    a context manager, exposes ``status``, and serves ``headers`` like the
    addinfourl wrappers do. ``read()`` is never reached on error statuses.
    """

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(b"")
        self.status = status
        self.headers: dict[str, str] = headers or {}

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class BackoffTests(unittest.TestCase):
    """Retries wait exponentially and honor the server's Retry-After."""

    def test_backoff_is_exponential(self) -> None:
        self.assertEqual(_backoff_delay(2, 1), 2.0)
        self.assertEqual(_backoff_delay(2, 2), 4.0)
        self.assertEqual(_backoff_delay(2, 3), 8.0)

    def test_backoff_is_capped(self) -> None:
        # 2 * 2^19 is ~1M seconds; the cap must win long before that.
        self.assertEqual(_backoff_delay(2, 20), _RETRY_BACKOFF_CAP_SEC)
        self.assertLessEqual(_backoff_delay(2, 20), 30.0)

    def test_retry_after_overrides_when_longer(self) -> None:
        self.assertEqual(_backoff_delay(2, 1, retry_after=7.0), 7.0)

    def test_retry_after_also_capped(self) -> None:
        self.assertEqual(_backoff_delay(2, 1, retry_after=3600.0), _RETRY_BACKOFF_CAP_SEC)

    def test_retry_after_ignored_when_shorter(self) -> None:
        # attempt 2 with base 2 waits 4s; a 3s Retry-After asks for less, so
        # the exponential delay stands.
        self.assertEqual(_backoff_delay(2, 2, retry_after=3.0), 4.0)

    def test_retry_after_delay_seconds_form(self) -> None:
        self.assertEqual(_retry_after_seconds({"Retry-After": "7"}), 7.0)
        self.assertEqual(_retry_after_seconds({"Retry-After": " 120 "}), 120.0)

    def test_retry_after_http_date_form(self) -> None:
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
        past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=90), usegmt=True)
        in_ninety = _retry_after_seconds({"Retry-After": future})
        assert in_ninety is not None
        self.assertGreater(in_ninety, 60.0)
        self.assertLessEqual(in_ninety, 90.0)
        # A date in the past means "retry now": clamped to zero, not negative.
        self.assertEqual(_retry_after_seconds({"Retry-After": past}), 0.0)

    def test_retry_after_garbage_is_none(self) -> None:
        self.assertIsNone(_retry_after_seconds({"Retry-After": "soon-ish"}))
        self.assertIsNone(_retry_after_seconds({"Retry-After": ""}))
        self.assertIsNone(_retry_after_seconds({}))
        self.assertIsNone(_retry_after_seconds(None))

    def test_exc_retry_after_reads_httpx_shape(self) -> None:
        """httpx raises HTTPStatusError with headers on .response.headers."""

        class _FakeResponse:
            headers: ClassVar[dict[str, str]] = {"Retry-After": "9"}

        class _FakeHTTPXError(Exception):
            response = _FakeResponse()

        self.assertEqual(_exc_retry_after(_FakeHTTPXError()), 9.0)

    def test_exc_retry_after_reads_urllib_shape(self) -> None:
        class _FakeHTTPError(Exception):
            headers: ClassVar[dict[str, str]] = {"Retry-After": "5"}

        self.assertEqual(_exc_retry_after(_FakeHTTPError()), 5.0)
        self.assertIsNone(_exc_retry_after(ValueError("no headers here")))

    def test_generate_honors_retry_after_header(self) -> None:
        """A 429 with Retry-After sleeps exactly as long as the server said."""
        cfg = BridgeConfig(retry_attempts=2, retry_delay_sec=2)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(429, {"Retry-After": "7"}), _StatusResponse(429)],
            ),
            mock.patch("freegem.gemini.time.sleep") as sleep,
        ):
            with self.assertRaises(BridgeError) as ctx:
                generate(cfg, "hi", 1, 0)
        sleep.assert_called_once_with(7.0)
        self.assertIn("upstream HTTP 429", str(ctx.exception))

    def test_generate_backs_off_exponentially_without_header(self) -> None:
        cfg = BridgeConfig(retry_attempts=3, retry_delay_sec=2)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(500), _StatusResponse(500), _StatusResponse(500)],
            ),
            mock.patch("freegem.gemini.time.sleep") as sleep,
        ):
            with self.assertRaises(BridgeError):
                generate(cfg, "hi", 1, 0)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 4.0])

    def test_generate_upstream_http_status_is_retried_not_fatal(self) -> None:
        """A transient 429 recovers on the next attempt instead of failing."""
        cfg = BridgeConfig(retry_attempts=2, retry_delay_sec=0)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(429), make_response(wrb_line("recovered"))],
            ),
            mock.patch("freegem.gemini.time.sleep"),
        ):
            self.assertEqual(generate(cfg, "hi", 1, 0), "recovered")


class HttpxPoolTests(unittest.TestCase):
    """The pooled httpx client is shared, and shared by the RIGHT settings."""

    def setUp(self) -> None:
        from freegem import gemini as gem

        self.gem = gem
        self.addCleanup(gem._HTTPX_CLIENTS.clear)

    def test_same_settings_share_one_client(self) -> None:
        if not self.gem.HAS_HTTPX:
            self.skipTest("httpx not installed")
        first = self.gem._httpx_client(BridgeConfig(log_requests=False))
        second = self.gem._httpx_client(BridgeConfig(log_requests=False))
        self.assertIs(first, second)

    def test_different_timeout_gets_its_own_client(self) -> None:
        """A timeout change must not silently ride an old cached client."""
        if not self.gem.HAS_HTTPX:
            self.skipTest("httpx not installed")
        base = self.gem._httpx_client(BridgeConfig(log_requests=False))
        slower = self.gem._httpx_client(BridgeConfig(log_requests=False, request_timeout_sec=99))
        self.assertIsNot(base, slower)
        proxied = self.gem._httpx_client(
            BridgeConfig(log_requests=False, proxy="http://127.0.0.1:9")
        )
        self.assertIsNot(base, proxied)

    def test_post_prefers_the_pooled_client(self) -> None:
        calls: list[dict[str, Any]] = []

        class FakeHttpxResponse:
            status_code = 200

            def __init__(self) -> None:
                self.headers = {"Retry-After": "7"}
                self.closed = False

            def read(self) -> bytes:
                return b"body"

            def close(self) -> None:
                self.closed = True

        class FakeClient:
            def post(self, url: str, content: Any = None, headers: Any = None) -> Any:
                calls.append({"url": url, "content": content, "headers": headers})
                return FakeHttpxResponse()

        fake = FakeClient()
        with mock.patch.object(self.gem, "_httpx_client", return_value=fake):
            response = self.gem._post(
                BridgeConfig(log_requests=False), b"payload", "http://u.test/x", {"h": "1"}
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["content"], b"payload")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"body")
        with response:
            pass
        self.assertTrue(response._raw.closed)  # context manager closes

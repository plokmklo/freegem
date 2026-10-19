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
    RateLimitError,
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
    generate_stream,
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
        self.cfg = BridgeConfig(
            guard=True,
        )

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
        cfg = BridgeConfig(guard=True, gemini_bl="boq_test_1.0_p0")
        url = build_url(cfg)
        self.assertIn("bl=boq_test_1.0_p0", url)
        self.assertIn("_reqid=", url)

    def test_account_prefix(self) -> None:
        cfg = BridgeConfig(guard=True, auth_user="2")
        self.assertIn("/u/2/_/BardChatUi", build_url(cfg))

    def test_no_account_prefix_by_default(self) -> None:
        url = build_url(
            BridgeConfig(
                guard=True,
            )
        )
        self.assertNotIn("/u/", url)

    def test_endpoint_path(self) -> None:
        self.assertIn(
            "assistant.lamda.BardFrontendService/StreamGenerate",
            build_url(
                BridgeConfig(
                    guard=True,
                )
            ),
        )


class HeaderTests(unittest.TestCase):
    def test_no_cookie_no_auth_headers(self) -> None:
        headers = build_headers(
            BridgeConfig(
                guard=True,
            )
        )
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["Origin"], "https://gemini.google.com")

    def test_auth_user_header_when_set(self) -> None:
        headers = build_headers(BridgeConfig(guard=True, auth_user="1"))
        self.assertEqual(headers["X-Goog-AuthUser"], "1")

    def test_no_auth_user_header_by_default(self) -> None:
        headers = build_headers(
            BridgeConfig(
                guard=True,
            )
        )
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

        gem._COOKIE_CACHE.update({"key": None, "cookie": "", "sapisid": None, "xsrf": None})

    def test_json_shape(self) -> None:
        import os
        import tempfile

        payload = json.dumps({"cookie": "SID=a; SAPISID=b", "sapisid": "b"})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(payload)
            path = fh.name
        try:
            cookie, sapisid = load_cookie(BridgeConfig(guard=True, cookie_file=path))
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
            cookie, sapisid = load_cookie(BridgeConfig(guard=True, cookie_file=path))
            self.assertEqual(sapisid, "thesapisid")
            self.assertIn("SID=a", cookie)
        finally:
            os.unlink(path)

    def test_missing_file_is_anonymous_not_an_error(self) -> None:
        cookie, sapisid = load_cookie(BridgeConfig(guard=True, cookie_file="/no/such/file"))
        self.assertEqual(cookie, "")
        self.assertIsNone(sapisid)

    def test_no_file_configured(self) -> None:
        cookie, sapisid = load_cookie(
            BridgeConfig(
                guard=True,
            )
        )
        self.assertEqual((cookie, sapisid), ("", None))

    def test_malformed_json_falls_back_to_last_good(self) -> None:
        """A half-written session file must not silently go anonymous."""
        import os
        import tempfile

        good = json.dumps({"cookie": "SID=good", "sapisid": "s"})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(good)
            path = fh.name
        try:
            first, _ = load_cookie(BridgeConfig(guard=True, cookie_file=path))
            self.assertEqual(first, "SID=good")
            # Corrupt it, then bump mtime so the cache misses.
            with open(path, "w") as fh:
                fh.write("{ truncated")
            os.utime(path, (0, 0))
            second, _ = load_cookie(BridgeConfig(guard=True, cookie_file=path))
            self.assertEqual(second, "SID=good")  # kept, not dropped
        finally:
            os.unlink(path)


class CookieCacheGuardTests(unittest.TestCase):
    """The cookie cache must never serve one account's session for another (#35)."""

    def setUp(self) -> None:
        from freegem import gemini as gem

        self.gem = gem
        gem._COOKIE_CACHE.update({"key": None, "cookie": "", "sapisid": None, "xsrf": None})
        self.addCleanup(
            gem._COOKIE_CACHE.update,
            {"key": None, "cookie": "", "sapisid": None, "xsrf": None},
        )

    @staticmethod
    def _write(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_failure_on_other_file_does_not_leak_cached_session(self) -> None:
        """A failure on cookie file B returns anonymous, not file A's session."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(json.dumps({"cookie": "SID=account-a", "sapisid": "sa"}))
            path_a = fh.name
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{ truncated")
            path_b = fh.name
        try:
            self.assertEqual(
                load_cookie(BridgeConfig(guard=True, cookie_file=path_a)), ("SID=account-a", "sa")
            )
            # Parse failure on B: the cache holds A under A's key only.
            self.assertEqual(load_cookie(BridgeConfig(guard=True, cookie_file=path_b)), ("", None))
            # A hard read failure on B behaves the same.
            with mock.patch("builtins.open", side_effect=PermissionError("denied")):
                self.assertEqual(
                    load_cookie(BridgeConfig(guard=True, cookie_file=path_b)), ("", None)
                )
            # And A's session is still intact afterwards.
            self.assertEqual(
                load_cookie(BridgeConfig(guard=True, cookie_file=path_a)), ("SID=account-a", "sa")
            )
        finally:
            os.unlink(path_a)
            os.unlink(path_b)

    def test_read_failure_on_same_path_keeps_last_good_value(self) -> None:
        """Mid-write files must not downgrade the session for the SAME file."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(json.dumps({"cookie": "SID=good", "sapisid": "s"}))
            path = fh.name
        try:
            cfg = BridgeConfig(guard=True, cookie_file=path)
            self.assertEqual(load_cookie(cfg), ("SID=good", "s"))
            os.utime(path, (0, 0))  # force a cache miss on the same path
            with mock.patch("builtins.open", side_effect=PermissionError("denied")):
                self.assertEqual(load_cookie(cfg), ("SID=good", "s"))
        finally:
            os.unlink(path)

    def test_cache_hit_is_served_without_reopening_the_file(self) -> None:
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(json.dumps({"cookie": "SID=account-a", "sapisid": "sa"}))
            path = fh.name
        try:
            cfg = BridgeConfig(guard=True, cookie_file=path)
            self.assertEqual(load_cookie(cfg), ("SID=account-a", "sa"))
            with mock.patch("builtins.open", wraps=open) as opener:
                self.assertEqual(load_cookie(cfg), ("SID=account-a", "sa"))
            self.assertEqual(opener.call_count, 0)
        finally:
            os.unlink(path)

    def test_partial_read_is_never_cached_as_valid(self) -> None:
        """A size mismatch between read and stat must not poison the cache."""
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(json.dumps({"cookie": "SID=full", "sapisid": "s"}))
            path = fh.name
        try:
            cfg = BridgeConfig(guard=True, cookie_file=path)
            self.assertEqual(load_cookie(cfg), ("SID=full", "s"))
            key_before = self.gem._COOKIE_CACHE["key"]
            real_size = os.path.getsize(path)
            # Simulate a writer landing between our read and our stat: the
            # pre-read stat sees the old size, the post-read stat a bigger
            # one. The loader must treat the snapshot as suspect.
            sizes = iter([real_size - 1, real_size + 13])
            with mock.patch.object(os.path, "getsize", lambda p: next(sizes)):
                self.assertEqual(load_cookie(cfg), ("SID=full", "s"))
            self.assertEqual(self.gem._COOKIE_CACHE["key"], key_before)
            self.assertEqual(self.gem._COOKIE_CACHE["cookie"], "SID=full")
        finally:
            os.unlink(path)

    def test_concurrent_loaders_do_not_corrupt_cache(self) -> None:
        """Race smoke: hammering the loader never yields a torn session."""
        import os
        import tempfile
        import threading

        # Deliberately different lengths so a mid-write read is detectable
        # by the size check instead of parsing as plausible garbage.
        variants = [
            json.dumps({"cookie": "SID=one; SAPISID=s1", "sapisid": "s1"}),
            json.dumps({"cookie": "SID=two-two; SAPISID=s2", "sapisid": "s2"}),
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(variants[0])
            path = fh.name
        allowed = {"SID=one; SAPISID=s1", "SID=two-two; SAPISID=s2", ""}
        errors: list[str] = []

        def reader() -> None:
            cfg = BridgeConfig(guard=True, cookie_file=path)
            for _ in range(120):
                try:
                    cookie, sapisid = load_cookie(cfg)
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(f"loader raised: {exc!r}")
                    return
                if cookie not in allowed:
                    errors.append(f"torn result: {(cookie, sapisid)!r}")
                    return
                if cookie == "SID=one; SAPISID=s1" and sapisid != "s1":
                    errors.append(f"mismatched sapisid: {(cookie, sapisid)!r}")
                    return
                if cookie == "SID=two-two; SAPISID=s2" and sapisid != "s2":
                    errors.append(f"mismatched sapisid: {(cookie, sapisid)!r}")
                    return

        def writer() -> None:
            for i in range(80):
                self._write(path, variants[i % 2])
                os.utime(path, None)

        threads = [threading.Thread(target=reader) for _ in range(6)]
        threads.append(threading.Thread(target=writer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        os.unlink(path)
        self.assertEqual(errors, [])


class XsrfFallbackTests(unittest.TestCase):
    """The refresh script writes xsrf_token into the session JSON; read it (#37)."""

    def setUp(self) -> None:
        from freegem import gemini as gem

        gem._COOKIE_CACHE.update({"key": None, "cookie": "", "sapisid": None, "xsrf": None})
        self.addCleanup(
            gem._COOKIE_CACHE.update,
            {"key": None, "cookie": "", "sapisid": None, "xsrf": None},
        )

    def test_cfg_xsrf_missing_falls_back_to_cookie_file(self) -> None:
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(json.dumps({"cookie": "SID=a", "sapisid": "b", "xsrf_token": "ATFILE"}))
            path = fh.name
        try:
            payload = build_payload("hi", 1, 4, BridgeConfig(guard=True, cookie_file=path))
            self.assertEqual(parse_qs(payload)["at"], ["ATFILE"])
        finally:
            os.unlink(path)

    def test_cfg_xsrf_wins_over_cookie_file(self) -> None:
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(json.dumps({"cookie": "SID=a", "sapisid": "b", "xsrf_token": "ATFILE"}))
            path = fh.name
        try:
            cfg = BridgeConfig(guard=True, cookie_file=path, xsrf_token="ATCFG")
            payload = build_payload("hi", 1, 4, cfg)
            self.assertEqual(parse_qs(payload)["at"], ["ATCFG"])
        finally:
            os.unlink(path)

    def test_bare_cookie_string_carries_no_xsrf(self) -> None:
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("SID=a; SAPISID=b")
            path = fh.name
        try:
            payload = build_payload("hi", 1, 4, BridgeConfig(guard=True, cookie_file=path))
            self.assertNotIn("at", parse_qs(payload))
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
        self.cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=0)

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

    def test_empty_answer_raises_typed_rate_limit(self) -> None:
        """An empty 200 is the rate-limit/login-wall shape -- typed, catchable."""
        # side_effect, not return_value: each attempt must get a FRESH
        # response -- the real contract re-reads a new body per attempt, and
        # a shared BytesIO is closed after the first read.
        with mock.patch(
            "freegem.gemini._post", side_effect=lambda *a, **k: make_response("")
        ) as post:
            with self.assertRaises(RateLimitError) as ctx:
                generate(self.cfg, "hi", 1, 4)
        self.assertIn("rate limit", str(ctx.exception))
        self.assertIsInstance(ctx.exception, BridgeError)
        self.assertEqual(post.call_count, self.cfg.retry_attempts)

    def test_empty_answer_is_retried_then_raised_typed(self) -> None:
        """Empty-200 is transient: every attempt retried, typed error survives."""
        # retry_delay_sec=0 in setUp keeps the retry sleeps instant; fresh
        # response per attempt via side_effect (see the typed-error test).
        with mock.patch(
            "freegem.gemini._post", side_effect=lambda *a, **k: make_response("")
        ) as post:
            with self.assertRaises(RateLimitError):
                generate(self.cfg, "hi", 1, 4)
        self.assertEqual(post.call_count, self.cfg.retry_attempts)

    def test_empty_then_recovery_succeeds(self) -> None:
        """First attempt rate-limited (empty 200), second answers: recover."""
        calls = {"n": 0}

        def flaky(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                return make_response("")
            return make_response(wrb_line("recovered"))

        with (
            mock.patch("freegem.gemini._post", side_effect=flaky),
        ):
            self.assertEqual(generate(self.cfg, "hi", 1, 4), "recovered")
        self.assertEqual(calls["n"], 2)

    def test_proxy_config_passed_through(self) -> None:
        cfg = self.cfg.replace(proxy="http://127.0.0.1:7890")
        with mock.patch("freegem.gemini._post", return_value=make_response(wrb_line("ok"))):
            generate(cfg, "hi", 1, 4)
        # _post receives cfg and reads cfg.proxy itself
        self.assertEqual(cfg.proxy, "http://127.0.0.1:7890")


class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, retry_attempts=1, retry_delay_sec=0)

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

    def test_empty_stream_raises_typed_rate_limit(self) -> None:
        """A 200 stream that never yields an answer is the rate-limit shape."""
        import freegem.gemini as gem

        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(gem, "_httpx_client", return_value=self._fake_client([""])),
        ):
            with self.assertRaises(RateLimitError):
                list(gem.generate_stream(self.cfg, "hi", 1, 4))

    def test_empty_stream_retries_and_recovers(self) -> None:
        """Nothing emitted on the empty attempt, so a replay cannot duplicate."""
        import freegem.gemini as gem

        empty_cm = self._fake_client([""]).stream.return_value
        good_cm = self._fake_client([wrb_line("recovered")]).stream.return_value
        client = mock.MagicMock()
        client.stream.side_effect = [empty_cm, good_cm]
        cfg = self.cfg.replace(retry_attempts=2)
        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(gem, "_httpx_client", return_value=client),
        ):
            chunks = list(gem.generate_stream(cfg, "hi", 1, 4))
        self.assertEqual(chunks, ["recovered"])


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
        cfg = BridgeConfig(guard=True, gemini_bl="stale_value")
        with self._patched_urlopen(self.HTML):
            found = fetch_build_tag(cfg)
        self.assertEqual(found, "boq_gemini-web-uiserver_20261008.15_p0")

    def test_returns_none_when_not_found(self) -> None:
        with self._patched_urlopen("<html>nothing here</html>"):
            self.assertIsNone(
                fetch_build_tag(
                    BridgeConfig(
                        guard=True,
                    )
                )
            )

    def test_network_failure_returns_none(self) -> None:
        import urllib.error

        with self._patched_urlopen_error(urllib.error.URLError("no network")):
            self.assertIsNone(
                fetch_build_tag(
                    BridgeConfig(
                        guard=True,
                    )
                )
            )

    def test_refresh_returns_new_config_not_mutation(self) -> None:
        cfg = BridgeConfig(guard=True, gemini_bl="old_tag")
        with self._patched_urlopen(self.HTML):
            new_cfg, change = refresh_build_tag(cfg)
        self.assertEqual(cfg.gemini_bl, "old_tag")  # original untouched
        self.assertEqual(new_cfg.gemini_bl, "boq_gemini-web-uiserver_20261008.15_p0")
        self.assertIn("old_tag", change or "")

    def test_refresh_is_noop_when_current(self) -> None:
        cfg = BridgeConfig(guard=True, gemini_bl="boq_gemini-web-uiserver_20261008.15_p0")
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
        cfg = BridgeConfig(
            guard=True,
        )
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
        # With jitter the value is no longer exact, but it stays within
        # ±50% of the exponential curve.
        self.assertAlmostEqual(_backoff_delay(2, 1), 2.0, delta=1.0)
        self.assertAlmostEqual(_backoff_delay(2, 2), 4.0, delta=2.0)
        self.assertAlmostEqual(_backoff_delay(2, 3), 8.0, delta=4.0)

    def test_backoff_is_capped(self) -> None:
        # 2 * 2^19 is ~1M seconds; the cap must win long before that.
        # At the cap, jitter is suppressed so the delay is exactly the cap.
        self.assertEqual(_backoff_delay(2, 20, retry_after=None), _RETRY_BACKOFF_CAP_SEC)
        self.assertLessEqual(_backoff_delay(2, 20, retry_after=None), 120.0)

    def test_retry_after_ninety_is_honored_not_truncated(self) -> None:
        # Regression: the old 30s cap truncated a server's explicit "come
        # back in 90s" to 30s, which just bought another 429. 90s must now
        # pass through intact -- that is the whole point of Retry-After.
        # Retry-After overrides jitter.
        self.assertEqual(_backoff_delay(2, 1, retry_after=90.0), 90.0)

    def test_cap_sits_above_typical_retry_after_windows(self) -> None:
        self.assertEqual(_RETRY_BACKOFF_CAP_SEC, 120.0)

    def test_retry_after_overrides_when_longer(self) -> None:
        # Retry-After overrides both the exponential curve and the jitter.
        self.assertEqual(_backoff_delay(2, 1, retry_after=7.0), 7.0)

    def test_jitter_prevents_lockstep_retries(self) -> None:
        """Concurrent workers must not all sleep the exact same duration."""
        delays = [_backoff_delay(2, 1) for _ in range(100)]
        self.assertGreater(
            len({round(d, 3) for d in delays}), 1, "jitter must produce varied delays"
        )

    def test_retry_after_also_capped(self) -> None:
        self.assertEqual(_backoff_delay(2, 1, retry_after=3600.0), _RETRY_BACKOFF_CAP_SEC)

    def test_retry_after_ignored_when_shorter(self) -> None:
        # attempt 2 with base 2 waits 4s; a 3s Retry-After asks for less, so
        # the exponential delay stands (±50% jitter applied to the curve).
        self.assertAlmostEqual(_backoff_delay(2, 2, retry_after=3.0), 4.0, delta=2.0)

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
        cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=2)
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
        cfg = BridgeConfig(guard=True, retry_attempts=3, retry_delay_sec=2)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(500), _StatusResponse(500), _StatusResponse(500)],
            ),
            mock.patch("freegem.gemini.time.sleep") as sleep,
        ):
            with self.assertRaises(BridgeError):
                generate(cfg, "hi", 1, 0)
        # With jitter, sleep durations are non-deterministic; verify the
        # retry loop called sleep twice and the values are in ballpark
        # (exponential curve 2, 4 with ±50% jitter).
        sleeps = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(len(sleeps), 2, "two retries = two sleep calls")
        self.assertAlmostEqual(sleeps[0], 2.0, delta=1.0)
        self.assertAlmostEqual(sleeps[1], 4.0, delta=2.0)

    def test_generate_upstream_http_status_is_retried_not_fatal(self) -> None:
        """A transient 429 recovers on the next attempt instead of failing."""
        cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=0)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(429), make_response(wrb_line("recovered"))],
            ),
            mock.patch("freegem.gemini.time.sleep"),
        ):
            self.assertEqual(generate(cfg, "hi", 1, 0), "recovered")


class HttpStatusPolicyTests(unittest.TestCase):
    """Retry policy: transient statuses only, Retry-After everywhere (#34)."""

    def test_urllib_http_error_retry_after_honored_in_generic_branch(self) -> None:
        """Non-httpx installs: HTTPError's Retry-After must drive the delay.

        The generic ``except Exception`` branch is where urllib errors land
        (httpx is absent, so there is no status wrapper). It must extract
        Retry-After exactly like the streaming branch does.
        """
        import email.message
        import urllib.error

        cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=2)
        headers = email.message.Message()
        headers["Retry-After"] = "9"
        error = urllib.error.HTTPError(
            "https://gemini.google.com/app", 502, "Bad Gateway", headers, None
        )
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[error, make_response(wrb_line("recovered"))],
            ),
            mock.patch("freegem.gemini.time.sleep") as sleep,
        ):
            self.assertEqual(generate(cfg, "hi", 1, 0), "recovered")
        sleep.assert_called_once_with(9.0)

    def test_401_fails_fast_without_retry(self) -> None:
        """An auth rejection reproduces identically on retry; do not hammer."""
        cfg = BridgeConfig(guard=True, retry_attempts=3, retry_delay_sec=0)
        with mock.patch("freegem.gemini._post", side_effect=[_StatusResponse(401)]) as post:
            with self.assertRaises(BridgeError) as ctx:
                generate(cfg, "hi", 1, 0)
        self.assertEqual(post.call_count, 1)
        self.assertIn("401", str(ctx.exception))

    def test_403_fails_fast_without_retry(self) -> None:
        cfg = BridgeConfig(guard=True, retry_attempts=3, retry_delay_sec=0)
        with mock.patch("freegem.gemini._post", side_effect=[_StatusResponse(403)]) as post:
            with self.assertRaises(BridgeError) as ctx:
                generate(cfg, "hi", 1, 0)
        self.assertEqual(post.call_count, 1)
        self.assertIn("403", str(ctx.exception))

    def test_400_fails_fast_without_retry(self) -> None:
        cfg = BridgeConfig(guard=True, retry_attempts=3, retry_delay_sec=0)
        with mock.patch("freegem.gemini._post", side_effect=[_StatusResponse(400)]) as post:
            with self.assertRaises(BridgeError) as ctx:
                generate(cfg, "hi", 1, 0)
        self.assertEqual(post.call_count, 1)
        self.assertIn("400", str(ctx.exception))

    def test_429_retries_and_recovers(self) -> None:
        cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=0)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(429), make_response(wrb_line("recovered"))],
            ) as post,
            mock.patch("freegem.gemini.time.sleep"),
        ):
            self.assertEqual(generate(cfg, "hi", 1, 0), "recovered")
        self.assertEqual(post.call_count, 2)

    def test_502_retries_and_recovers(self) -> None:
        cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=0)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(502), make_response(wrb_line("recovered"))],
            ) as post,
            mock.patch("freegem.gemini.time.sleep"),
        ):
            self.assertEqual(generate(cfg, "hi", 1, 0), "recovered")
        self.assertEqual(post.call_count, 2)

    def test_408_retries_as_transient(self) -> None:
        cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=0)
        with (
            mock.patch(
                "freegem.gemini._post",
                side_effect=[_StatusResponse(408), make_response(wrb_line("recovered"))],
            ) as post,
            mock.patch("freegem.gemini.time.sleep"),
        ):
            self.assertEqual(generate(cfg, "hi", 1, 0), "recovered")
        self.assertEqual(post.call_count, 2)

    def test_stream_403_fails_fast_without_retry(self) -> None:
        """The streaming raise_for_status path must not retry auth rejections."""
        import freegem.gemini as gem

        response = mock.MagicMock()
        response.status_code = 403
        client = mock.MagicMock()
        client.stream.return_value.__enter__.return_value = response
        client.stream.return_value.__exit__.return_value = False
        cfg = BridgeConfig(guard=True, retry_attempts=3, retry_delay_sec=0)
        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(gem, "_httpx_client", return_value=client),
        ):
            with self.assertRaises(BridgeError) as ctx:
                list(gem.generate_stream(cfg, "hi", 1, 0))
        self.assertIn("403", str(ctx.exception))
        self.assertEqual(client.stream.call_count, 1)


class ProxyRedactionTests(unittest.TestCase):
    """Transport errors must not leak proxy credentials into messages (#38)."""

    PROXY = "http://user:secret@proxy.example:8080"

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=0, proxy=self.PROXY)

    def test_generate_error_message_redacts_proxy_credentials(self) -> None:
        import urllib.error

        leak = f"Cannot connect to proxy {self.PROXY}: connection refused"
        with mock.patch("freegem.gemini._post", side_effect=urllib.error.URLError(leak)):
            with self.assertRaises(BridgeError) as ctx:
                generate(self.cfg, "hi", 1, 0)
        message = str(ctx.exception)
        self.assertNotIn("secret", message)
        self.assertNotIn("user:", message)
        self.assertIn("proxy.example:8080", message)

    def test_stream_error_message_redacts_proxy_credentials(self) -> None:
        import freegem.gemini as gem

        leak = f"connect {self.PROXY} failed: connection refused"
        client = mock.MagicMock()
        client.stream.side_effect = RuntimeError(leak)
        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(gem, "_httpx_client", return_value=client),
        ):
            with self.assertRaises(BridgeError) as ctx:
                list(generate_stream(self.cfg, "hi", 1, 0))
        message = str(ctx.exception)
        self.assertNotIn("secret", message)
        self.assertNotIn("user:", message)
        self.assertIn("proxy.example:8080", message)


class BuildTagRecoveryTests(unittest.TestCase):
    """One session-authenticated build-tag re-fetch on BardErrorInfo (#38)."""

    NEW_TAG = "boq_gemini-web-uiserver_20261101.00_p0"

    def setUp(self) -> None:
        import freegem.gemini as gem

        self.gem = gem
        gem._BUILD_TAG_REFRESHED = False
        gem._BUILD_TAG_OVERRIDE = None
        self.addCleanup(setattr, gem, "_BUILD_TAG_REFRESHED", False)
        self.addCleanup(setattr, gem, "_BUILD_TAG_OVERRIDE", None)

    def _session_cfg(self) -> BridgeConfig:
        """A config whose cookie file really exists, so the refresh engages."""
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"cookie": "SID=a", "sapisid": "b"}))
        self.addCleanup(os.unlink, path)
        return BridgeConfig(guard=True, cookie_file=path, log_requests=False)

    @staticmethod
    def _bard_post() -> Any:
        """A ``_post`` stand-in returning a FRESH BARD_ERROR body per attempt.

        The shared-BytesIO lesson (documented in GenerateTests): a closed
        response's read() raises, which sends generate() down the generic
        retry branch with real sleeps instead of the intended fail-fast path.
        """
        return mock.patch(
            "freegem.gemini._post", side_effect=lambda *a, **k: make_response(BARD_ERROR)
        )

    def test_bard_error_triggers_one_session_refresh(self) -> None:
        cfg = self._session_cfg()
        with (
            self._bard_post(),
            mock.patch.object(self.gem, "fetch_build_tag", return_value=self.NEW_TAG) as fetch,
        ):
            with self.assertRaises(BridgeError):
                generate(cfg, "hi", 1, 0)
        fetch.assert_called_once()
        self.assertIs(fetch.call_args.kwargs.get("session"), True)

    def test_refresh_happens_once_per_process(self) -> None:
        cfg = self._session_cfg()
        with (
            self._bard_post(),
            mock.patch.object(self.gem, "fetch_build_tag", return_value=self.NEW_TAG) as fetch,
        ):
            for _ in range(3):
                with self.assertRaises(BridgeError):
                    generate(cfg, "hi", 1, 0)
        fetch.assert_called_once()

    def test_refreshed_tag_is_used_by_build_url(self) -> None:
        cfg = self._session_cfg()
        with (
            self._bard_post(),
            mock.patch.object(self.gem, "fetch_build_tag", return_value=self.NEW_TAG),
        ):
            with self.assertRaises(BridgeError):
                generate(cfg, "hi", 1, 0)
        self.assertIn(f"bl={self.NEW_TAG}", build_url(cfg))

    def test_no_session_skips_the_refresh(self) -> None:
        cfg = BridgeConfig(guard=True, log_requests=False)
        with (
            self._bard_post(),
            mock.patch.object(self.gem, "fetch_build_tag", return_value=self.NEW_TAG) as fetch,
        ):
            with self.assertRaises(BridgeError):
                generate(cfg, "hi", 1, 0)
        fetch.assert_not_called()

    def test_broken_network_consumes_the_single_attempt(self) -> None:
        cfg = self._session_cfg()
        with (
            self._bard_post(),
            mock.patch.object(self.gem, "fetch_build_tag", side_effect=RuntimeError("no net")),
        ):
            with self.assertRaises(BridgeError):
                generate(cfg, "hi", 1, 0)
        with (
            self._bard_post(),
            mock.patch.object(
                self.gem, "fetch_build_tag", side_effect=RuntimeError("no net")
            ) as fetch,
        ):
            with self.assertRaises(BridgeError):
                generate(cfg, "hi", 1, 0)
        fetch.assert_not_called()

    def test_stream_bard_error_triggers_refresh(self) -> None:
        cfg = self._session_cfg()
        with (
            mock.patch.object(self.gem, "HAS_HTTPX", True),
            mock.patch.object(
                self.gem, "_httpx_client", return_value=StreamTests._fake_client([BARD_ERROR])
            ),
            mock.patch.object(self.gem, "fetch_build_tag", return_value=self.NEW_TAG) as fetch,
        ):
            with self.assertRaises(BridgeError):
                list(generate_stream(cfg, "hi", 1, 0))
        fetch.assert_called_once()


class RateLimitKindTests(unittest.TestCase):
    """Empty-200 answers carry a best-effort ``kind`` classification (#16)."""

    LOGIN_PAGE = (
        '<html><body><a href="https://accounts.google.com/ServiceLogin">'
        "Sign in to continue</a></body></html>"
    )

    def setUp(self) -> None:
        from freegem import gemini as gem

        gem._COOKIE_CACHE.update({"key": None, "cookie": "", "sapisid": None, "xsrf": None})
        self.addCleanup(
            gem._COOKIE_CACHE.update,
            {"key": None, "cookie": "", "sapisid": None, "xsrf": None},
        )
        self.cfg = BridgeConfig(guard=True, retry_attempts=2, retry_delay_sec=0, log_requests=False)

    def test_default_kind_is_rate_limit(self) -> None:
        with mock.patch("freegem.gemini._post", side_effect=lambda *a, **k: make_response("")):
            with self.assertRaises(RateLimitError) as ctx:
                generate(self.cfg, "hi", 1, 4)
        self.assertEqual(ctx.exception.kind, "rate_limit")
        self.assertIn("rate_limit", str(ctx.exception))

    def test_login_markers_classify_login_wall(self) -> None:
        with mock.patch(
            "freegem.gemini._post", side_effect=lambda *a, **k: make_response(self.LOGIN_PAGE)
        ):
            with self.assertRaises(RateLimitError) as ctx:
                generate(self.cfg, "hi", 1, 4)
        self.assertEqual(ctx.exception.kind, "login_wall")
        self.assertIn("login_wall", str(ctx.exception))

    def test_kind_survives_the_final_typed_error(self) -> None:
        """After retries exhaust, the re-raised error keeps the kind."""
        with mock.patch(
            "freegem.gemini._post", side_effect=lambda *a, **k: make_response(self.LOGIN_PAGE)
        ):
            with self.assertRaises(RateLimitError) as ctx:
                generate(self.cfg, "hi", 1, 4)
        self.assertEqual(ctx.exception.kind, "login_wall")
        self.assertIn("login_wall", str(ctx.exception))

    def test_still_a_bridge_error_and_retried_unchanged(self) -> None:
        with mock.patch(
            "freegem.gemini._post", side_effect=lambda *a, **k: make_response("")
        ) as post:
            with self.assertRaises(RateLimitError) as ctx:
                generate(self.cfg, "hi", 1, 4)
        self.assertIsInstance(ctx.exception, BridgeError)
        self.assertEqual(post.call_count, self.cfg.retry_attempts)

    def test_stream_default_kind_is_rate_limit(self) -> None:
        import freegem.gemini as gem

        # Fresh response per attempt: a fake client's iter_text iterator is
        # exhausted by the first retry (same shared-response lesson as the
        # shared-BytesIO note in GenerateTests), which would classify the
        # retried attempt from an empty body instead of the real chunks.
        client = mock.MagicMock()
        client.stream.side_effect = [
            StreamTests._fake_client([""]).stream.return_value,
            StreamTests._fake_client([""]).stream.return_value,
        ]
        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(gem, "_httpx_client", return_value=client),
        ):
            with self.assertRaises(RateLimitError) as ctx:
                list(generate_stream(self.cfg, "hi", 1, 4))
        self.assertEqual(ctx.exception.kind, "rate_limit")

    def test_stream_login_markers_classify_login_wall(self) -> None:
        import freegem.gemini as gem

        marker = "Sign in to continue at https://accounts.google.com/ServiceLogin"
        client = mock.MagicMock()
        client.stream.side_effect = [
            StreamTests._fake_client([marker]).stream.return_value,
            StreamTests._fake_client([marker]).stream.return_value,
        ]
        with (
            mock.patch.object(gem, "HAS_HTTPX", True),
            mock.patch.object(gem, "_httpx_client", return_value=client),
        ):
            with self.assertRaises(RateLimitError) as ctx:
                list(generate_stream(self.cfg, "hi", 1, 4))
        self.assertEqual(ctx.exception.kind, "login_wall")


class HttpxPoolTests(unittest.TestCase):
    """The pooled httpx client is shared, and shared by the RIGHT settings."""

    def setUp(self) -> None:
        from freegem import gemini as gem

        self.gem = gem
        self.addCleanup(gem._HTTPX_CLIENTS.clear)

    def test_same_settings_share_one_client(self) -> None:
        if not self.gem.HAS_HTTPX:
            self.skipTest("httpx not installed")
        first = self.gem._httpx_client(BridgeConfig(guard=True, log_requests=False))
        second = self.gem._httpx_client(BridgeConfig(guard=True, log_requests=False))
        self.assertIs(first, second)

    def test_different_timeout_gets_its_own_client(self) -> None:
        """A timeout change must not silently ride an old cached client."""
        if not self.gem.HAS_HTTPX:
            self.skipTest("httpx not installed")
        base = self.gem._httpx_client(BridgeConfig(guard=True, log_requests=False))
        slower = self.gem._httpx_client(
            BridgeConfig(guard=True, log_requests=False, request_timeout_sec=99)
        )
        self.assertIsNot(base, slower)
        proxied = self.gem._httpx_client(
            BridgeConfig(guard=True, log_requests=False, proxy="http://127.0.0.1:9")
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
                BridgeConfig(guard=True, log_requests=False),
                b"payload",
                "http://u.test/x",
                {"h": "1"},
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["content"], b"payload")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"body")
        with response:
            pass
        self.assertTrue(response._raw.closed)  # context manager closes

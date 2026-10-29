"""Tests for the image-upload path and its credential-leak guard.

The resumable upload posts image bytes *with the session cookie* to a URL taken
from a response header. A hostile or compromised HTTP proxy — and `proxy` is a
supported configuration — can answer that header with any host, so the host must
be constrained before anything credential-bearing is sent to it. These tests pin
that guard, because a regression here silently leaks the Google session.
"""

import unittest
import urllib.parse
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.multimodal import (
    _validate_upload_url,
    detect_image_mime,
    fetch_image_bytes,
    reset_page_tokens,
    upload_image,
)


class UploadUrlValidationTests(unittest.TestCase):
    """The host allowlist that keeps the cookie on Google infrastructure."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def test_googleapis_upload_host_accepted(self) -> None:
        url = "https://content-push.googleapis.com/upload/session-123"
        self.assertEqual(_validate_upload_url(self.cfg, url), url)

    def test_alternate_google_host_accepted(self) -> None:
        url = "https://upload.googleapis.com/upload/session-123"
        self.assertEqual(_validate_upload_url(self.cfg, url), url)

    def test_plain_http_refused(self) -> None:
        """Downgrading to http would expose the cookie on the wire."""
        with self.assertRaises(BridgeError) as ctx:
            _validate_upload_url(self.cfg, "http://content-push.googleapis.com/x")
        self.assertIn("non-HTTPS", str(ctx.exception))

    def test_foreign_host_refused(self) -> None:
        with self.assertRaises(BridgeError) as ctx:
            _validate_upload_url(self.cfg, "https://evil.example.com/steal")
        self.assertIn("unexpected host", str(ctx.exception))

    def test_suffix_spoof_refused(self) -> None:
        """`content-push.googleapis.com.evil.com` must not pass a contains-check."""
        with self.assertRaises(BridgeError):
            _validate_upload_url(self.cfg, "https://content-push.googleapis.com.evil.com/x")

    def test_userinfo_spoof_refused(self) -> None:
        """`https://content-push.googleapis.com@evil.com/` — real host is evil.com."""
        with self.assertRaises(BridgeError):
            _validate_upload_url(self.cfg, "https://content-push.googleapis.com@evil.com/x")

    def test_non_http_scheme_refused(self) -> None:
        """Only https reaches the host check; every other scheme dies first."""
        for scheme in ("ftp", "file", "gopher", "data", "http"):
            with self.subTest(scheme=scheme):
                with self.assertRaises(BridgeError):
                    _validate_upload_url(self.cfg, f"{scheme}://content-push.googleapis.com/x")

    def test_unparseable_url_refused(self) -> None:
        with self.assertRaises(BridgeError):
            _validate_upload_url(self.cfg, "https://[not-a-url")

    def test_empty_host_refused(self) -> None:
        with self.assertRaises(BridgeError):
            _validate_upload_url(self.cfg, "https:///x")


class MimeDetectionTests(unittest.TestCase):
    def test_png_signature(self) -> None:
        self.assertEqual(detect_image_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 20), "image/png")

    def test_jpeg_signature(self) -> None:
        self.assertEqual(detect_image_mime(b"\xff\xd8\xff\xe0" + b"x" * 20), "image/jpeg")

    def test_gif_both_variants(self) -> None:
        for magic in (b"GIF87a", b"GIF89a"):
            self.assertEqual(detect_image_mime(magic + b"x" * 20), "image/gif")

    def test_webp_riff_container(self) -> None:
        data = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"x" * 10
        self.assertEqual(detect_image_mime(data), "image/webp")

    def test_riff_but_not_webp_falls_back(self) -> None:
        data = b"RIFF" + b"\x00\x00\x00\x00" + b"AVI " + b"x" * 10
        self.assertEqual(
            detect_image_mime(data, "application/octet-stream"), "application/octet-stream"
        )

    def test_heic_ftyp_brand(self) -> None:
        data = b"\x00\x00\x00\x18" + b"ftyp" + b"heic" + b"x" * 10
        self.assertEqual(detect_image_mime(data), "image/heic")

    def test_avif_ftyp_brand(self) -> None:
        data = b"\x00\x00\x00\x18" + b"ftyp" + b"avif" + b"x" * 10
        self.assertEqual(detect_image_mime(data), "image/avif")

    def test_unknown_falls_back_to_argument(self) -> None:
        self.assertEqual(detect_image_mime(b"nonsense-bytes", "image/png"), "image/png")

    def test_non_bytes_falls_back(self) -> None:
        # deliberately wrong type: the guard must not depend on the caller
        self.assertEqual(detect_image_mime("not bytes", "image/png"), "image/png")  # type: ignore[arg-type]

    def test_empty_input_falls_back(self) -> None:
        self.assertEqual(detect_image_mime(b"", "image/gif"), "image/gif")


class FetchImageBytesTests(unittest.TestCase):
    """Remote image fetching must not carry the session cookie."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def test_non_http_scheme_refused_without_request(self) -> None:
        for scheme in ("file", "ftp", "data", ""):
            with (
                self.subTest(scheme=scheme),
                mock.patch("freegem.multimodal.urllib.request.urlopen") as urlopen,
            ):
                self.assertEqual(fetch_image_bytes(self.cfg, f"{scheme}://x/y"), b"")
                urlopen.assert_not_called()

    def test_no_credential_headers_on_fetch(self) -> None:
        """A user-supplied image URL must never receive the session cookie."""
        opener = mock.MagicMock()
        # fetch_image_bytes streams the body in 1 MiB blocks and stops at the
        # first empty read; a fixed return_value here never goes empty and the
        # loop only exits at the 25 MB cap (~2.9M recorded mock calls -> OOM).
        opener.open.return_value.read.side_effect = [b"imagedata", b""]
        with (
            mock.patch("freegem.multimodal._host_resolves_public", return_value=True),
            # (#63) fetch_image_bytes dials through the hand-built guarded
            # opener; the seam is build_guarded_opener itself now.
            mock.patch("freegem.multimodal.build_guarded_opener", return_value=opener),
        ):
            result = fetch_image_bytes(self.cfg, "https://example.com/pic.png")
        self.assertEqual(result, b"imagedata")

        request = opener.open.call_args[0][0]
        header_names = {k.lower() for k in request.headers}
        self.assertNotIn("cookie", header_names)
        self.assertNotIn("authorization", header_names)
        self.assertIn("user-agent", header_names)

    def test_network_failure_returns_empty_not_raise(self) -> None:
        opener = mock.MagicMock()
        opener.open.side_effect = OSError("connection refused")
        with (
            mock.patch("freegem.multimodal._host_resolves_public", return_value=True),
            mock.patch("freegem.multimodal.urllib.request.build_opener", return_value=opener),
        ):
            self.assertEqual(fetch_image_bytes(self.cfg, "https://example.com/x.png"), b"")

    def test_private_hosts_refused_before_any_request(self) -> None:
        """SSRF guard: loopback/private/metadata targets never leave the host."""
        from freegem.multimodal import _host_resolves_public

        with mock.patch(
            "freegem.multimodal.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 0))],
        ):
            self.assertFalse(_host_resolves_public("internal.service"))
        with mock.patch(
            "freegem.multimodal.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("169.254.169.254", 0))],
        ):
            self.assertFalse(_host_resolves_public("metadata.google.internal"))
        with mock.patch("freegem.multimodal.socket.getaddrinfo", side_effect=OSError):
            self.assertFalse(_host_resolves_public("nonexistent.example"))

        with mock.patch("freegem.multimodal._host_resolves_public", return_value=False):
            with mock.patch("freegem.multimodal.urllib.request.build_opener") as build_opener:
                self.assertEqual(fetch_image_bytes(self.cfg, "http://192.168.1.1/admin"), b"")
                build_opener.assert_not_called()

    def test_credentialed_upload_refuses_redirects(self) -> None:
        """_NoRedirect must raise, not follow: urllib copies headers to hops."""
        from freegem.multimodal import _NoRedirect

        handler = _NoRedirect()
        with self.assertRaises(BridgeError):
            handler.redirect_request(
                mock.MagicMock(), None, 302, "Found", {}, "https://evil.example.com/x"
            )


class UploadFlowTests(unittest.TestCase):
    """The two-step Scotty handshake, including the host guard at step two."""

    def setUp(self) -> None:
        reset_page_tokens()
        self.cfg = BridgeConfig(guard=True, log_requests=False, cookie_file=None)

    def _response(
        self,
        headers: dict[str, str] | None = None,
        body: bytes = b"{}",
        url: str = "https://content-push.googleapis.com/upload/s1",
    ) -> mock.MagicMock:
        """A urlopen return value that behaves like a real HTTP response.

        ``headers`` is a real dict, because ``MagicMock().headers.get()``
        returns a truthy MagicMock rather than None — which would let a bogus
        upload URL past the "no upload URL" guard and into urlparse. ``full_url``
        is set explicitly because the guard parses it to decide which host a
        credential would be sent to.
        """
        resp = mock.MagicMock()
        resp.headers = dict(headers or {})
        resp.read.return_value = body
        resp.full_url = url
        return resp

    def _patch(
        self,
        urlopen_side_effect: object,
        cookie: tuple[str, str | None] = ("", None),
    ) -> tuple[AbstractContextManager[Any], ...]:
        """Patch the three boundaries upload_image touches.

        ``urlopen_side_effect`` may be a list (consumed in order) or a callable.
        """

        # upload_image builds an opener (redirects must be refused outright for
        # credentialed requests), so the seam is build_opener, not urlopen.
        def _opener_factory(*args: object, **kwargs: object) -> mock.MagicMock:
            opener = mock.MagicMock()
            opener.open.side_effect = urlopen_side_effect
            return opener

        return (
            mock.patch("freegem.multimodal._page_tokens", return_value={}),
            mock.patch("freegem.multimodal.load_cookie", return_value=cookie),
            mock.patch("freegem.multimodal.urllib.request.build_opener", new=_opener_factory),
        )

    @staticmethod
    def _passthrough(
        response: mock.MagicMock,
    ) -> Callable[..., mock.MagicMock]:
        """A side_effect callable that returns a fresh response each call."""
        return lambda *a, **kw: response

    def test_happy_path(self) -> None:
        p_tokens, p_cookie, p_urlopen = self._patch(
            [
                self._response(
                    {"X-Goog-Upload-URL": "https://content-push.googleapis.com/upload/s1"}
                ),
                self._response(body=b"/ref/uploaded-file"),
            ]
        )
        with p_tokens, p_cookie, p_urlopen:
            ref = upload_image(self.cfg, b"pngbytes", "image/png")
        self.assertEqual(ref, "/ref/uploaded-file")

    def test_hostile_upload_url_refused_before_sending_cookie(self) -> None:
        """If step one answers with a foreign host, step two must never run."""
        seen = []

        def fake_urlopen(request: mock.MagicMock, *a: object, **kw: object) -> mock.MagicMock:
            seen.append(request.full_url)
            if len(seen) == 1:
                return self._response({"X-Goog-Upload-URL": "https://evil.example.com/x"})
            return self._response(body=b"/should-not-happen")

        p_tokens, p_cookie, p_urlopen = self._patch(fake_urlopen, cookie=("SID=secret", "SAP"))
        with p_tokens, p_cookie, p_urlopen:
            with self.assertRaises(BridgeError) as ctx:
                upload_image(self.cfg, b"pngbytes", "image/png")

        self.assertIn("unexpected host", str(ctx.exception))
        self.assertEqual(len(seen), 1, "must not contact the hostile host")
        self.assertNotIn("evil.example.com", " ".join(seen))

    def test_cookie_never_sent_to_foreign_host(self) -> None:
        """The credential must not leave Google infrastructure.

        Step one legitimately sends the cookie to content-push.googleapis.com.
        The guard's job is to stop step two, which would send it wherever the
        response header pointed. So assert per-host, not globally.
        """
        sent = []  # (host, headers)

        def fake_urlopen(request: mock.MagicMock, *a: object, **kw: object) -> mock.MagicMock:
            host = urllib.parse.urlparse(request.full_url).hostname or ""
            sent.append((host, dict(request.headers)))
            if len(sent) == 1:
                return self._response({"X-Goog-Upload-URL": "https://evil.example.com/x"})
            return self._response(body=b"/x")

        p_tokens, p_cookie, p_urlopen = self._patch(
            fake_urlopen, cookie=("SID=realsecret", "SAPISIDVALUE")
        )
        with p_tokens, p_cookie, p_urlopen:
            with self.assertRaises(BridgeError):
                upload_image(self.cfg, b"x", "image/png")

        self.assertEqual(len(sent), 1, "step two must never run")
        host, headers = sent[0]
        self.assertEqual(host, "content-push.googleapis.com")
        # and the hostile host was never contacted at all
        self.assertNotIn("evil.example.com", [h for h, _ in sent])
        _ = headers  # cookie present here is correct and expected

    def test_missing_upload_header_raises(self) -> None:
        p_tokens, p_cookie, p_urlopen = self._patch(self._passthrough(self._response(headers={})))
        with p_tokens, p_cookie, p_urlopen:
            with self.assertRaises(BridgeError) as ctx:
                upload_image(self.cfg, b"x", "image/png")
        self.assertIn("no upload URL", str(ctx.exception))

    def test_bad_file_ref_rejected(self) -> None:
        """A reference that is not a path cannot be embedded in the payload."""
        p_tokens, p_cookie, p_urlopen = self._patch(
            [
                self._response(
                    {"X-Goog-Upload-URL": "https://content-push.googleapis.com/upload/s1"}
                ),
                self._response(body=b"not-a-path"),
            ]
        )
        with p_tokens, p_cookie, p_urlopen:
            with self.assertRaises(BridgeError) as ctx:
                upload_image(self.cfg, b"x", "image/png")
        self.assertIn("unusable reference", str(ctx.exception))

    def test_page_tokens_cached(self) -> None:
        """A hot loop must not re-scrape the page on every image."""
        from freegem import multimodal

        reset_page_tokens()
        html = b"<html>var a = 'feeds/token123'; var b = 'CgcSBWjK7pYx';</html>"

        def fake_urlopen(request: mock.MagicMock, *a: object, **kw: object) -> mock.MagicMock:
            resp = mock.MagicMock()
            resp.read.return_value = html
            return resp

        with mock.patch(
            "freegem.multimodal.urllib.request.urlopen", side_effect=fake_urlopen
        ) as urlopen:
            first = multimodal._page_tokens(self.cfg)
            second = multimodal._page_tokens(self.cfg)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(first, second)

    def test_page_token_failure_does_not_refetch_every_call(self) -> None:
        """Regression: a failed fetch used to leave the cache unsatisfied.

        The old guard was `if ts fresh and tokens`, so an empty result was
        falsy and every subsequent image re-scraped the page -- an extra
        authenticated request per image precisely when the network is failing.
        """
        from freegem import multimodal

        reset_page_tokens()
        with mock.patch(
            "freegem.multimodal.urllib.request.urlopen",
            side_effect=OSError("no network"),
        ) as urlopen:
            first = multimodal._page_tokens(self.cfg)
            second = multimodal._page_tokens(self.cfg)
            third = multimodal._page_tokens(self.cfg)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(first, {})
        self.assertEqual(second, {})
        self.assertEqual(third, {})

        # A failure must not be pinned for the full TTL: once the 15s backoff
        # has passed, the next call retries for real.
        multimodal._PAGE_TOKENS["ts"] -= 16
        with mock.patch(
            "freegem.multimodal.urllib.request.urlopen",
            side_effect=OSError("still down"),
        ) as urlopen2:
            multimodal._page_tokens(self.cfg)
        self.assertEqual(urlopen2.call_count, 1)


class DecodeDataUrlTests(unittest.TestCase):
    """data: URLs as model output actually writes them."""

    def test_plain_base64(self) -> None:
        from freegem.multimodal import decode_data_url

        self.assertEqual(decode_data_url("data:image/png;base64,aGVsbG8="), (b"hello", "image/png"))

    def test_line_wrapped_base64_is_decoded(self) -> None:
        """Models wrap base64 at 76 chars; validate=True rejects whitespace,
        so the decoder must strip it first."""
        from freegem.multimodal import decode_data_url

        self.assertEqual(
            decode_data_url("data:image/png;base64,aGVs\nbG8="), (b"hello", "image/png")
        )

    def test_extra_header_parameters_are_tolerated(self) -> None:
        from freegem.multimodal import decode_data_url

        decoded = decode_data_url("data:image/jpeg;charset=utf-8;base64,aGVsbG8=")
        self.assertEqual(decoded, (b"hello", "image/jpeg"))

    def test_url_encoded_payload(self) -> None:
        from freegem.multimodal import decode_data_url

        self.assertEqual(decode_data_url("data:,hello%20world"), (b"hello world", "image/png"))

    def test_garbage_is_none(self) -> None:
        from freegem.multimodal import decode_data_url

        self.assertIsNone(decode_data_url("not a data url"))
        self.assertIsNone(decode_data_url("data:image/png;base64,%%%"))  # invalid alphabet
        self.assertIsNone(decode_data_url("data:image/png;base64"))  # no comma


class ProxyLogRedactionTests(unittest.TestCase):
    """Failure logs must not leak proxy credentials (#38, log-line variant)."""

    PROXY = "http://user:secret@proxy.example:8080"

    def test_fetch_image_failure_log_redacts_proxy(self) -> None:
        import io
        from contextlib import redirect_stderr

        cfg = BridgeConfig(guard=True, log_requests=True, proxy=self.PROXY)
        leak = f"Cannot connect to proxy {self.PROXY}: connection refused"
        opener = mock.MagicMock()
        opener.open.side_effect = OSError(leak)
        err = io.StringIO()
        with (
            mock.patch("freegem.multimodal._host_resolves_public", return_value=True),
            mock.patch("freegem.multimodal.build_guarded_opener", return_value=opener),
            redirect_stderr(err),
        ):
            self.assertEqual(fetch_image_bytes(cfg, "https://example.com/x.png"), b"")
        self.assertIn("image fetch failed", err.getvalue())
        self.assertNotIn("secret", err.getvalue())
        self.assertNotIn("user:", err.getvalue())

    def test_page_token_failure_log_redacts_proxy(self) -> None:
        import io
        from contextlib import redirect_stderr

        from freegem import multimodal

        reset_page_tokens()
        cfg = BridgeConfig(guard=True, log_requests=True, proxy=self.PROXY)
        leak = f"Cannot connect to proxy {self.PROXY}: connection refused"
        err = io.StringIO()
        with (
            mock.patch(
                "freegem.multimodal.urllib.request.build_opener",
                side_effect=OSError(leak),
            ),
            redirect_stderr(err),
        ):
            self.assertEqual(multimodal._page_tokens(cfg), {})
        self.assertIn("upload-token fetch failed", err.getvalue())
        self.assertNotIn("secret", err.getvalue())
        self.assertNotIn("user:", err.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Sec-Fetch-Site: cross-site POST gate, loopback-aware.

Fetch Metadata gives the server a third, independent signal that a POST was
born on another site -- and unlike ``Origin`` it cannot be withheld or forged
by page scripts. These tests pin the gate:

* ``Sec-Fetch-Site: cross-site`` with a **loopback** ``Origin`` passes -- a
  local UI on another port of this machine (``localhost:3000`` -> this
  server) is cross-site by origin rules and is the documented
  friend-of-the-operator case;
* ``cross-site`` with a foreign, ``null``, or lookalike Origin is a 403
  before generate() is reached;
* ``cross-site`` with **no** Origin at all is a 403 too: the header is
  browser-sworn (a forbidden header name) and a browser always sends Origin
  on a cross-site POST, so its absence is not a normal client shape;
* the other legal values (``same-origin``, ``same-site``, ``none``) pass;
* an absent ``Sec-Fetch-Site`` header (curl, SDKs, tests) passes --
  non-browser clients never send Fetch Metadata.
"""

from __future__ import annotations

from typing import Any, ClassVar
from unittest import mock

from freegem.tests.test_server import EndpointTestCase


class SecFetchSiteTests(EndpointTestCase):
    BODY: ClassVar[dict[str, Any]] = {
        "model": "gemini-3.6-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }

    @mock.patch("freegem.server.generate")
    def test_cross_site_with_loopback_origin_passes(self, generate: Any) -> None:
        """A local UI on another loopback port is cross-site but welcome."""
        generate.return_value = "ok"
        for origin in (
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://[::1]:3000",
        ):
            with self.subTest(origin=origin):
                generate.reset_mock()
                status, _, _ = self.post(
                    "/v1/chat/completions",
                    self.BODY,
                    headers={
                        "Sec-Fetch-Site": "cross-site",
                        "Origin": origin,
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(status, 200)
                generate.assert_called_once()

    @mock.patch("freegem.server.generate")
    def test_cross_site_with_foreign_origin_403(self, generate: Any) -> None:
        """A browser swearing cross-site from a foreign origin is refused."""
        status, _, raw = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-site", json_message(raw))
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_cross_site_with_null_origin_403(self, generate: Any) -> None:
        """``Origin: null`` (sandboxed iframes, redirected posts) is refused."""
        status, _, _raw = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Origin": "null",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status, 403)
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_cross_site_with_lookalike_origin_403(self, generate: Any) -> None:
        """A lookalike hostname is not loopback just because it starts alike."""
        status, _, _raw = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Origin": "http://localhost.evil.example",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status, 403)
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_cross_site_header_403_without_origin(self, generate: Any) -> None:
        """Browser-sworn cross-site with no Origin is an abnormal client."""
        status, _, _raw = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_header_is_case_insensitive(self, generate: Any) -> None:
        status, _, _ = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={"Sec-Fetch-Site": "Cross-Site", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_same_origin_and_none_pass(self, generate: Any) -> None:
        for value in ("same-origin", "same-site", "none"):
            with self.subTest(value=value):
                generate.reset_mock()
                generate.return_value = "ok"
                status, _, _ = self.post(
                    "/v1/chat/completions",
                    self.BODY,
                    headers={
                        "Sec-Fetch-Site": value,
                        "Origin": "http://127.0.0.1",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(status, 200)
                generate.assert_called_once()

    @mock.patch("freegem.server.generate")
    def test_absent_header_still_passes(self, generate: Any) -> None:
        """curl/SDK clients never send Fetch Metadata; they keep working."""
        generate.return_value = "ok"
        status, _, _ = self.post("/v1/chat/completions", self.BODY)
        self.assertEqual(status, 200)

    @mock.patch("freegem.server.generate")
    def test_cross_site_header_checked_on_non_chat_paths_too(self, generate: Any) -> None:
        status, _, _ = self.post(
            "/v1/responses",
            {"model": "gemini-3.6-flash", "input": "hi"},
            headers={"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        generate.assert_not_called()


def json_message(raw: str | bytes) -> str:
    import json

    return str(json.loads(raw)["error"]["message"])

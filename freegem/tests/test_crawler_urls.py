"""URL normalization and scope rules."""

from __future__ import annotations

import unittest

from freegem.crawler.urls import (
    Budget,
    Scope,
    normalize_url,
    registered_domain,
    strip_tracking_params,
    url_origin,
)


class NormalizeTests(unittest.TestCase):
    def test_scheme_and_host_lowercased(self) -> None:
        self.assertEqual(normalize_url("HTTP://EXAMPLE.COM/A"), "http://example.com/A")

    def test_default_port_dropped(self) -> None:
        self.assertEqual(normalize_url("https://example.com:443/x"), "https://example.com/x")
        self.assertEqual(normalize_url("http://example.com:80/"), "http://example.com/")
        self.assertEqual(normalize_url("http://example.com:8080/x"), "http://example.com:8080/x")

    def test_fragment_dropped(self) -> None:
        self.assertEqual(normalize_url("https://example.com/x#section"), "https://example.com/x")

    def test_empty_path_becomes_slash(self) -> None:
        self.assertEqual(normalize_url("https://example.com"), "https://example.com/")

    def test_tracking_params_removed(self) -> None:
        out = normalize_url("https://example.com/x?utm_source=feed&id=7&fbclid=abc")
        self.assertEqual(out, "https://example.com/x?id=7")
        self.assertEqual(strip_tracking_params("a=1&utmcampaign=x"), "a=1&utmcampaign=x")
        self.assertEqual(strip_tracking_params("a=1&utm_campaign=x"), "a=1")

    def test_query_order_preserved(self) -> None:
        self.assertEqual(
            normalize_url("https://example.com/?b=2&a=1"), "https://example.com/?b=2&a=1"
        )

    def test_percent_encoding_normalized(self) -> None:
        self.assertEqual(
            normalize_url("https://example.com/a%2fb%2F"), "https://example.com/a%2Fb%2F"
        )
        self.assertEqual(normalize_url("https://example.com/%7Euser"), "https://example.com/~user")

    def test_relative_resolution(self) -> None:
        self.assertEqual(
            normalize_url("../img/x.png", base="https://example.com/a/b/c.html"),
            "https://example.com/a/img/x.png",
        )

    def test_non_http_schemes_refused(self) -> None:
        for url in ("mailto:a@b.c", "javascript:x()", "ftp://h/x", "data:text/html,x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_embedded_credentials_refused(self) -> None:
        with self.assertRaises(ValueError):
            normalize_url("https://user:pass@example.com/")

    def test_idn_host_punycoded(self) -> None:
        self.assertEqual(normalize_url("https://ñandu.example/x"), "https://xn--andu-fqa.example/x")

    def test_whitespace_stripped(self) -> None:
        self.assertEqual(normalize_url("  https://example.com/x \n"), "https://example.com/x")


class ScopeTests(unittest.TestCase):
    def test_same_host_default_admits_seed_host(self) -> None:
        scope = Scope()
        verdict = scope.admits("https://example.com/x", depth=0, seed_hosts=["example.com"])
        self.assertTrue(verdict.ok)

    def test_same_host_default_refuses_off_site(self) -> None:
        scope = Scope()
        verdict = scope.admits("https://other.org/x", depth=0, seed_hosts=["example.com"])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "out_of_scope_host")

    def test_allow_domain_suffix_match(self) -> None:
        scope = Scope(allow_domains=("example.org",))
        self.assertTrue(scope.admits("https://www.example.org/x", depth=0, seed_hosts=[]).ok)
        self.assertFalse(scope.admits("https://notexample.org/x", depth=0, seed_hosts=[]).ok)

    def test_deny_domain_wins_over_allow(self) -> None:
        scope = Scope(allow_domains=("example.org",), deny_domains=("secret.example.org",))
        self.assertFalse(scope.admits("https://secret.example.org/x", depth=0, seed_hosts=[]).ok)
        self.assertEqual(
            scope.admits("https://secret.example.org/x", depth=0, seed_hosts=[]).reason,
            "deny_domain",
        )

    def test_path_prefixes(self) -> None:
        scope = Scope(allow_paths=("/docs",))
        self.assertTrue(scope.admits("https://e.com/docs/a", depth=0, seed_hosts=["e.com"]).ok)
        self.assertFalse(scope.admits("https://e.com/documents", depth=0, seed_hosts=["e.com"]).ok)
        self.assertEqual(
            scope.admits("https://e.com/other", depth=0, seed_hosts=["e.com"]).reason,
            "out_of_scope_path",
        )

    def test_deny_path_wins(self) -> None:
        scope = Scope(allow_paths=("/docs",), deny_paths=("/docs/private",))
        self.assertFalse(
            scope.admits("https://e.com/docs/private/x", depth=0, seed_hosts=["e.com"]).ok
        )

    def test_depth_budget(self) -> None:
        scope = Scope(max_depth=1)
        self.assertTrue(scope.admits("https://e.com/x", depth=1, seed_hosts=["e.com"]).ok)
        verdict = scope.admits("https://e.com/x", depth=2, seed_hosts=["e.com"])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "too_deep")

    def test_malformed_url(self) -> None:
        verdict = Scope().admits("not a url", depth=0, seed_hosts=[])
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "malformed_url")


class BudgetTests(unittest.TestCase):
    def test_spending_down(self) -> None:
        budget = Budget(max_pages=3)
        self.assertFalse(budget.exhausted)
        budget.fetched += 1
        budget.failed += 2
        self.assertEqual(budget.spent, 3)
        self.assertEqual(budget.remaining, 0)
        self.assertTrue(budget.exhausted)

    def test_resume_credit(self) -> None:
        budget = Budget(max_pages=10, fetched_before=7)
        self.assertEqual(budget.remaining, 3)


class HelpersTests(unittest.TestCase):
    def test_url_origin(self) -> None:
        self.assertEqual(url_origin("https://example.com:443/a"), "https://example.com")

    def test_registered_domain(self) -> None:
        self.assertEqual(registered_domain("www.example.org"), "example.org")
        self.assertEqual(registered_domain("example.org"), "example.org")
        self.assertEqual(registered_domain("a.b.example.co.uk"), "co.uk")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""The 1000-site wall corpus: scale check for the smart-fetch ladder + AI reads.

The ladder's unit tests pin single behaviors; this module runs the whole
corpus those behaviors were shaped for — one thousand synthetic hosts
across the wall shapes real scraping meets (naive UA walls, reader-friendly
sites, challenge interstitials, archive-only pages, AI-only reads, honest
dead ends, and our own SSRF refusals) — through the REAL ``fallback_scrape``
walk. Every fetch and model seam is faked, so the corpus is deterministic:
per-category winners, attempt counts, AI-rung usage, and the strategy
memory's second-pass savings are exact numbers, not samples. The ai-clean
round sweeps another two hundred boilerplate-heavy pages through the real
gating (size floors, bounded input, honest refusals) with a fake provider.
"""

from __future__ import annotations

import unittest
from typing import Any, ClassVar
from unittest import mock

from freegem.config import BridgeConfig
from freegem.smart_fetch import (
    _AI_CLEAN_MAX_INPUT_CHARS,
    _AI_CLEAN_MIN_CHARS,
    StrategyMemory,
    ai_clean,
    clear_memory,
    default_memory_path,
    fallback_scrape,
)
from freegem.web_scraper import ScrapeError, ScrapeResult

# ─── the corpus: 1000 sites, 8 wall shapes, exact proportions ───────────────

#: The scripted outcome per (category, rung) for the default config, where
#: browser-render, tls-browser and flaresolverr are gated OFF.
_OUTCOMES: dict[str, dict[str, str]] = {
    "ua-wall": {"browser-headers": "win"},
    "reader-friendly": {"reader": "win"},
    "challenge-wall": {"browser-headers": "challenge", "ai-search": "win"},
    "wayback-only": {"wayback": "win"},
    "type-refusal": {"reader": "win"},
    "ai-only": {"browser-headers": "challenge", "ai-search": "win"},
    "hard-dead": {},
}

#: 200+200+150+150+100+100+50+50 = 1000 sites.
_CATEGORY_COUNTS: dict[str, int] = {
    "ua-wall": 200,
    "reader-friendly": 200,
    "challenge-wall": 150,
    "wayback-only": 150,
    "type-refusal": 100,
    "ai-only": 100,
    "hard-dead": 50,
    "ssrf-refused": 50,
}

_CONTENT_TEXT = "The article body, readable prose sentence number one, twice. " * 40
_CHALLENGE_TEXT = (
    "Just a moment...\nVerify you are human by completing the challenge. "
    "Attention Required! | Cloudflare   access, please wait."
)


def _sites() -> list[tuple[str, str, str]]:
    """(url, category, direct-first-error) for all 1000 sites, deterministically."""
    sites: list[tuple[str, str, str]] = []
    for category, count in _CATEGORY_COUNTS.items():
        for i in range(count):
            host = f"{category}-{i:03d}.test"
            url = f"https://{host}/page.html"
            if category == "ssrf-refused":
                first_error = f"SSRF guard refused '{url}': loopback host"
            else:
                first_error = f"HTTP 403 fetching '{url}'"
            sites.append((url, category, first_error))
    return sites


_CATEGORY_BY_URL: dict[str, str] = {url: category for url, category, _ in _sites()}

#: First pass: rungs walked per category before a win or an honest failure.
#: ua-wall 1, reader-friendly 2, challenge-wall 4, wayback-only 3,
#: type-refusal 2, ai-only 4, hard-dead 4, ssrf-refused 0.
_FIRST_PASS_ATTEMPTS = 200 * 1 + 200 * 2 + 150 * 4 + 150 * 3 + 100 * 2 + 100 * 4 + 50 * 4
#: Second pass: every remembered winner is tried first and wins immediately;
#: only the dead sites walk all four rungs again.
_SECOND_PASS_ATTEMPTS = 200 + 200 + 150 + 150 + 100 + 100 + 50 * 4


class _WallCorpus:
    """Fake ``_try_strategy``: scripted outcomes per (category, rung)."""

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str]] = []
        self.ai_rung_attempts = 0

    def try_strategy(
        self,
        cfg: Any,
        url: str,
        strategy: str,
        max_chars: int,
        ignore_robots: bool,
        budget: float | None = None,
    ) -> ScrapeResult:
        self.attempts.append((url, strategy))
        if strategy == "ai-search":
            self.ai_rung_attempts += 1
        outcome = _OUTCOMES[_CATEGORY_BY_URL[url]].get(strategy, "fail")
        if outcome == "win":
            return ScrapeResult(url=url, title="T", text=_CONTENT_TEXT, via=strategy)
        if outcome == "challenge":
            return ScrapeResult(url=url, title="Just a moment", text=_CHALLENGE_TEXT)
        raise ScrapeError(f"HTTP 403 fetching {url!r}")


class WallCorpusTests(unittest.TestCase):
    """fallback_scrape over the full corpus: winners, economy, memory, honesty."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)
        clear_memory()

    def tearDown(self) -> None:
        clear_memory()

    def _walk(self, corpus: _WallCorpus) -> dict[str, str | None]:
        """One pass over all 1000 sites; url -> winning rung (None = failed)."""
        winners: dict[str, str | None] = {}
        with (
            mock.patch("freegem.smart_fetch._try_strategy", side_effect=corpus.try_strategy),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            for url, _category, first_error in _sites():
                try:
                    result = fallback_scrape(
                        self.cfg,
                        url,
                        max_chars=2000,
                        first_error=first_error,
                    )
                except ScrapeError:
                    winners[url] = None
                else:
                    winners[url] = result.via
        return winners

    @staticmethod
    def _expected_winner(category: str) -> str | None:
        for rung, outcome in _OUTCOMES.get(category, {}).items():
            if outcome == "win":
                return rung
        return None

    def test_corpus_shape_is_exactly_one_thousand_sites(self) -> None:
        sites = _sites()
        self.assertEqual(len(sites), 1000)
        self.assertEqual(len({url for url, _, _ in sites}), 1000, "hosts must be unique")
        by_category: dict[str, int] = {}
        for _url, category, _err in sites:
            by_category[category] = by_category.get(category, 0) + 1
        self.assertEqual(by_category, _CATEGORY_COUNTS)

    def test_first_pass_winners_rate_and_ai_usage(self) -> None:
        """Every category lands on its expected rung; 900/1000 succeed; the
        AI rung wins 250 of those (25% of the whole corpus) and is attempted
        nowhere else."""
        corpus = _WallCorpus()
        winners = self._walk(corpus)
        for url, category, _err in _sites():
            self.assertEqual(winners[url], self._expected_winner(category), url)
        succeeded = [url for url, rung in winners.items() if rung is not None]
        ai_wins = [url for url, rung in winners.items() if rung == "ai-search"]
        self.assertEqual(len(succeeded), 900)
        self.assertEqual(len(ai_wins), 250)
        # challenge-wall (150) + ai-only (100) win through the model; the
        # hard-dead sites (50) exhaust every rung including it. Nobody else.
        self.assertEqual(corpus.ai_rung_attempts, 300)
        cheap = {
            url
            for url, category, _err in _sites()
            if category in {"ua-wall", "reader-friendly", "type-refusal", "wayback-only"}
        }
        ai_touched = {url for url, rung in corpus.attempts if rung == "ai-search"}
        self.assertFalse(cheap & ai_touched)

    def test_memory_makes_the_second_pass_cheaper(self) -> None:
        """Pass 1 walks 2450 rungs; pass 2, memory warm, walks 1100 and
        starts every successful walk AT the remembered rung."""
        first = _WallCorpus()
        self._walk(first)
        self.assertEqual(len(first.attempts), _FIRST_PASS_ATTEMPTS)
        for host, rung in (
            ("ua-wall-000.test", "browser-headers"),
            ("reader-friendly-199.test", "reader"),
            ("challenge-wall-149.test", "ai-search"),
            ("wayback-only-149.test", "wayback"),
        ):
            self.assertEqual(StrategyMemory(default_memory_path()).get(host), rung, host)
        second = _WallCorpus()
        self._walk(second)
        self.assertEqual(len(second.attempts), _SECOND_PASS_ATTEMPTS)
        self.assertLess(len(second.attempts), len(first.attempts))
        first_rung: dict[str, str] = {}
        for url, rung in second.attempts:
            first_rung.setdefault(url, rung)
        for url, rung in first_rung.items():
            winner = self._expected_winner(_CATEGORY_BY_URL[url])
            if winner is not None:
                self.assertEqual(rung, winner, url)

    def test_refusals_never_walk_and_dead_sites_fail_honestly(self) -> None:
        corpus = _WallCorpus()
        with (
            mock.patch("freegem.smart_fetch._try_strategy", side_effect=corpus.try_strategy),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            for url, category, first_error in _sites():
                if category not in {"ssrf-refused", "hard-dead"}:
                    continue
                with self.assertRaises(ScrapeError) as caught:
                    fallback_scrape(self.cfg, url, max_chars=1000, first_error=first_error)
                if category == "ssrf-refused":
                    self.assertEqual(str(caught.exception), first_error)
                else:
                    self.assertIn("every read path refused", str(caught.exception))
                    self.assertIn("honest failure taxonomy", str(caught.exception))
        attempted = {url for url, _rung in corpus.attempts}
        refused_urls = {url for url, c, _ in _sites() if c == "ssrf-refused"}
        self.assertFalse(attempted & refused_urls, "our own refusals must not walk")
        dead_urls = {url for url, c, _ in _sites() if c == "hard-dead"}
        self.assertEqual(
            sorted(corpus.attempts),
            sorted(
                (url, rung)
                for url in dead_urls
                for rung in ("browser-headers", "reader", "wayback", "ai-search")
            ),
        )


# ─── the ai-clean round: 200 boilerplate-heavy pages through the real gate ──

_BOILERPLATE = (
    "Accept all cookies? Subscribe to our newsletter! Related stories you "
    "may like. Nav item. Nav item. Footer link. "
) * 10
_ARTICLE = "The actual article body, one readable paragraph after another. " * 30
#: ai_clean strips the model's answer before returning it, so the expected
#: body is that trimmed shape.
_CLEANED_BODY = ("The cleaned article body, free of chrome. " * 40).strip()


class _FakeCleanProvider:
    """Records prompts and answers with a clean article body (or a refusal)."""

    prompts: ClassVar[list[str]] = []
    answer: ClassVar[str] = _CLEANED_BODY

    def __init__(self, cfg: Any) -> None:
        pass

    def complete(self, prompt: str) -> str:
        type(self).prompts.append(prompt)
        return type(self).answer


class AiCleanCorpusTests(unittest.TestCase):
    """ai_clean over 200 walled-in pages: bounded, honest, model-gated."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(smart_fetch_ai_clean=True)
        _FakeCleanProvider.prompts = []
        _FakeCleanProvider.answer = _CLEANED_BODY

    def test_two_hundred_boilerplate_pages_clean_to_the_article(self) -> None:
        with mock.patch("freegem.llm_extract.BridgeProvider", _FakeCleanProvider):
            for i in range(200):
                url = f"https://magazine-{i:03d}.test/post.html"
                cleaned = ai_clean(self.cfg, url, _BOILERPLATE + _ARTICLE)
                self.assertEqual(cleaned, _CLEANED_BODY)
        self.assertEqual(len(_FakeCleanProvider.prompts), 200)
        for prompt in _FakeCleanProvider.prompts:
            self.assertIn("[PAGE TEXT]", prompt)
            self.assertIn("[/PAGE TEXT]", prompt)

    def test_oversized_pages_reach_the_model_bounded(self) -> None:
        huge = ("filler prose that is definitely not content. " * 2000) + _ARTICLE
        self.assertGreater(len(huge), _AI_CLEAN_MAX_INPUT_CHARS)
        with mock.patch("freegem.llm_extract.BridgeProvider", _FakeCleanProvider):
            ai_clean(self.cfg, "https://huge.test/", huge)
        prompt = _FakeCleanProvider.prompts[0]
        self.assertLess(len(prompt), _AI_CLEAN_MAX_INPUT_CHARS + 600)

    def test_short_pages_are_refused_without_a_model_call(self) -> None:
        with mock.patch("freegem.llm_extract.BridgeProvider", _FakeCleanProvider):
            with self.assertRaises(ScrapeError) as caught:
                ai_clean(self.cfg, "https://tiny.test/", "tiny page")
        self.assertIn("nothing worth cleaning", str(caught.exception))
        self.assertLess(len("tiny page"), _AI_CLEAN_MIN_CHARS)
        self.assertEqual(_FakeCleanProvider.prompts, [])

    def test_honest_nonanswers_are_refused_as_errors(self) -> None:
        _FakeCleanProvider.answer = "NO MAIN CONTENT"
        with mock.patch("freegem.llm_extract.BridgeProvider", _FakeCleanProvider):
            with self.assertRaises(ScrapeError) as caught:
                ai_clean(self.cfg, "https://empty.test/", _BOILERPLATE + _ARTICLE)
        self.assertIn("no main content", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

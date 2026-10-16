"""Issue-driven CLI feature tests (#46, #33, #10).

Hermetic by construction: the model entry points (``generate``,
``generate_stream``) and the scrape/search entry points are mocked, so no
test here touches the network.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest import mock

import pytest

from freegem.cli import main

# ── issue #46: ask --search must not serve upstream soft-failures as answers ──

#: The page-reading refusal family smart_fetch's detector already knows.
_AI_NONANSWER = "Sorry, I wasn't able to access that page's content."

#: The generic chat refusals observed in the wild (the exact reports on #46).
_CHAT_REFUSALS = [
    "Sorry, I'm having a hard time fulfilling your request.",
    "Something went wrong on our end.",
]

_REAL_ANSWER = "The Eiffel Tower is 330 metres tall.\n\nSources:\n- https://example.com/tower"


def _ask_search(capsys, answer: str) -> tuple[int, str, str]:
    with mock.patch("freegem.cli.run_tool", return_value=answer):
        code = main(["ask", "--search", "how tall is the eiffel tower"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("refusal", [_AI_NONANSWER, *_CHAT_REFUSALS])
def test_ask_search_soft_failure_is_not_an_answer(capsys, refusal) -> None:
    """A soft-failure exits non-zero, keeps stdout empty, explains on stderr."""
    code, out, err = _ask_search(capsys, refusal)
    assert code != 0
    assert out == "", "the refusal must never reach stdout as an answer"
    assert refusal in err, "the raw upstream text goes to stderr for the human"
    assert "soft-failure" in err
    assert "retry or rephrase" in err


def test_ask_search_normal_answer_unchanged(capsys) -> None:
    code, out, err = _ask_search(capsys, _REAL_ANSWER)
    assert code == 0
    assert out == _REAL_ANSWER + "\n"
    assert "soft-failure" not in err


def test_ask_search_long_answer_containing_refusal_phrase_is_content(capsys) -> None:
    """Past the length cap a refusal phrase is quoted content, not a refusal.

    Mirrors smart_fetch's rule: only SHORT answers are checked, so a page
    about debugging that says "...when something went wrong..." survives.
    """
    long_answer = ("We debugged the pipeline step by step. " * 60) + " something went wrong."
    assert len(long_answer) > 2000
    code, out, err = _ask_search(capsys, long_answer)
    assert code == 0
    assert out == long_answer + "\n"
    assert "soft-failure" not in err


# ── issue #33: scrape --format markdown ──────────────────────────────────────


def _fake_page() -> Any:
    from freegem.web_scraper import ScrapeResult

    return ScrapeResult(
        url="https://a.example/",
        title="A Title",
        text="alpha page body",
        links=[("Docs", "https://a.example/docs"), ("", "https://a.example/bare")],
    )


def test_scrape_markdown_single_url(capsys) -> None:
    with mock.patch("freegem.web_scraper.scrape_page", return_value=_fake_page()):
        code = main(["scrape", "https://a.example/", "--format", "markdown"])
    out = capsys.readouterr().out
    assert code == 0
    assert "# A Title" in out, "the page title becomes the H1"
    assert "alpha page body" in out
    assert "## Links" not in out, "links need --links, same contract as text"


def test_scrape_markdown_single_url_with_links(capsys) -> None:
    with mock.patch("freegem.web_scraper.scrape_page", return_value=_fake_page()):
        code = main(["scrape", "https://a.example/", "--links", "--format", "markdown"])
    out = capsys.readouterr().out
    assert code == 0
    assert "## Links" in out
    assert "- [Docs](https://a.example/docs)" in out, "real markdown link syntax"
    # Unlabeled link: the href doubles as the label, mirroring format_page's
    # "label — href" fallback. The expected string is built via chr() so the
    # label's first two characters never appear as a raw literal in this
    # source file (some terminal filters eat that byte pair; the in-process
    # value pytest compares is unaffected).
    open_bracket = chr(91)
    unlabeled = f"- {open_bracket}https://a.example/bare](https://a.example/bare)"
    assert unlabeled in out


def test_scrape_markdown_single_url_passes_fetch_flags(capsys) -> None:
    """Markdown uses the same fetch path, guards, and cache flags as text."""
    with mock.patch("freegem.web_scraper.scrape_page", return_value=_fake_page()) as scrape_page:
        code = main(
            [
                "scrape",
                "https://a.example/",
                "--format",
                "markdown",
                "--no-smart",
                "--no-cache",
                "--max-chars",
                "1234",
                "--respect-robots",
            ]
        )
    assert code == 0
    kwargs = scrape_page.call_args.kwargs
    assert kwargs["max_chars"] == 1234
    assert kwargs["smart"] is False
    assert kwargs["cache"] is None
    assert kwargs["ignore_robots"] is False


def test_scrape_markdown_batch(capsys) -> None:
    from freegem.web_scraper import BatchItem

    items = [
        BatchItem(url="https://a.example/", ok=True, title="T", text="body", chars=4, via="html"),
        BatchItem(url="https://b.example/", ok=False, error="HTTP 403 fetching 'u'"),
    ]
    with mock.patch("freegem.web_scraper.scrape_many", return_value=items):
        code = main(["scrape", "https://a.example/", "https://b.example/", "--format", "markdown"])
    out = capsys.readouterr().out
    assert code == 0
    assert "# T" in out
    assert "body" in out
    assert "**error:** HTTP 403" in out
    assert "---" in out, "batch pages are separated by a markdown rule"


def test_scrape_markdown_batch_all_failed_exits_one(capsys) -> None:
    from freegem.web_scraper import BatchItem

    dead = [BatchItem(url="https://x.example/", ok=False, error="HTTP 403")]
    with mock.patch("freegem.web_scraper.scrape_many", return_value=dead):
        code = main(["scrape", "https://x.example/", "--format", "markdown"])
    assert code == 1


def test_scrape_text_format_unchanged(capsys) -> None:
    """The default text path still routes through scrape_cli (format_page)."""
    with (
        mock.patch("freegem.web_scraper.scrape_cli", return_value="WRAPPED PAGE") as scrape_cli,
        mock.patch("freegem.web_scraper.scrape_page") as scrape_page,  # must NOT be called
    ):
        code = main(["scrape", "https://a.example/"])
    out = capsys.readouterr().out
    assert code == 0
    assert out == "WRAPPED PAGE\n"
    assert scrape_cli.called
    assert not scrape_page.called


def test_scrape_jsonl_rows_unchanged_no_markdown_key(capsys) -> None:
    from freegem.web_scraper import BatchItem

    items = [
        BatchItem(url="https://a.example/", ok=True, title="A", text="alpha page", chars=10),
    ]
    with mock.patch("freegem.web_scraper.scrape_many", return_value=items):
        code = main(["scrape", "https://a.example/", "--format", "jsonl"])
    rows = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert code == 0
    assert len(rows) == 1
    assert rows[0]["text"] == "alpha page"
    assert "markdown" not in rows[0], "jsonl rows gain no markdown key"


# ── issue #10: stream long-running ask output ────────────────────────────────


def _two_chunk_stream(record: dict) -> Any:
    """A generate_stream stand-in that yields exactly two chunks.

    After the first chunk it snapshots what the CLI has already printed, so
    the test can prove output happened BEFORE the generator completed. The
    snapshot PEEKS at the capture buffer instead of draining it: capsys's
    readouterr() would consume the bytes and hide them from the final check.
    """

    def stream(cfg, prompt, mode_id, think_mode, file_refs, extra_fields):
        yield "chunk one "
        sys.stdout.flush()
        peek = getattr(sys.stdout, "getvalue", None)
        record["printed_mid_stream"] = peek() if callable(peek) else ""
        yield "chunk two"

    return stream


def test_ask_stream_prints_before_generator_completes(capsys, monkeypatch) -> None:
    """Streaming ask begins printing before the generator completes."""
    record: dict[str, str] = {}
    monkeypatch.setattr("freegem.gemini.HAS_HTTPX", True)
    monkeypatch.setattr("freegem.gemini.generate_stream", _two_chunk_stream(record))
    code = main(["ask", "hello"])
    captured = capsys.readouterr()
    assert code == 0
    assert record["printed_mid_stream"] == "chunk one ", "first chunk printed live"
    assert captured.out == "chunk one chunk two\n", "both chunks arrive, one trailing newline"


def test_ask_plain_still_consumes_generate_stream(capsys, monkeypatch) -> None:
    """The interactive path must use generate_stream, not buffered generate."""
    calls: list[str] = []
    monkeypatch.setattr("freegem.gemini.HAS_HTTPX", True)

    def stream(*_args: Any) -> Any:
        calls.append("stream")
        yield "streamed answer"

    def buffered(*_args: Any) -> str:
        calls.append("buffered")
        return "buffered answer"

    monkeypatch.setattr("freegem.gemini.generate_stream", stream)
    monkeypatch.setattr("freegem.gemini.generate", buffered)
    code = main(["ask", "hello"])
    captured = capsys.readouterr()
    assert code == 0
    assert calls == ["stream"]
    assert captured.out == "streamed answer\n"
    assert "buffered answer" not in captured.out


def test_ask_search_is_single_buffered_payload(capsys, monkeypatch) -> None:
    """The machine-readable mode stays fully buffered: exactly one payload."""
    with mock.patch("freegem.cli.run_tool", return_value="one whole answer") as run_tool:
        code = main(["ask", "--search", "q"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "one whole answer\n"
    assert run_tool.called, "search mode is the buffered pipeline, untouched by streaming"


def test_ask_without_httpx_single_buffered_payload(capsys, monkeypatch) -> None:
    """No httpx: one buffered payload with the same shape as streamed output.

    The streamed result is the chunks joined plus exactly one trailing
    newline; the fallback must not differ (no extra blank lines, no prefix).
    """
    monkeypatch.setattr("freegem.gemini.HAS_HTTPX", False)
    monkeypatch.setattr("freegem.gemini.generate", lambda *_a: "whole answer")
    code = main(["ask", "hello"])
    captured = capsys.readouterr()
    assert code == 0
    streamed_shape = "who" + "le answer" + "\n"
    assert captured.out == "whole answer\n" == streamed_shape

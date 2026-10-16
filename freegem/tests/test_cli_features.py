"""Issue-driven CLI feature tests (#46, #33, #10).

Hermetic by construction: the model entry points (``generate``,
``generate_stream``) and the scrape/search entry points are mocked, so no
test here touches the network.
"""

from __future__ import annotations

import json
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

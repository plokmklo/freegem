"""Important-only distillation of oversized pages.

A 10-50 MB page is not more INFORMATION than a 50 KB one -- it is more
boilerplate, more inline JSON, more tracking markup. The fetch stack now
reads pages up to 100 MB, so the campaign/runner can hand the extracted
text of a huge page to Gemini with a strict contract: keep the load-bearing
content (headings, facts, figures, statements, lists), drop navigation,
ads, scripts and repeated chrome; output stays bounded.

When no Gemini session is configured -- or the call fails -- an extractive
fallback keeps headings plus the densest paragraphs, so a distilled record
is never empty and never invented. The mode used is ALWAYS recorded:
``gemini`` (model summary), ``extractive`` (deterministic fallback), or
``empty`` (the page had no text to distill).
"""

from __future__ import annotations

from ..config import BridgeConfig
from ..gemini import generate
from ..models import DEFAULT_MODEL, resolve_model

__all__ = [
    "DISTILL_INPUT_CHARS",
    "DISTILL_OUTPUT_CHARS",
    "DISTILL_THRESHOLD_BYTES",
    "distill_text",
    "extractive_summary",
]

#: Pages at or above this raw size are distillation candidates.
DISTILL_THRESHOLD_BYTES = 2 * 1024 * 1024
#: Text fed to Gemini, capped (a huge page's head carries most signal).
DISTILL_INPUT_CHARS = 180_000
#: Bounded output: important-only, not a reprint.
DISTILL_OUTPUT_CHARS = 14_000

_PROMPT = (
    "The following is the extracted text of one large web page "
    "({chars} characters). Reduce it to the IMPORTANT content only: keep "
    "headings, facts, numbers, named entities, statements and list items "
    "that carry the page's actual information; drop navigation menus, "
    "promotions, cookie notices, repeated boilerplate and UI fragments. "
    "Preserve the original language. Output plain text, at most {out} "
    "characters, no commentary about the task.\n\n---\n{body}\n---"
)


def distill_text(
    cfg: BridgeConfig, url: str, text: str, *, content_type: str = ""
) -> tuple[str, str]:
    """Return ``(distilled_text, mode)`` for one page's extracted text.

    ``mode`` is ``gemini``, ``extractive`` or ``empty``. Never raises: a
    failed model call degrades to the extractive cut.
    """
    body = text.strip()
    if not body:
        return "", "empty"
    prompt = _PROMPT.format(
        chars=len(body), out=DISTILL_OUTPUT_CHARS, body=body[:DISTILL_INPUT_CHARS]
    )
    try:
        _name, mode_id, think, _err, _extra = resolve_model(DEFAULT_MODEL)
        out = generate(cfg, prompt, mode_id or 0, think or 0).strip()
        if out:
            return out[:DISTILL_OUTPUT_CHARS], "gemini"
    except Exception:
        pass  # no session, upstream refused, network down: degrade honestly
    return extractive_summary(body), "extractive"


def extractive_summary(text: str, *, max_chars: int = DISTILL_OUTPUT_CHARS) -> str:
    """Deterministic important-only cut, no model involved.

    Keeps heading-shaped lines and the densest paragraphs in document order;
    drops obvious boilerplate (menu fragments, cookie/legal chrome lines).
    """
    lines = [ln.strip() for ln in text.splitlines()]
    blocks: list[tuple[str, str]] = []  # (kind, content)
    buf: list[str] = []
    for ln in lines:
        if not ln:
            if buf:
                blocks.append(("p", " ".join(buf)))
                buf = []
            continue
        if _headingish(ln):
            if buf:
                blocks.append(("p", " ".join(buf)))
                buf = []
            blocks.append(("h", ln))
        else:
            buf.append(ln)
    if buf:
        blocks.append(("p", " ".join(buf)))

    paragraphs = [b for b in blocks if b[0] == "p" and len(b[1]) >= 80]
    boiler = {id(b) for b in paragraphs if _boilerplate(b[1])}
    keep_p = {id(b) for b in paragraphs} - boiler
    ranked = sorted(
        (b for b in paragraphs if id(b) in keep_p),
        key=lambda b: _density(b[1]),
        reverse=True,
    )
    chosen: list[tuple[str, str]] = []
    lead = next((b for b in paragraphs if id(b) in keep_p), None)
    if lead is not None:
        chosen.append(lead)
    for b in ranked:
        if len(chosen) >= 24:
            break
        if b is lead:
            continue
        chosen.append(b)
    keep_ids = {id(b) for b in chosen}

    # Document order, heading attached to the kept paragraph it introduces;
    # headings over dropped/boilerplate paragraphs disappear with them.
    budget = max_chars
    out: list[str] = []
    current_heading: str | None = None
    for block in blocks:
        if block[0] == "h":
            current_heading = block[1]
            continue
        if id(block) not in keep_ids:
            continue
        piece = block[1]
        if len(piece) > 2000:
            piece = piece[:2000] + "…"
        needed = len(piece) + (len(current_heading) + 1 if current_heading else 0)
        if budget - needed < 0:
            break
        if current_heading:
            out.append(current_heading)
            budget -= len(current_heading) + 1
        out.append(piece)
        budget -= len(piece) + 1
        current_heading = None
    return "\n".join(out)[:max_chars]


def _headingish(line: str) -> bool:
    return (
        2 <= len(line) <= 120
        and not line.endswith((".", ",", ";", ":", "?", "!"))
        and sum(ch.isalpha() for ch in line) >= 3
    )


def _boilerplate(paragraph: str) -> bool:
    low = paragraph.lower()
    markers = (
        "cookie",
        "accept all",
        "privacy policy",
        "terms of service",
        "sign in",
        "subscribe",
        "newsletter",
        "all rights reserved",
        "advertisement",
        "skip to content",
        "javascript is",
        "enable js",
        "enable javascript",
    )
    return len(paragraph) < 220 and any(m in low for m in markers)


def _density(paragraph: str) -> float:
    alpha = sum(ch.isalnum() for ch in paragraph)
    return alpha / max(1, len(paragraph))

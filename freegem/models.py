"""Gemini model registry.

``mode`` values come from Gemini Web's frontend ``MODE_CATEGORY`` enum:
    1 = FAST, 2 = THINKING, 3 = PRO, 4 = AUTO, 5 = FAST_DYNAMIC_THINKING, 6 = FLASH_LITE
``think`` is the reasoning depth: 0 = deepest, 4 = auto.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["MODELS", "ModelSpec", "model_ids", "parse_think_override", "resolve_model"]


@dataclass(frozen=True)
class ModelSpec:
    mode: int
    think: int
    desc: str
    requires_cookie: bool = False
    extra: dict[int, Any] | None = None


MODELS: dict[str, ModelSpec] = {
    "gemini-3.7-flash": ModelSpec(
        mode=1, think=4, desc="Latest all-around model (Gemini 3.7 Flash)"
    ),
    "gemini-3.6-flash": ModelSpec(mode=1, think=4, desc="All-around model (Gemini 3.6 Flash)"),
    "gemini-3.5-flash": ModelSpec(
        mode=1, think=4, desc="Alias for gemini-3.6-flash (backend upgraded)"
    ),
    "gemini-3.5-flash-thinking": ModelSpec(
        mode=2,
        think=0,
        desc="Deep thinking mode, longest output (~20k chars)",
    ),
    "gemini-3.5-flash-thinking-lite": ModelSpec(
        mode=5, think=0, desc="Dynamic thinking with adaptive depth"
    ),
    "gemini-3.1-pro": ModelSpec(
        mode=3,
        think=4,
        desc="Pro model; routes to Flash unless a Gemini Advanced cookie is set",
        requires_cookie=True,
    ),
    "gemini-3.1-pro-enhanced": ModelSpec(
        mode=3,
        think=4,
        desc="Pro with enhanced output (experimental)",
        requires_cookie=True,
        extra={31: 2, 80: 3},
    ),
    "gemini-auto": ModelSpec(mode=4, think=4, desc="Auto model selection"),
    "gemini-flash-lite": ModelSpec(mode=6, think=4, desc="Lightweight fast model"),
}

DEFAULT_MODEL = "gemini-3.6-flash"


def model_ids() -> tuple[str, ...]:
    return tuple(MODELS.keys())


def parse_think_override(name: str) -> tuple[str, int | None, str | None]:
    """Split ``model@think=N`` into (base_name, override, error).

    Returns the error string instead of raising so callers can map it to a
    protocol-appropriate status code.
    """
    if "@think=" not in name:
        return name, None, None
    base, _, raw = name.rpartition("@think=")
    base = base or name
    try:
        level = int(raw)
    except ValueError:
        return base, None, f"Invalid think level: {raw!r} (expected 0-4)"
    if not 0 <= level <= 4:
        return base, None, f"Think level out of range: {level} (expected 0-4)"
    return base, level, None


def resolve_model(
    model_name: str, default: str = DEFAULT_MODEL
) -> tuple[str | None, int | None, int | None, str | None, dict[int, Any] | None]:
    """Resolve a client-supplied model name.

    Returns ``(name, mode_id, think_mode, error, extra_fields)``.

    Unknown names fall back to the default instead of erroring: clients such as
    Codex CLI send model identifiers that predate this registry, and hard
    failing on them is more annoying than routing them to the default. The
    fallback is silent by design but surfaced through ``log`` by the caller.
    """
    base, think_override, err = parse_think_override(model_name)
    if err:
        return None, None, None, err, None

    spec = MODELS.get(base)
    if spec is None:
        # The echoed name must be the one actually routed: if the configured
        # default is itself unknown, keep base = DEFAULT_MODEL so responses do
        # not advertise a model that never reached upstream.
        base = default if default in MODELS else DEFAULT_MODEL
        spec = MODELS[base]

    think = spec.think if think_override is None else think_override
    return base, spec.mode, think, None, dict(spec.extra) if spec.extra else None


def describe_model(model_name: str) -> str:
    """One-line human description, for tool listings and errors."""
    base, _, _ = parse_think_override(model_name)
    spec = MODELS.get(base)
    if spec is None:
        return "unknown model"
    return spec.desc

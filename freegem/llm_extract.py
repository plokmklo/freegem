"""Schema-constrained, provider-neutral structured extraction (2.8.0).

"Give me the product name, price, and availability from this page" — answered
by an LLM — is only reliable when the answer is CHECKED against a schema
before anyone consumes it. This module owns three separable concerns:

* :class:`ExtractionSchema` — named typed fields (str/int/float/bool/enum/
  array), rendered BOTH as a JSON Schema fragment (for APIs that take one)
  and as an instruction block (for models that do not);
* :func:`validate_extraction` — the gate: unknown keys dropped, scalar types
  coerced ("42" -> 42, "yes" -> true), enums enforced, required fields
  reported missing instead of silently absent. Repair happens BEFORE
  rejection — models stringifying numbers is normal, not an error;
* :class:`ExtractionProvider` implementations — the bridge's own
  :func:`freegem.gemini.generate` (zero new dependencies, the default),
  any OpenAI-compatible endpoint (stdlib urllib), and a :class:`NullProvider`
  that always fails validation so callers can test the pipeline without a
  model.

Provider-neutral means: the schema, validation, and repair logic are
identical no matter which provider answers; swapping providers changes one
constructor argument and nothing else. The provider never sees anything but
the text and the schema — no tool definitions, no free-form chat.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import BridgeConfig
from .gemini import generate

__all__ = [
    "BridgeProvider",
    "ExtractionError",
    "ExtractionProvider",
    "ExtractionSchema",
    "FieldSpec",
    "NullProvider",
    "OpenAICompatProvider",
    "extract_structured",
    "validate_extraction",
]

_FIELD_TYPES = ("string", "integer", "number", "boolean", "array", "enum")


def _parse_json_object(raw: str) -> Any:
    """Parse the model's answer, tolerating code fences and stray prose."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z0-9]*\s*|\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except ValueError:
        pass
    # Prose braces are the norm ("items: {n} -> {json}"). The old greedy
    # {...} match spanned the FIRST brace to the LAST, which never decodes
    # the moment any stray pair appears. Scan each '{' instead and take the
    # first balanced object that actually parses.
    decoder = json.JSONDecoder()
    index = raw.find("{")
    while index != -1:
        try:
            value, _consumed = decoder.raw_decode(raw, index)
        except ValueError:
            index = raw.find("{", index + 1)
            continue
        return value
    raise ValueError("no JSON object found in the answer")


class ExtractionError(Exception):
    """Extraction failed at the provider or could not satisfy the schema."""


@dataclass(frozen=True)
class FieldSpec:
    """One field of an extraction schema."""

    name: str
    type: str = "string"  # one of _FIELD_TYPES
    description: str = ""
    required: bool = True
    enum: tuple[str, ...] = ()
    items_type: str = "string"  # for type == "array"

    def __post_init__(self) -> None:
        if self.type not in _FIELD_TYPES:
            raise ValueError(f"unknown field type {self.type!r}; expected one of {_FIELD_TYPES}")
        if self.type == "enum" and not self.enum:
            raise ValueError(f"enum field {self.name!r} needs a non-empty enum tuple")


@dataclass(frozen=True)
class ExtractionSchema:
    """A named set of fields to extract from a document."""

    name: str
    fields: tuple[FieldSpec, ...] = field(default=())

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    def to_json_schema(self) -> dict[str, Any]:
        """JSON-Schema object form (for providers/APIs that accept one)."""
        props: dict[str, Any] = {}
        required: list[str] = []
        for f in self.fields:
            if f.type == "enum":
                props[f.name] = {
                    "type": "string",
                    "enum": list(f.enum),
                    "description": f.description,
                }
            elif f.type == "array":
                props[f.name] = {
                    "type": "array",
                    "items": {"type": f.items_type},
                    "description": f.description,
                }
            else:
                props[f.name] = {"type": f.type, "description": f.description}
            if f.required:
                required.append(f.name)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.name,
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }

    def to_prompt_block(self) -> str:
        """Human/model-readable instructions embedded into the prompt."""
        lines = [f'Extract JSON object "{self.name}" with exactly these fields:']
        for f in self.fields:
            spec = f.type
            if f.type == "enum":
                spec = f"one of {list(f.enum)}"
            elif f.type == "array":
                spec = f"array of {f.items_type}"
            req = "required" if f.required else "optional"
            desc = f" — {f.description}" if f.description else ""
            lines.append(f'- "{f.name}" ({spec}, {req}){desc}')
        lines.append("Respond with ONLY the JSON object, no prose, no code fences.")
        return "\n".join(lines)


def _coerce_scalar(value: Any, ftype: str) -> tuple[Any, bool]:
    """Best-effort coercion of one value to the field type. (value, ok)."""
    if ftype == "string":
        if isinstance(value, str):
            return value, True
        if isinstance(value, (int, float, bool)):
            return str(value), True
        return value, False
    if ftype in ("integer", "number"):
        if isinstance(value, bool):  # bool is an int subclass; never a count
            return value, False
        if ftype == "integer" and isinstance(value, int):
            return value, True
        if ftype == "number" and isinstance(value, (int, float)):
            return value, True
        if isinstance(value, str):
            try:
                return (
                    (int(value.strip()), True)
                    if ftype == "integer"
                    else (float(value.strip()), True)
                )
            except ValueError:
                return value, False
        return value, False
    if ftype == "boolean":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "1", "on"):
                return True, True
            if low in ("false", "no", "0", "off"):
                return False, True
        return value, False
    if ftype == "array":
        if isinstance(value, list):
            return value, True
        if isinstance(value, str) and value.strip():
            # a model that forgot it promised JSON arrays still split sanely
            return [v.strip() for v in value.split(",") if v.strip()], True
        return value, False
    return value, False


def validate_extraction(schema: ExtractionSchema, data: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate/repair one extraction against the schema.

    Returns ``(clean, problems)``: ``clean`` contains only known fields,
    coerced to their declared types; ``problems`` lists every repair or
    violation found. A field required-and-missing lands in ``problems`` AND
    is absent from ``clean`` — callers decide whether that is fatal
    (:func:`extract_structured` treats it as such) or tolerable.
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return {}, [f"expected a JSON object, got {type(data).__name__}"]
    clean: dict[str, Any] = {}
    for f in schema.fields:
        if f.name not in data or data[f.name] is None:
            if f.required:
                problems.append(f"missing required field {f.name!r}")
            continue
        value = data[f.name]
        if f.type == "enum":
            if isinstance(value, str) and value in f.enum:
                clean[f.name] = value
            else:
                problems.append(f"field {f.name!r} value {value!r} not in enum {list(f.enum)}")
            continue
        if f.type == "array":
            coerced, ok = _coerce_scalar(value, "array")
            if not ok:
                problems.append(f"field {f.name!r} is not an array")
                continue
            items: list[Any] = []
            bad = 0
            for item in coerced:
                c, ok_item = _coerce_scalar(item, f.items_type)
                if ok_item:
                    items.append(c)
                else:
                    bad += 1
            if bad:
                problems.append(f"field {f.name!r}: dropped {bad} item(s) of wrong type")
            clean[f.name] = items
            continue
        coerced, ok = _coerce_scalar(value, f.type)
        if ok:
            clean[f.name] = coerced
            if coerced != value and not isinstance(value, bool):
                problems.append(f"field {f.name!r} coerced {value!r} -> {coerced!r}")
        else:
            problems.append(f"field {f.name!r} expected {f.type}, got {type(value).__name__}")
    for key in data:
        if schema.field(key) is None:
            problems.append(f"dropped unknown field {key!r}")
    return clean, problems


class ExtractionProvider(Protocol):
    """Anything that turns one prompt into one completion string."""

    def complete(self, prompt: str) -> str: ...


def _extraction_prompt(schema: ExtractionSchema, text: str) -> str:
    from .web_scraper import (  # lazy: keep this module import-light
        _UNTRUSTED_CLOSE,
        _UNTRUSTED_OPEN,
        _defuse_untrusted_markers,
    )

    # The document is page-derived, hence untrusted: cap it first, then
    # defuse BOTH the shared untrusted markers and this frame's own closer so
    # page text cannot end the [DOCUMENT] block early and answer the schema
    # itself. The real closer is appended after the defused content.
    document = _defuse_untrusted_markers(text[:200_000])
    document = re.sub(r"\[/document\]", "[/DOCUMENT removed]", document, flags=re.IGNORECASE)
    return (
        "You are a JSON extraction engine. Do not chat.\n"
        + schema.to_prompt_block()
        + f"\n\n[DOCUMENT]\n{_UNTRUSTED_OPEN}\n{document}\n{_UNTRUSTED_CLOSE}\n[/DOCUMENT]"
    )


class BridgeProvider:
    """Uses freegem's own Gemini bridge (generate()) — the zero-config default.

    ``model_id``/``think_mode`` map to generate()'s numeric selectors
    (0 = default model, 0 = auto thinking), matching the crawler distill
    call convention; ``model`` is accepted for interface parity with the
    OpenAI-compatible provider and ignored (the bridge has one model).
    """

    def __init__(
        self, cfg: BridgeConfig, *, model: str | None = None, model_id: int = 0, think_mode: int = 0
    ) -> None:
        self.cfg = cfg
        self.model = model  # accepted, unused: the bridge routes one session
        self.model_id = model_id
        self.think_mode = think_mode

    def complete(self, prompt: str) -> str:
        return generate(self.cfg, prompt, self.model_id, self.think_mode)


class OpenAICompatProvider:
    """Any OpenAI-compatible /chat/completions endpoint, via stdlib urllib.

    Lets freegem's extraction run through an external model (vLLM, Ollama's
    OpenAI face, a hosted provider) without an SDK dependency. Auth is the
    caller's business: pass the api_key you negotiated.
    """

    def __init__(
        self, base_url: str, api_key: str | None = None, model: str = "gpt-4o-mini"
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_sec = 120.0

    def complete(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ExtractionError(f"openai-compatible endpoint failed: {exc}") from exc
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExtractionError(f"unexpected response shape: {exc}") from exc


class NullProvider:
    """Always raises — pipeline testing without a model."""

    def complete(self, prompt: str) -> str:
        raise ExtractionError("null provider produces nothing by design")


def extract_structured(
    schema: ExtractionSchema,
    text: str,
    provider: ExtractionProvider,
    *,
    max_repairs: int = 1,
) -> tuple[dict[str, Any], list[str]]:
    """Extract once, validate, and (optionally) retry once with the problems shown.

    The retry is the cheap 80%: a model that stringified numbers, invented a
    field, or answered with unparseable prose usually fixes it when the
    complaint is read back verbatim. Returns ``(clean, problems)``; raises
    :class:`ExtractionError` when required fields are still missing or the
    provider is unreachable. The better of the two attempts wins (fewest
    problems; ties go to the repair, which saw the complaints). One repair is
    spent either on the parse failure or on the validation problems, never on
    both.
    """
    prompt = _extraction_prompt(schema, text)
    try:
        first_raw = provider.complete(prompt)
        first_data = _parse_json_object(first_raw)
    except ValueError as exc:
        # Unparseable output is the most common malformation; give the model
        # one shot at it, exactly like the validation round gets one.
        if max_repairs <= 0:
            raise ExtractionError(f"provider returned unparseable JSON: {exc}") from exc
        try:
            second_raw = provider.complete(
                prompt
                + f"\n\nYour previous answer could not be parsed as JSON ({exc}). "
                "Answer again with ONLY the JSON object, no prose, no code fences."
            )
        except Exception as retry_exc:
            # The repair call itself failed (or a second answer never
            # arrived); the original parse failure is what the caller needs.
            raise ExtractionError(
                f"provider returned unparseable JSON: {exc}"
            ) from retry_exc
        try:
            second_data = _parse_json_object(second_raw)
        except ValueError as retry_exc:
            raise ExtractionError(
                f"provider returned unparseable JSON: {retry_exc}"
            ) from retry_exc
        clean, problems = validate_extraction(schema, second_data)
        missing = [p for p in problems if p.startswith("missing required")]
        if missing:
            raise ExtractionError("; ".join(missing)) from exc
        return clean, problems
    clean, problems = validate_extraction(schema, first_data)

    if max_repairs > 0 and problems:
        repair_prompt = (
            prompt
            + "\n\nYour previous answer had these problems:\n- "
            + "\n- ".join(problems)
            + "\n\nAnswer again with ONLY the corrected JSON object."
        )
        try:
            second_raw = provider.complete(repair_prompt)
            second_data = _parse_json_object(second_raw)
            clean2, problems2 = validate_extraction(schema, second_data)
        except (ValueError, ExtractionError):
            return clean, problems  # a failed repair never destroys a first attempt
        if len(problems2) <= len(problems):
            clean, problems = clean2, problems2

    missing = [p for p in problems if p.startswith("missing required")]
    if missing:
        raise ExtractionError("; ".join(missing))
    return clean, problems


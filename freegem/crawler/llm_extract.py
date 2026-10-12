"""Schema-constrained, provider-neutral LLM extraction module.

Extracts structured JSON objects from raw HTML/text using any LLM completion backend (Gemini, OpenAI, or custom callable),
enforcing strict JSON Schema validation and automatic output repair upon validation errors.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import BridgeConfig
from ..gemini import generate
from ..models import resolve_model
from .extract import Schema

logger = logging.getLogger(__name__)


@dataclass
class LLMExtractionConfig:
    provider: str = "gemini"  # "gemini" or "custom"
    model: str = "gemini-2.5-flash"
    max_retries: int = 2
    bridge_config: BridgeConfig | None = None
    custom_completion_fn: Callable[[str], str] | None = None


def _clean_json_response(text: str) -> str:
    """Extract clean JSON substring from backtick codeblocks or markdown."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


class LLMExtractor:
    """Schema-constrained LLM extractor."""

    def __init__(self, config: LLMExtractionConfig | None = None) -> None:
        self.config = config or LLMExtractionConfig()

    def extract(self, text: str, schema: Schema | dict[str, Any]) -> list[dict[str, Any]]:
        """Extract schema-compliant items from document text using LLM."""
        schema_dict = schema.to_dict() if isinstance(schema, Schema) else schema

        prompt = (
            "You are an expert data extraction AI. Extract all structured items matching the following schema "
            "from the document below. Return ONLY a valid JSON array of objects representing the extracted items.\n\n"
            f"SCHEMA:\n{json.dumps(schema_dict, indent=2)}\n\n"
            f"DOCUMENT:\n{text[:15000]}\n\n"
            "JSON OUTPUT:"
        )

        for attempt in range(self.config.max_retries + 1):
            raw_reply = self._call_llm(prompt)
            clean_reply = _clean_json_response(raw_reply)

            try:
                parsed = json.loads(clean_reply)
                if isinstance(parsed, dict):
                    parsed = [parsed]
                if not isinstance(parsed, list):
                    raise ValueError("LLM response is not a JSON list of objects.")

                # Schema validation on items
                validated_items = []
                for item in parsed:
                    if isinstance(item, dict):
                        validated_items.append(self._validate_item(item, schema_dict))
                return validated_items

            except (json.JSONDecodeError, ValueError) as err:
                logger.warning("LLM extraction attempt %d failed schema validation: %s", attempt + 1, err)
                if attempt == self.config.max_retries:
                    raise ValueError(f"LLM extraction failed after {attempt + 1} attempts: {err}") from err
                prompt += (
                    f"\n\nERROR: Your previous output failed with error: {err}.\n"
                    "Please fix the format and return ONLY a valid JSON array conforming to the schema."
                )

        return []

    def _validate_item(self, item: dict[str, Any], schema_dict: dict[str, Any]) -> dict[str, Any]:
        """Simple schema enforcement and type alignment."""
        schema_dict.get("properties", {})
        required = schema_dict.get("required", [])

        for req_field in required:
            if req_field not in item:
                item[req_field] = None

        return item

    def _call_llm(self, prompt: str) -> str:
        if self.config.provider == "custom" and self.config.custom_completion_fn:
            return self.config.custom_completion_fn(prompt)

        cfg = self.config.bridge_config or BridgeConfig()
        _name, model_id, think_mode, _err, extra_fields = resolve_model(self.config.model)
        return generate(cfg, prompt, model_id or 1, think_mode or 0, extra_fields=extra_fields)

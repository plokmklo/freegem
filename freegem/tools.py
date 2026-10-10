"""Protocol translation: OpenAI / Google message shapes -> one flat prompt.

Gemini Web has no structured tool-calling and no message roles. Function
calling is therefore done in prose: the tool schemas are written into the
prompt with a fenced-block calling convention, and the reply is scraped back
out of code fences. This is lossy by nature and is called out in the README's
limitations section.

Every function here is pure: same input, same output, no network, no config.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from typing import Any

__all__ = [
    "ImageRef",
    "google_contents_to_prompt",
    "messages_to_prompt",
    "parse_function_calls",
    "parse_tool_calls",
]

# An image is either decoded bytes or a URL the multimodal layer must fetch.
ImageRef = tuple[Any, str]

TOOL_CALL_FENCE = "```tool_call"
FUNCTION_CALL_FENCE = "```function_call"
MAX_INLINE_IMAGE_B64 = 50_000


def _as_text(value: Any) -> str:
    """Coerce a message field to text, tolerating wrong client types.

    ``content`` is specified as a string or a list of parts, but clients in the
    wild send integers, dicts, and nulls. Without this, a single malformed part
    raises ``TypeError: sequence item 0: expected str instance`` inside
    ``" ".join`` and the whole request 500s -- a client-triggerable crash from
    input the spec never allowed. Anything non-string becomes its JSON form, so
    the model still sees something rather than a dropped turn.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


# ─── OpenAI helpers ───────────────────────────────────────────────────────────


def _tool_call_function(tc: Any) -> dict[str, Any]:
    """Safely extract the ``function`` object from a tool_call entry.

    ``{"function": "notadict"}`` raises AttributeError on ``.get``, which 500s
    the request. Anything non-dict yields an empty function so the turn still
    renders instead of crashing.
    """
    if not isinstance(tc, dict):
        return {}
    fn = tc.get("function")
    return fn if isinstance(fn, dict) else {}


def _arguments_field(fn: dict[str, Any]) -> str:
    """The tool call's ``arguments`` as a JSON *string* (OpenAI wire contract)."""
    value = fn.get("arguments", "{}")
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _tool_choice_instruction(tool_choice: Any) -> str:
    """Text appended to the tool preamble to express ``tool_choice``."""
    if tool_choice == "none":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if tool_choice == "required":
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return f'\n\nIMPORTANT: You MUST call the tool "{name}". Do not call other tools.'
    return ""


def _tool_preamble(tools: list[dict] | None, tool_choice: Any) -> str:
    if not tools or tool_choice == "none":
        return ""
    specs: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function", tool) if tool.get("type") == "function" else tool
        specs.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
        )
    specs = [s for s in specs if s["name"]]
    if not specs:
        return ""
    return (
        "# Tool Use\n\n"
        "You can call the following tools. Call format:\n"
        f'{TOOL_CALL_FENCE}\n{{"name": "func_name", "arguments": {{...}}}}\n```\n'
        "When calling tools, output ONLY the tool_call block(s).\n\n"
        f"Available tools:\n{json.dumps(specs, indent=2)}"
        f"{_tool_choice_instruction(tool_choice)}"
    )


def _image_from_openai_part(part: dict) -> ImageRef | None:
    """Extract an image from one OpenAI content part, or None if it is not one."""
    kind = part.get("type")
    if kind == "image_url":
        container = part.get("image_url") or {}
        if isinstance(container, dict):
            return _image_from_url(container.get("url"), container.get("mime_type"))
        return _image_from_url(container)
    if kind in ("input_image", "image"):
        container = part.get("image_url") or part.get("url")
        if isinstance(container, dict):
            return _image_from_url(container.get("url"), container.get("mime_type"))
        if container:
            return _image_from_url(container, part.get("mime_type"))
        raw = part.get("data") or part.get("base64")
        if isinstance(raw, str):
            mime = part.get("mime_type") or part.get("media_type") or "image/png"
            if raw.startswith("data:"):
                from .multimodal import decode_data_url

                decoded = decode_data_url(raw)
                return decoded
            try:
                return base64.b64decode(raw, validate=True), mime
            except (ValueError, TypeError, binascii.Error):
                return None
    return None


def _image_from_url(url: Any, mime: str | None = None) -> ImageRef | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        from .multimodal import decode_data_url

        return decode_data_url(url)
    return url, mime or "image/png"


def messages_to_prompt(
    messages: list[Any], tools: list[dict] | None = None, tool_choice: Any = "auto"
) -> tuple[str, list[ImageRef]]:
    """Flatten OpenAI messages into (prompt, images).

    Accepts ``list[Any]`` rather than ``list[dict]`` because the loop below
    deliberately skips non-dict entries -- real clients (and the tests) do send
    junk, and dropping it silently is the documented behaviour.
    """
    parts: list[str] = []
    images: list[ImageRef] = []

    preamble = _tool_preamble(tools, tool_choice)
    if preamble:
        parts.append(preamble)

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = _as_text(message.get("content", ""))

        if isinstance(message.get("content"), list):
            texts: list[str] = []
            for chunk in message["content"]:
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("type") in ("text", "input_text", "output_text"):
                    texts.append(_as_text(chunk.get("text", "")))
                    continue
                image = _image_from_openai_part(chunk)
                if image:
                    images.append(image)
                    texts.append("[Image attached]")
            content = " ".join(t for t in texts if t)

        tool_calls = message.get("tool_calls")
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            rendered = f"[Assistant]: {content}" if content else "[Assistant]:"
            if tool_calls:
                # json.dumps the whole call: a dict argument must become JSON
                # (not Python repr), and names containing quotes must not break
                # the fence. Arguments stay a JSON string per the OpenAI wire
                # contract; _arguments_field serialises non-strings.
                blocks = "\n".join(
                    TOOL_CALL_FENCE
                    + "\n"
                    + json.dumps(
                        {
                            "name": _as_text(fn.get("name", "")),
                            "arguments": _arguments_field(fn),
                        },
                        ensure_ascii=False,
                    )
                    + "\n```"
                    for tc in (tool_calls if isinstance(tool_calls, list) else [])
                    for fn in [_tool_call_function(tc)]
                    if fn
                )
                rendered = f"{rendered}\n{blocks}" if blocks else rendered
            parts.append(rendered)
        elif role in ("tool", "function"):
            parts.append(f"[Tool result for {message.get('name', '')}]: {content}")
        else:
            parts.append(content or "")

    return "\n\n".join(p for p in parts if p), images


# ─── Google-native helpers ────────────────────────────────────────────────────


def _google_function_calling_config(req: dict) -> dict[str, Any]:
    """Safely read ``toolConfig.functionCallingConfig``.

    A string or list here raises AttributeError on ``.get``, 500ing the request.
    """
    tool_config = req.get("toolConfig")
    if not isinstance(tool_config, dict):
        return {}
    fc = tool_config.get("functionCallingConfig")
    return fc if isinstance(fc, dict) else {}


def _google_tool_choice_instruction(req: dict) -> str:
    config = _google_function_calling_config(req)
    mode = config.get("mode", "AUTO")
    allowed = config.get("allowedFunctionNames") or []
    if mode == "NONE":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if mode == "ANY":
        if allowed:
            names = ", ".join(f'"{n}"' for n in allowed)
            return f"\n\nIMPORTANT: You MUST call one of these tools: {names}. Do not respond with text only."
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    return ""


def _google_tool_preamble(req: dict) -> str:
    tools = req.get("tools")
    if not tools:
        return ""
    if _google_function_calling_config(req).get("mode") == "NONE":
        return ""

    specs: list[dict[str, Any]] = []
    # Google's schema says `tools` is an array. A dict-shaped `tools` means the
    # client sent a single group unwrapped -- tolerate it rather than dropping
    # every declaration, which would return HTTP 200 with no tools advertised
    # and read as "the model chose not to call".
    groups = tools if isinstance(tools, list) else [tools]
    for group in groups:
        if not isinstance(group, dict):
            continue
        declarations = group.get("functionDeclarations")
        if isinstance(declarations, dict):
            declarations = [declarations]
        if not isinstance(declarations, list):
            # A string here means the client misunderstood the schema. Skipping
            # it silently returns HTTP 200 with no tools advertised, which reads
            # as "the model chose not to call" rather than a malformed request.
            continue
        for fn in declarations:
            if not isinstance(fn, dict):
                continue
            spec = {"name": fn.get("name", ""), "description": _as_text(fn.get("description"))}
            params = fn.get("parameters") or fn.get("parametersJsonSchema")
            if params:
                spec["parameters"] = params
            specs.append(spec)
    specs = [s for s in specs if s["name"]]
    if not specs:
        return ""

    return (
        "# Tool Use\n\n"
        "You can call the following tools to help accomplish tasks. "
        "These tools connect to the user's local environment and will execute when called.\n\n"
        "Call format (use this exact format):\n"
        f'{FUNCTION_CALL_FENCE}\n{{"name": "<tool_name>", "args": {{<arguments>}}}}\n```\n\n'
        "When calling tools:\n"
        "- Output ONLY the function_call block(s), nothing else\n"
        "- You may call multiple tools with multiple blocks\n"
        "- After receiving a [Tool result for ...], use that data to answer the user\n\n"
        f"Available tools:\n{json.dumps(specs, indent=2, ensure_ascii=False)}"
        f"{_google_tool_choice_instruction(req)}"
    )


def google_contents_to_prompt(req: dict) -> tuple[str, list[ImageRef]]:
    """Flatten Google ``contents`` / ``systemInstruction`` into (prompt, images)."""
    parts: list[str] = []
    images: list[ImageRef] = []

    system = req.get("systemInstruction")
    if not isinstance(system, dict):
        # Tolerate a string or list here too: Google's spec says an object, but
        # a bare string systemInstruction 500s on .get().
        system = {"parts": [{"text": _as_text(system)}]} if system else {}
    system_text = " ".join(
        _as_text(p.get("text", ""))
        for p in system.get("parts") or []
        if isinstance(p, dict) and p.get("text")
    )
    preamble = _google_tool_preamble(req)
    if system_text:
        parts.append(f"{system_text}\n\n{preamble}" if preamble else system_text)
    elif preamble:
        parts.append(preamble)

    for content in req.get("contents") or []:
        if not isinstance(content, dict):
            continue
        role = content.get("role", "user")
        chunks: list[str] = []
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("text"):
                chunks.append(part["text"])
            elif part.get("inlineData"):
                inline = part["inlineData"] or {}
                try:
                    data = base64.b64decode(inline["data"], validate=True)
                except (KeyError, ValueError, TypeError, binascii.Error):
                    continue
                images.append((data, inline.get("mimeType", "image/png")))
                chunks.append("[Image attached]")
            elif part.get("functionCall"):
                fc = part["functionCall"]
                if isinstance(fc, dict):
                    chunks.append(
                        f"{FUNCTION_CALL_FENCE}\n"
                        f"{json.dumps({'name': fc.get('name'), 'args': fc.get('args', {})}, ensure_ascii=False)}\n"
                        "```"
                    )
            elif part.get("functionResponse"):
                fr = part["functionResponse"]
                if isinstance(fr, dict):
                    chunks.append(
                        f"[Tool result for {fr.get('name', '')}]: "
                        f"{json.dumps(fr.get('response', {}), ensure_ascii=False)}"
                    )
        text = "\n".join(chunks)
        parts.append(f"[Assistant]: {text}" if role == "model" else text)

    return "\n\n".join(p for p in parts if p), images


# ─── Reply parsing ────────────────────────────────────────────────────────────


def parse_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Extract OpenAI ``tool_calls`` from fenced ``tool_call`` blocks.

    ``name`` is required to be a string by the OpenAI schema, and ``arguments``
    must be a JSON-encoded string. A model sometimes emits ``"name": null`` or
    omits the name entirely; those blocks are dropped rather than forwarded,
    because a strict client rejects the whole completion over one malformed
    tool call.
    """
    calls: list[dict] = []
    kept: list[str] = []
    cursor = 0
    pattern = re.compile(r"```tool_call\s*\n(.*?)\n```", re.DOTALL)
    for match in pattern.finditer(text):
        kept.append(text[cursor : match.start()])
        cursor = match.end()
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = data.get("arguments", {})
        calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )
    kept.append(text[cursor:])
    return "".join(kept).strip(), calls


def _balanced_json(text: str, start: int) -> str | None:
    """Return the balanced ``{...}`` substring beginning at ``text[start]``.

    Brace matching is done by counting while respecting string literals, so a
    ``}`` inside a JSON string does not close the object. Returns None when the
    text does not begin with ``{`` or the braces never balance.

    A regex like ``\\{[^`]*?\\}`` is not sufficient here: it stops at the first
    ``}``, which truncates any nested object such as
    ``{"name": "x", "args": {"city": "Lima"}}`` and makes the whole call fail
    to parse.
    """
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _parse_call_object(raw: str) -> dict[str, Any] | None:
    """Parse a function-call object, or None if it is not a well-formed call.

    A model occasionally emits ``"args": null``. Passing that through yields
    ``args: None``, which serialises as JSON ``null`` where Google expects an
    object. Coerce it to ``{}`` here so the emitted ``functionCall`` part is
    always schema-valid.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict) and "name" in data:
        args = data.get("args", data.get("arguments", {}))
        if not isinstance(args, dict):
            args = {}
        return {"name": data["name"], "args": args}
    return None


def parse_function_calls(text: str) -> tuple[str, list[dict]]:
    """Extract Google ``functionCall`` parts from fenced ``function_call`` blocks.

    Handles three shapes a model may emit: a proper fence, the fence keyword
    without backticks, and bare JSON when the whole reply is the call.

    Scanning works on a shrinking buffer: each matched call is cut out before
    the search continues, so two calls in a row are both found and neither
    leaks into the returned prose. Stripping fences must happen per-match
    rather than up front -- removing all backticks first desynchronises the
    offsets and drops every call after the first.
    """
    calls: list[dict] = []
    remaining = text

    # Shapes 1 and 2: with or without backticks. Find the keyword, take the
    # balanced object after it, then cut the whole span out of the buffer.
    fence = re.compile(r"(?:^|\n)\s*(?:```\s*)?function_call\s*\n?")
    search_from = 0
    while True:
        match = fence.search(remaining, search_from)
        if not match:
            break
        payload = _balanced_json(remaining, match.end())
        parsed = _parse_call_object(payload) if payload else None
        if payload is None or parsed is None:
            search_from = match.end()
            continue
        calls.append(parsed)
        # Cut from the match start through the end of the JSON object. Any
        # closing fence that follows is removed by the regex on the next pass
        # or by the final cleanup below.
        remaining = remaining[: match.start()] + remaining[match.end() + len(payload) :]
        search_from = match.start()

    # Remove any fence markers left behind by the spans we cut out.
    remaining = re.sub(r"```\s*", "", remaining).strip()

    # Shape 3: the entire reply is the call.
    if not calls and remaining.startswith("{"):
        payload = _balanced_json(remaining, 0)
        if payload is not None:
            parsed = _parse_call_object(payload)
            if parsed:
                calls.append(parsed)
                remaining = remaining[len(payload) :].strip()

    return remaining, calls

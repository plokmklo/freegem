"""MCP face: expose Gemini as agent-callable tools.

Each tool is a thin, opinionated wrapper over :func:`gemini.generate`:

* ``gemini_search``   -- web-grounded, must cite, must not guess
* ``gemini_ask``      -- fast direct answer, no search, no reasoning
* ``gemini_deep_think`` -- long-form reasoning on hard problems
* ``gemini_advisor``  -- structured second opinion on a decision
* ``gemini_chat``     -- raw multi-turn, full control of message shape
* ``gemini_list_models`` -- registry introspection

The ``mcp`` package is an optional dependency: this module imports fine
without it, and only fails when :func:`build_server` is called.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .config import BridgeConfig
from .gemini import BridgeError, generate
from .models import MODELS, resolve_model

__all__ = ["TOOL_SPECS", "build_server", "run_tool"]

# Default model per tool. Any tool also accepts an explicit `model` override.
DEFAULTS = {
    "search": "gemini-3.6-flash",
    "ask": "gemini-3.6-flash",
    "think": "gemini-3.5-flash-thinking",
    "advisor": "gemini-3.5-flash-thinking",
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "gemini_search",
        "summary": "Web-grounded search with sources",
        "description": (
            "Answer a question using current information from the web, with "
            "sources listed. Use as the default tool for news, current facts, "
            "documentation, and prices. Prefer it over gemini_ask whenever the "
            "answer must be current or verifiable."
        ),
    },
    {
        "name": "gemini_ask",
        "summary": "Fast direct answer, no web",
        "description": (
            "Quick factual answer or explanation. No web grounding and no long "
            "reasoning. Use for definitions, short explanations, and 'what is X' "
            "lookups. For current facts use gemini_search; for hard multi-step "
            "problems use gemini_deep_think."
        ),
    },
    {
        "name": "gemini_deep_think",
        "summary": "Deep reasoning, long output",
        "description": (
            "Reason carefully through a hard or ambiguous problem. Long-form "
            "output using the extended-thinking model. Use for analysis, design "
            "tradeoffs, math and code reasoning, and root-cause hypotheses. "
            "Slower than gemini_ask."
        ),
    },
    {
        "name": "gemini_advisor",
        "summary": "Structured second opinion",
        "description": (
            "Independent second opinion on a decision, returned as "
            "Recommendation / Options / Tradeoffs / Risks / What would change my "
            "mind. Use when you want a skeptical outside view on an approach. "
            "Not for simple lookups or facts."
        ),
    },
    {
        "name": "gemini_chat",
        "summary": "Raw multi-turn chat",
        "description": (
            "Send a full message history for maximum control over the "
            "conversation shape, few-shot examples, or system prompts. Prefer "
            "the purpose-built tools when they fit."
        ),
    },
    {
        "name": "gemini_list_models",
        "summary": "List available models",
        "description": "List the Gemini model ids available, with descriptions.",
    },
]


def _run(cfg: BridgeConfig, prompt: str, model: str | None = None) -> str:
    """Resolve a model name, run one generation, and normalise failures."""
    _name, mode_id, think_mode, err, extra = resolve_model(
        model or cfg.default_model, cfg.default_model
    )
    if err:
        raise ValueError(err)
    # resolve_model's contract: no error means every field is populated.
    # Its Optional return type cannot express that, so guard explicitly --
    # a contract violation here must fail loudly, not as int(None).
    assert mode_id is not None and think_mode is not None, "resolve_model broke its contract"
    text = generate(cfg, prompt, int(mode_id), int(think_mode), None, extra)
    if not text:
        raise BridgeError(
            "Gemini returned nothing. Likely a rate limit, a stale build tag, "
            "or an expired session cookie."
        )
    return text


def run_tool(cfg: BridgeConfig, name: str, arguments: dict[str, Any]) -> str:
    """Execute one tool by name. Pure dispatch, usable without the mcp package."""
    arguments = arguments or {}

    if name == "gemini_search":
        recency = arguments.get("recency")
        constraint = f"\nLimit the information to this timeframe: {recency}." if recency else ""
        prompt = (
            "Search the web and answer the question below using current, verifiable "
            "information. Rules:\n"
            "- Base the answer on what you find, not on memory alone.\n"
            "- Cite sources inline as markdown links.\n"
            "- End with a 'Sources:' list of the URLs you used.\n"
            "- If you cannot find reliable information, say so plainly instead of "
            "guessing.\n"
            f"{constraint}\n\nQuestion: {arguments.get('query', '')}"
        )
        return _run(cfg, prompt, arguments.get("model") or DEFAULTS["search"])

    if name == "gemini_ask":
        system = arguments.get("system")
        question = arguments.get("question", "")
        prompt = f"{system}\n\n{question}" if system else question
        return _run(cfg, prompt, arguments.get("model") or DEFAULTS["ask"])

    if name == "gemini_deep_think":
        return _run(cfg, arguments.get("problem", ""), arguments.get("model") or DEFAULTS["think"])

    if name == "gemini_advisor":
        context = arguments.get("context")
        constraints = arguments.get("constraints")
        prompt = (
            "You are an independent senior advisor. Give a decision-grade analysis "
            "of the question below. Be direct and skeptical; do not hedge or pad. "
            "Structure your response exactly as:\n\n"
            "## Recommendation\nOne paragraph stating the recommended course of action.\n\n"
            "## Options\n2-4 concrete options, each with a one-line description.\n\n"
            "## Tradeoffs\nFor each option: what it buys, what it costs, and when it "
            "is the right choice.\n\n"
            "## Risks\nWhat could go wrong, ranked by likelihood and impact, with a "
            "mitigation for each.\n\n"
            "## What would change my mind\nThe evidence or condition that would flip "
            "the recommendation.\n\n"
            f"Question: {arguments.get('topic', '')}"
        )
        if context:
            prompt += f"\n\nContext: {context}"
        if constraints:
            prompt += f"\n\nConstraints: {constraints}"
        return _run(cfg, prompt, arguments.get("model") or DEFAULTS["advisor"])

    if name == "gemini_chat":
        messages = arguments.get("messages") or []
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        rendered: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            if role == "system":
                rendered.append(f"[System instruction]: {content}")
            elif role == "assistant":
                rendered.append(f"[Assistant]: {content}")
            else:
                rendered.append(str(content))
        return _run(cfg, "\n\n".join(p for p in rendered if p), arguments.get("model"))

    if name == "gemini_list_models":
        lines = ["Available models:", ""]
        for model_name, spec in MODELS.items():
            suffix = " (needs a session cookie for real routing)" if spec.requires_cookie else ""
            lines.append(f"- {model_name}: {spec.desc}{suffix}")
        lines += [
            "",
            "Thinking depth override: append @think=N (0 = deepest, 4 = auto).",
            "",
            f"Default model: {cfg.default_model}",
            f"Session: {'cookie configured' if cfg.has_session_cookie() else 'none (anonymous)'}",
            f"Proxy: {cfg.proxy or 'system env'}",
        ]
        return "\n".join(lines)

    raise ValueError(f"unknown tool: {name}")


def build_server(cfg: BridgeConfig, name: str = "gemini-bridge") -> Any:
    """Create a FastMCP server wired to this config.

    Raises ImportError with an actionable message if the optional ``mcp``
    dependency is missing.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - depends on install
        raise ImportError(
            "the 'mcp' package is required for the MCP face. "
            "Install it with:  pip install 'gemini-bridge[mcp]'"
        ) from exc

    server = FastMCP(name)
    # FastMCP exposes no version= kwarg, and the low-level server reads
    # ``_mcp_server.version`` when building serverInfo -- left as None, the
    # SDK reports *its own* package version (e.g. "1.28.1") as ours, so every
    # MCP client would log the wrong gemini-bridge version. Pin ours.
    server._mcp_server.version = __version__

    @server.tool()
    def gemini_search(query: str, recency: str | None = None, model: str | None = None) -> str:
        """Web-grounded search. Answers a question using current internet
        information, with sources listed.

        Use this as the default search tool for news, facts, docs, prices, and
        anything that changes over time. Prefer it over plain Q&A when the answer
        must be current or verifiable.

        Args:
            query: What to search for (a question or a topic works best).
            recency: Optional time constraint, e.g. "today", "this week",
                     "past month", "2026". Omit for no constraint.
            model: Optional Gemini model id override.
        """
        return run_tool(cfg, "gemini_search", {"query": query, "recency": recency, "model": model})

    @server.tool()
    def gemini_ask(question: str, system: str | None = None, model: str | None = None) -> str:
        """Fast, direct answer or explanation. No web grounding, no long reasoning.

        Use for quick factual questions, definitions, short explanations, and
        "what is X" style lookups. For current facts use gemini_search; for hard
        multi-step problems use gemini_deep_think.

        Args:
            question: The question to answer.
            system: Optional system instruction to steer tone or role.
            model: Optional Gemini model id override.
        """
        return run_tool(cfg, "gemini_ask", {"question": question, "system": system, "model": model})

    @server.tool()
    def gemini_deep_think(problem: str, model: str | None = None) -> str:
        """Deep reasoning on a hard or ambiguous problem. Long-form output using
        the extended-thinking model.

        Use for analysis, design tradeoffs, math/code reasoning, root-cause
        hypotheses, and multi-step problems. Slower than gemini_ask.

        Args:
            problem: The problem to reason through. Include relevant context.
            model: Optional Gemini model id override (e.g. ...@think=2 for a
                   shallower pass; @think=0 is deepest, the default).
        """
        return run_tool(cfg, "gemini_deep_think", {"problem": problem, "model": model})

    @server.tool()
    def gemini_advisor(
        topic: str,
        context: str | None = None,
        constraints: str | None = None,
        model: str | None = None,
    ) -> str:
        """Independent second opinion on a decision, with structured output:
        options, tradeoffs, a concrete recommendation, and risks.

        Use when you want a skeptical outside view on an approach — architecture,
        product, process, strategy, research direction, or a hard tradeoff. Not
        for simple lookups (use gemini_ask) or facts (use gemini_search).

        Args:
            topic: The decision or question to advise on.
            context: Relevant background, what has been tried, current state.
            constraints: Limits to respect (time, budget, stack, team size, policy).
            model: Optional Gemini model id override.
        """
        return run_tool(
            cfg,
            "gemini_advisor",
            {"topic": topic, "context": context, "constraints": constraints, "model": model},
        )

    @server.tool()
    def gemini_chat(messages: list, model: str | None = None) -> str:
        """Raw multi-turn chat. Send a full message history.

        Use when you need control over the conversation shape, few-shot examples,
        or a system prompt. Otherwise prefer gemini_search / gemini_ask /
        gemini_deep_think / gemini_advisor.

        Args:
            messages: List of {"role": "system"|"user"|"assistant",
                      "content": "..."} dicts, in order.
            model: Optional Gemini model id override.
        """
        return run_tool(cfg, "gemini_chat", {"messages": messages, "model": model})

    @server.tool()
    def gemini_list_models() -> str:
        """List the Gemini model ids this server exposes, with descriptions.

        Append @think=0 (deepest) to @think=4 (shallowest) to any model id to
        override its thinking depth.
        """
        return run_tool(cfg, "gemini_list_models", {})

    return server


def model_help(cfg: BridgeConfig) -> str:
    """Text for the CLI's model listing, including session status."""
    lines = [f"gemini-bridge v{__version__}", ""]
    for model_name, spec in MODELS.items():
        lines.append(
            f"  {model_name:<30} {spec.desc}{' [needs cookie]' if spec.requires_cookie else ''}"
        )
    lines += [
        "",
        f"  default: {cfg.default_model}",
        f"  session: {'cookie configured' if cfg.has_session_cookie() else 'anonymous'}",
        f"  build tag: {cfg.gemini_bl}",
    ]
    return "\n".join(lines)

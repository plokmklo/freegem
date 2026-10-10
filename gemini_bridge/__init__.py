"""gemini-bridge: free Gemini access, shaped as the APIs you already use.

One bridge, three faces:

* an OpenAI-compatible API server (``/v1/chat/completions``, ``/v1/responses``)
* a Google-native API server (``/v1beta/models/...``)
* an MCP server exposing Gemini as agent-callable tools

Backed by the same free Gemini web session your browser uses.
"""

__version__ = "2.0.0"

__all__ = ["__version__"]

"""Regression guard for the MCP tool descriptions agents see (issue #73).

The defect class: a tool whose call REQUIRES the caller to supply pages up
front, advertised with a description that read as though it found pages
itself. An agent that picked the tool from "tools/list" and called it the
obvious way got a validation error instead of research.

The shipped fix rewrote one docstring. These tests keep the whole surface
honest mechanically: this module parses mcp_tools with ast (no "mcp" install
needed) and fails CI whenever an advertised description hides what the call
requires. FastMCP sends the wrapped functions' docstrings over "tools/list",
so those docstrings ARE the product surface an agent plans against.
"""

import ast
import inspect
import unittest

import freegem.mcp_tools as mcp_tools

EXPECTED_TOOLS = [
    "freegem_get_artifact",
    "freegem_get_job",
    "freegem_manage_profile",
    "freegem_submit_job",
    "gemini_advisor",
    "gemini_ask",
    "gemini_chat",
    "gemini_deep_think",
    "gemini_list_models",
    "gemini_search",
    "web_research",
    "web_scrape",
    "web_verify",
]


def _tool_functions() -> list[ast.FunctionDef]:
    """Every function inside build_server decorated with @server.tool()."""
    tree = ast.parse(inspect.getsource(mcp_tools))
    build: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_server":
            build = node
            break
    assert build is not None, "build_server not found in mcp_tools"
    tools: list[ast.FunctionDef] = []
    for node in build.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if _is_tool_decorator(decorator):
                tools.append(node)
    return tools


def _is_tool_decorator(decorator: ast.expr) -> bool:
    """The @server.tool() marker FastMCP registers a function with."""
    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "tool"
    )


def _required_params(fn: ast.FunctionDef) -> list[ast.arg]:
    """The parameters a caller must pass: positional args without defaults."""
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    first_optional = len(positional) - len(args.defaults)
    required = positional[:first_optional]
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        if default is None:
            required.append(arg)
    return required


def _is_url_list(arg: ast.arg) -> bool:
    """A required parameter annotated as a bare list whose name has "url"."""
    annotation = arg.annotation
    return "url" in arg.arg and isinstance(annotation, ast.Name) and annotation.id == "list"


def _url_list_params(params: list[ast.arg]) -> list[ast.arg]:
    return [arg for arg in params if _is_url_list(arg)]


class ToolDescriptionGuardTests(unittest.TestCase):
    """Advertised descriptions must not hide what a call requires."""

    def test_the_guard_sees_the_full_tool_surface(self) -> None:
        names = sorted(fn.name for fn in _tool_functions())
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_every_tool_has_a_description(self) -> None:
        for fn in _tool_functions():
            with self.subTest(tool=fn.name):
                self.assertTrue(ast.get_docstring(fn))

    def test_required_arguments_are_named_in_the_description(self) -> None:
        for fn in _tool_functions():
            doc = ast.get_docstring(fn) or ""
            for arg in _required_params(fn):
                with self.subTest(tool=fn.name, param=arg.arg):
                    self.assertIn(arg.arg, doc)

    def test_url_list_tools_lead_with_the_requirement(self) -> None:
        for fn in _tool_functions():
            url_params = _url_list_params(_required_params(fn))
            if not url_params:
                continue
            doc = ast.get_docstring(fn) or ""
            first_paragraph = doc.split("\n\n")[0]
            for arg in url_params:
                with self.subTest(tool=fn.name, param=arg.arg):
                    self.assertIn(arg.arg, first_paragraph)

    def test_web_research_description_is_the_issue_73_fix(self) -> None:
        fn = next(f for f in _tool_functions() if f.name == "web_research")
        first_paragraph = (ast.get_docstring(fn) or "").split("\n\n")[0]
        self.assertIn("Research pages YOU provide", first_paragraph)
        self.assertIn("urls", first_paragraph)
        self.assertIn("does not search the web", first_paragraph)

    def test_tool_specs_registry_matches_the_advertised_lead(self) -> None:
        spec = next(s for s in mcp_tools.TOOL_SPECS if s["name"] == "web_research")
        description = str(spec["description"])
        self.assertIn("Research pages YOU provide", description)
        self.assertIn("does not search the web", description)

"""Tests for the pure protocol-translation layer.

No network, no config, no mocks beyond the data. If a test here needs a
fixture directory, the logic probably belongs in a caller.
"""

import unittest

from freegem.tools import (
    google_contents_to_prompt,
    messages_to_prompt,
    parse_function_calls,
    parse_tool_calls,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


class OpenAIMessageTests(unittest.TestCase):
    def test_plain_message(self) -> None:
        prompt, images = messages_to_prompt([{"role": "user", "content": "hello"}])
        self.assertEqual(prompt, "hello")
        self.assertEqual(images, [])

    def test_system_role_marked(self) -> None:
        prompt, _ = messages_to_prompt(
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ]
        )
        self.assertIn("[System instruction]: be terse", prompt)

    def test_history_is_concatenated_in_order(self) -> None:
        prompt, _ = messages_to_prompt(
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ]
        )
        self.assertEqual(prompt, "first\n\n[Assistant]: second\n\nthird")

    def test_tool_result_role(self) -> None:
        prompt, _ = messages_to_prompt(
            [{"role": "tool", "name": "get_weather", "content": "18C and rainy"}]
        )
        self.assertIn("[Tool result for get_weather]: 18C and rainy", prompt)

    def test_assistant_tool_calls_rendered_as_fences(self) -> None:
        prompt, _ = messages_to_prompt(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'},
                        }
                    ],
                }
            ]
        )
        self.assertIn("```tool_call", prompt)
        self.assertIn('"name": "get_weather"', prompt)
        self.assertIn('"city": "Oslo"', prompt)

    def test_empty_message_list(self) -> None:
        prompt, images = messages_to_prompt([])
        self.assertEqual(prompt, "")
        self.assertEqual(images, [])

    def test_non_dict_messages_skipped(self) -> None:
        prompt, _ = messages_to_prompt(["not a dict", {"role": "user", "content": "ok"}])
        self.assertEqual(prompt, "ok")

    def test_no_prompt_content_is_falsy(self) -> None:
        prompt, _ = messages_to_prompt([{"role": "user", "content": ""}])
        self.assertFalse(prompt.strip())


class ToolPreambleTests(unittest.TestCase):
    def test_tools_prepended(self) -> None:
        prompt, _ = messages_to_prompt(
            [{"role": "user", "content": "weather in Tokyo?"}], TOOLS, "auto"
        )
        self.assertIn("# Tool Use", prompt)
        self.assertIn("get_weather", prompt)
        self.assertIn("weather in Tokyo?", prompt)

    def test_tool_choice_none_omits_preamble(self) -> None:
        prompt, _ = messages_to_prompt([{"role": "user", "content": "hi"}], TOOLS, "none")
        self.assertNotIn("# Tool Use", prompt)

    def test_no_tools_omits_preamble(self) -> None:
        prompt, _ = messages_to_prompt([{"role": "user", "content": "hi"}], None, "auto")
        self.assertNotIn("# Tool Use", prompt)

    def test_required_choice(self) -> None:
        prompt, _ = messages_to_prompt([{"role": "user", "content": "hi"}], TOOLS, "required")
        self.assertIn("You MUST call at least one tool", prompt)

    def test_named_tool_choice(self) -> None:
        choice = {"type": "function", "function": {"name": "get_weather"}}
        prompt, _ = messages_to_prompt([{"role": "user", "content": "hi"}], TOOLS, choice)
        self.assertIn('You MUST call the tool "get_weather"', prompt)

    def test_malformed_tool_entries_dropped(self) -> None:
        broken = [{"type": "function", "function": {"description": "no name"}}]
        prompt, _ = messages_to_prompt([{"role": "user", "content": "hi"}], broken, "auto")
        self.assertNotIn("# Tool Use", prompt)


class ToolCallParsingTests(unittest.TestCase):
    def test_parses_single_call(self) -> None:
        text = (
            "Let me check.\n"
            '```tool_call\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n```'
        )
        clean, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertIn("Paris", calls[0]["function"]["arguments"])
        self.assertEqual(clean, "Let me check.")
        self.assertTrue(calls[0]["id"].startswith("call_"))

    def test_parses_multiple_calls(self) -> None:
        text = (
            '```tool_call\n{"name": "a", "arguments": {}}\n```\n'
            '```tool_call\n{"name": "b", "arguments": {}}\n```'
        )
        _, calls = parse_tool_calls(text)
        self.assertEqual([c["function"]["name"] for c in calls], ["a", "b"])

    def test_no_calls_leaves_text_intact(self) -> None:
        text = "Just a normal answer."
        clean, calls = parse_tool_calls(text)
        self.assertEqual(calls, [])
        self.assertEqual(clean, text)

    def test_malformed_json_ignored_not_raised(self) -> None:
        text = "```tool_call\n{not json at all}\n```"
        clean, calls = parse_tool_calls(text)
        self.assertEqual(calls, [])
        self.assertNotIn("tool_call", clean)

    def test_missing_name_key_ignored(self) -> None:
        text = '```tool_call\n{"arguments": {"city": "Rome"}}\n```'
        _, calls = parse_tool_calls(text)
        self.assertEqual(calls, [])

    def test_empty_arguments_default_to_object(self) -> None:
        text = '```tool_call\n{"name": "ping"}\n```'
        _, calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["function"]["arguments"], "{}")

    def test_unicode_arguments_preserved(self) -> None:
        text = '```tool_call\n{"name": "greet", "arguments": {"city": "北京"}}\n```'
        _, calls = parse_tool_calls(text)
        self.assertIn("北京", calls[0]["function"]["arguments"])


class GoogleNativeTests(unittest.TestCase):
    def test_contents_flattened(self) -> None:
        prompt, images = google_contents_to_prompt(
            {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
        )
        self.assertEqual(prompt, "hello")
        self.assertEqual(images, [])

    def test_model_role_marked(self) -> None:
        prompt, _ = google_contents_to_prompt(
            {"contents": [{"role": "model", "parts": [{"text": "hi"}]}]}
        )
        self.assertIn("[Assistant]: hi", prompt)

    def test_system_instruction(self) -> None:
        prompt, _ = google_contents_to_prompt(
            {
                "systemInstruction": {"parts": [{"text": "be brief"}]},
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            }
        )
        self.assertEqual(prompt, "be brief\n\nhi")

    def test_inline_image_extracted(self) -> None:
        import base64

        payload = base64.b64encode(b"fakepng").decode()
        prompt, images = google_contents_to_prompt(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "describe"},
                            {"inlineData": {"mimeType": "image/png", "data": payload}},
                        ],
                    }
                ]
            }
        )
        self.assertIn("[Image attached]", prompt)
        self.assertEqual(images, [(b"fakepng", "image/png")])

    def test_malformed_inline_image_dropped(self) -> None:
        prompt, images = google_contents_to_prompt(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "describe"},
                            {"inlineData": {"mimeType": "image/png", "data": "%%%"}},
                        ],
                    }
                ]
            }
        )
        self.assertNotIn("[Image attached]", prompt)
        self.assertEqual(images, [])

    def test_tools_prepended_with_google_fence(self) -> None:
        req = {
            "tools": [
                {
                    "functionDeclarations": [
                        {"name": "get_weather", "description": "weather", "parameters": {}}
                    ]
                }
            ],
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        }
        prompt, _ = google_contents_to_prompt(req)
        self.assertIn("```function_call", prompt)
        self.assertIn("get_weather", prompt)

    def test_tool_config_none_suppresses_tools(self) -> None:
        req = {
            "tools": [
                {"functionDeclarations": [{"name": "get_weather", "description": "weather"}]}
            ],
            "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        }
        prompt, _ = google_contents_to_prompt(req)
        self.assertNotIn("function_call", prompt)

    def test_empty_request(self) -> None:
        prompt, images = google_contents_to_prompt({})
        self.assertEqual(prompt, "")
        self.assertEqual(images, [])

    def test_non_dict_content_skipped(self) -> None:
        prompt, _ = google_contents_to_prompt(
            {"contents": ["nope", {"role": "user", "parts": [{"text": "ok"}]}]}
        )
        self.assertEqual(prompt, "ok")


class FunctionCallParsingTests(unittest.TestCase):
    """Three shapes a model may emit; all must parse."""

    def test_fenced_call(self) -> None:
        text = 'Sure.\n```function_call\n{"name": "get_weather", "args": {"city": "Lima"}}\n```'
        clean, calls = parse_function_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["args"]["city"], "Lima")
        self.assertEqual(clean, "Sure.")

    def test_fence_without_backticks(self) -> None:
        text = 'function_call\n{"name": "ping", "args": {}}'
        _, calls = parse_function_calls(text)
        self.assertEqual([c["name"] for c in calls], ["ping"])

    def test_bare_json_whole_reply(self) -> None:
        text = '{"name": "ping", "args": {"x": 1}}'
        clean, calls = parse_function_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"], {"x": 1})
        self.assertEqual(clean, "")

    def test_arguments_key_accepted_as_alias(self) -> None:
        text = '```function_call\n{"name": "ping", "arguments": {"a": 1}}\n```'
        _, calls = parse_function_calls(text)
        self.assertEqual(calls[0]["args"], {"a": 1})

    def test_no_calls_returns_text_unchanged(self) -> None:
        text = "Nothing to call here."
        clean, calls = parse_function_calls(text)
        self.assertEqual(calls, [])
        self.assertEqual(clean, text)

    def test_bare_json_without_name_is_not_a_call(self) -> None:
        text = '{"foo": "bar"}'
        clean, calls = parse_function_calls(text)
        self.assertEqual(calls, [])
        self.assertEqual(clean, text)

    def test_unicode_in_args(self) -> None:
        text = '```function_call\n{"name": "t", "args": {"city": "東京"}}\n```'
        _, calls = parse_function_calls(text)
        self.assertEqual(calls[0]["args"]["city"], "東京")


class RoundTripTests(unittest.TestCase):
    """A call we render into a prompt must parse back out of a reply."""

    def test_openai_round_trip(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Cairo"}'},
                    }
                ],
            }
        ]
        prompt, _ = messages_to_prompt(messages, TOOLS, "auto")
        # simulate the model echoing the same fence back
        reply = prompt.split("```tool_call", 1)[1]
        reply = "```tool_call" + reply
        _, calls = parse_tool_calls(reply)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertIn("Cairo", calls[0]["function"]["arguments"])

    def test_google_round_trip(self) -> None:
        """A fence the model emits must parse back to the same call."""
        fence = "```"
        prompt_with_fence = (
            f'{fence}function_call\n{{"name": "get_weather", "args": {{"city": "Cairo"}}}}\n{fence}'
        )
        _, calls = parse_function_calls(prompt_with_fence)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["args"]["city"], "Cairo")

    def test_nested_args_survive(self) -> None:
        """A shallow regex truncates nested objects; the parser must not."""
        fence = "```"
        reply = (
            f"{fence}function_call\n"
            '{"name": "search", "args": {"q": "x", "filters": {"lang": "en", "n": 5}}}\n'
            f"{fence}"
        )
        _, calls = parse_function_calls(reply)
        self.assertEqual(calls[0]["args"]["filters"], {"lang": "en", "n": 5})

    def test_two_consecutive_calls_both_parsed(self) -> None:
        fence = "```"
        reply = (
            f"{fence}function_call\n"
            '{"name": "a", "args": {}}\n'
            f"{fence}\n"
            f"{fence}function_call\n"
            '{"name": "b", "args": {}}\n'
            f"{fence}"
        )
        clean, calls = parse_function_calls(reply)
        self.assertEqual([c["name"] for c in calls], ["a", "b"])
        self.assertNotIn("function_call", clean)

    def test_brace_inside_string_value(self) -> None:
        """A literal } in a JSON string must not close the object early."""
        reply = '```function_call\n{"name": "s", "args": {"note": "a}b{c"}}\n```'
        _, calls = parse_function_calls(reply)
        self.assertEqual(calls[0]["args"]["note"], "a}b{c")


if __name__ == "__main__":
    unittest.main()

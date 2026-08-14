from __future__ import annotations

import unittest
from unittest.mock import patch

from providers import registry as providers
from providers.base import ProviderError
from providers.registry import Provider
from providers.openai_compatible import (
    OpenAICompatibleProvider,
    chat_result_from_openai,
)


def _provider(**overrides) -> OpenAICompatibleProvider:
    record = Provider(
        id="groq",
        name="Groq",
        type="openai-compatible",
        host="https://api.groq.com/openai/v1",
        api_key="sk-test",
        api_key_env="KARA_PROVIDER_GROQ_API_KEY",
        **overrides,
    )
    return OpenAICompatibleProvider(record)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class ResponseMappingTests(unittest.TestCase):
    def test_text_response_maps_with_usage(self) -> None:
        result = chat_result_from_openai(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 321, "completion_tokens": 12},
            }
        )
        self.assertEqual(result.content, "Hi there")
        self.assertEqual(result.usage.prompt_tokens, 321)
        self.assertEqual(result.usage.completion_tokens, 12)
        self.assertEqual(result.finish_reason, "stop")

    def test_tool_calls_map_and_parse_arguments(self) -> None:
        result = chat_result_from_openai(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {
                                        "name": "web_search",
                                        "arguments": '{"query": "kara"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
        self.assertTrue(result.wants_tools)
        self.assertEqual(result.tool_calls[0].id, "call_abc")
        self.assertEqual(result.tool_calls[0].arguments, {"query": "kara"})

    def test_missing_usage_is_zero(self) -> None:
        result = chat_result_from_openai(
            {"choices": [{"message": {"role": "assistant", "content": "x"}}]}
        )
        self.assertFalse(result.usage.reported)

    def test_an_error_payload_raises_with_its_message(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            chat_result_from_openai({"error": {"message": "model_not_found"}})
        self.assertIn("model_not_found", str(ctx.exception))

    def test_a_non_object_payload_raises(self) -> None:
        with self.assertRaises(ProviderError):
            chat_result_from_openai("nope")

    def test_empty_choices_raise(self) -> None:
        with self.assertRaises(ProviderError):
            chat_result_from_openai({"choices": []})


class RequestShapeTests(unittest.TestCase):
    def test_tool_results_carry_their_call_id(self) -> None:
        out = OpenAICompatibleProvider._request_messages(
            [{"role": "tool", "content": "r", "tool_call_id": "c1", "tool_name": "t"}]
        )
        self.assertEqual(out, [{"role": "tool", "content": "r", "tool_call_id": "c1"}])

    def test_assistant_tool_calls_are_preserved(self) -> None:
        calls = [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]
        out = OpenAICompatibleProvider._request_messages(
            [{"role": "assistant", "content": "", "tool_calls": calls}]
        )
        self.assertEqual(out[0]["tool_calls"], calls)
        self.assertIsNone(out[0]["content"])

    def test_plain_messages_pass_through(self) -> None:
        out = OpenAICompatibleProvider._request_messages(
            [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
        )
        self.assertEqual(out, [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
        ])


class TransportTests(unittest.TestCase):
    def test_chat_posts_to_chat_completions_with_tools(self) -> None:
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _Resp({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
        with patch("providers.openai_compatible.httpx.post", fake_post):
            result = _provider().chat("llama-3.3", [{"role": "user", "content": "hi"}], tools=tools)

        self.assertEqual(result.content, "ok")
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(captured["json"]["model"], "llama-3.3")
        self.assertEqual(captured["json"]["tools"], tools)
        self.assertFalse(captured["json"]["stream"])

    def test_rate_limits_are_marked_retryable(self) -> None:
        import httpx

        def fake_post(url, headers=None, json=None, timeout=None):
            request = httpx.Request("POST", url)
            response = httpx.Response(429, text="slow down", request=request)
            raise httpx.HTTPStatusError("429", request=request, response=response)

        with patch("providers.openai_compatible.httpx.post", fake_post):
            with self.assertRaises(ProviderError) as ctx:
                _provider().chat("m", [{"role": "user", "content": "hi"}])
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_client_errors_are_not_retryable(self) -> None:
        import httpx

        def fake_post(url, headers=None, json=None, timeout=None):
            request = httpx.Request("POST", url)
            response = httpx.Response(401, text="bad key", request=request)
            raise httpx.HTTPStatusError("401", request=request, response=response)

        with patch("providers.openai_compatible.httpx.post", fake_post):
            with self.assertRaises(ProviderError) as ctx:
                _provider().chat("m", [{"role": "user", "content": "hi"}])
        self.assertFalse(ctx.exception.retryable)

    def test_connection_failures_are_retryable(self) -> None:
        import httpx

        def fake_post(url, headers=None, json=None, timeout=None):
            raise httpx.ConnectError("refused")

        with patch("providers.openai_compatible.httpx.post", fake_post):
            with self.assertRaises(ProviderError) as ctx:
                _provider().chat("m", [{"role": "user", "content": "hi"}])
        self.assertTrue(ctx.exception.retryable)

    def test_a_local_server_needs_no_api_key(self) -> None:
        local = OpenAICompatibleProvider(
            Provider(
                id="lmstudio",
                name="LM Studio",
                type="openai-compatible",
                host="http://localhost:1234/v1",
                api_key=None,
                api_key_env=None,
            )
        )
        self.assertTrue(local.has_credentials)

    def test_list_models_reads_the_data_array(self) -> None:
        def fake_get(url, headers=None, timeout=None):
            return _Resp({"data": [{"id": "llama-3.3"}, {"id": "mixtral"}]})

        with patch("providers.openai_compatible.httpx.get", fake_get):
            self.assertEqual(_provider().list_models(), ["llama-3.3", "mixtral"])


class DiscoveryTests(unittest.TestCase):
    ENV = {
        "KARA_PROVIDER_GROQ_BASE_URL": "https://api.groq.com/openai/v1",
        "KARA_PROVIDER_GROQ_API_KEY": "sk-groq",
        "KARA_PROVIDER_GROQ_MODEL": "llama-3.3-70b",
        "KARA_PROVIDER_LMSTUDIO_BASE_URL": "http://localhost:1234/v1",
    }

    def _discover(self, env: dict) -> list[dict]:
        with patch.dict(providers.os.environ, env, clear=False):
            return providers._discover_openai_compatible_defs(set())

    def test_a_base_url_creates_a_provider(self) -> None:
        found = {d["id"]: d for d in self._discover(self.ENV)}
        self.assertIn("groq", found)
        self.assertEqual(found["groq"]["type"], "openai-compatible")
        self.assertEqual(found["groq"]["host"], "https://api.groq.com/openai/v1")
        self.assertEqual(found["groq"]["default_model"], "llama-3.3-70b")

    def test_a_keyless_local_backend_reports_no_key_env(self) -> None:
        found = {d["id"]: d for d in self._discover(self.ENV)}
        self.assertIsNone(found["lmstudio"]["api_key_env"])

    def test_existing_ids_are_not_shadowed(self) -> None:
        with patch.dict(providers.os.environ, self.ENV, clear=False):
            found = providers._discover_openai_compatible_defs({"groq"})
        self.assertNotIn("groq", {d["id"] for d in found})

    def test_a_blank_base_url_is_ignored(self) -> None:
        found = self._discover({"KARA_PROVIDER_EMPTY_BASE_URL": "   "})
        self.assertEqual([d for d in found if d["id"] == "empty"], [])

    def test_the_adapter_is_built_for_the_new_type(self) -> None:
        record = Provider(
            id="groq", name="Groq", type="openai-compatible",
            host="https://api.groq.com/openai/v1",
        )
        self.assertIsInstance(
            providers.to_chat_provider(record), OpenAICompatibleProvider
        )


if __name__ == "__main__":
    unittest.main()

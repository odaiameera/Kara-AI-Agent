"""Unit tests for OpenAI Codex OAuth support (no live network calls)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
from provider_base import ChatProvider, ProviderError
from providers import Provider, to_chat_provider


class TempBrainMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patches = [patch.object(config, "BRAIN_DIR", self.tmp_path)]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()


class TestCodexAuthStore(TempBrainMixin, unittest.TestCase):
    def test_save_and_read_tokens_from_brain_auth_json(self) -> None:
        import codex_auth

        codex_auth.save_tokens({"access_token": "access", "refresh_token": "refresh"})

        data = codex_auth.read_tokens()
        self.assertEqual(data["tokens"]["access_token"], "access")
        self.assertEqual(data["tokens"]["refresh_token"], "refresh")
        self.assertTrue((self.tmp_path / "auth.json").exists())

    def test_read_tokens_missing_raises_clear_error(self) -> None:
        import codex_auth

        with self.assertRaises(codex_auth.CodexAuthError) as ctx:
            codex_auth.read_tokens()
        self.assertIn("No OpenAI Codex credentials", str(ctx.exception))

    def test_refresh_posts_to_openai_token_endpoint_and_persists(self) -> None:
        import codex_auth

        codex_auth.save_tokens({"access_token": "old", "refresh_token": "refresh-old"})
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = None
        client.post.return_value = response

        with patch("codex_auth.httpx.Client", return_value=client):
            refreshed = codex_auth.refresh_tokens()

        self.assertEqual(refreshed["access_token"], "new-access")
        self.assertEqual(codex_auth.read_tokens()["tokens"]["refresh_token"], "new-refresh")
        args, kwargs = client.post.call_args
        self.assertEqual(args[0], codex_auth.CODEX_OAUTH_TOKEN_URL)
        self.assertEqual(kwargs["data"]["grant_type"], "refresh_token")


class TestCodexDeviceLogin(TempBrainMixin, unittest.TestCase):
    def test_device_login_uses_codex_device_endpoints(self) -> None:
        import codex_auth

        responses = []
        for payload in (
            {"user_code": "ABCD-EFGH", "device_auth_id": "dev-1", "interval": 0},
            {"authorization_code": "auth-code", "code_verifier": "verifier"},
            {"access_token": "access", "refresh_token": "refresh"},
        ):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = payload
            responses.append(r)

        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = None
        client.post.side_effect = responses

        with patch("codex_auth.httpx.Client", return_value=client), patch("codex_auth.time.sleep"):
            creds = codex_auth.device_login(print_fn=lambda _msg="": None)

        self.assertEqual(creds["tokens"]["access_token"], "access")
        called_urls = [call.args[0] for call in client.post.call_args_list]
        self.assertIn("https://auth.openai.com/api/accounts/deviceauth/usercode", called_urls)
        self.assertIn("https://auth.openai.com/api/accounts/deviceauth/token", called_urls)
        self.assertIn(codex_auth.CODEX_OAUTH_TOKEN_URL, called_urls)


class TestCodexProvider(TempBrainMixin, unittest.TestCase):
    def test_codex_provider_satisfies_protocol_after_tokens_saved(self) -> None:
        import codex_auth
        from providers_codex import OpenAICodexProvider

        codex_auth.save_tokens({"access_token": "access", "refresh_token": "refresh"})
        provider = OpenAICodexProvider(
            Provider(
                id="openai-codex",
                name="OpenAI Codex",
                type="openai-codex",
                host="https://chatgpt.com/backend-api/codex",
            )
        )

        self.assertIsInstance(provider, ChatProvider)
        self.assertTrue(provider.has_credentials)

    def test_provider_factory_supports_codex(self) -> None:
        provider = to_chat_provider(
            Provider(
                id="openai-codex",
                name="OpenAI Codex",
                type="openai-codex",
                host="https://chatgpt.com/backend-api/codex",
            )
        )
        self.assertEqual(provider.id, "openai-codex")

    def _provider(self):
        from providers_codex import OpenAICodexProvider

        return OpenAICodexProvider(
            Provider(
                id="openai-codex",
                name="OpenAI Codex",
                type="openai-codex",
                host="https://chatgpt.com/backend-api/codex",
            )
        )

    @staticmethod
    def _stream(lines: list[str]):
        response = MagicMock()
        response.status_code = 200
        response.iter_lines.return_value = iter(lines)
        stream = MagicMock()
        stream.__enter__.return_value = response
        stream.__exit__.return_value = None
        return stream

    def test_codex_chat_normalizes_streamed_text_response(self) -> None:
        provider = self._provider()
        stream = self._stream([
            'data: {"type":"response.output_text.delta","delta":"Hello"}',
            'data: {"type":"response.output_text.delta","delta":" Kara"}',
            'data: {"type":"response.completed","response":{"status":"completed"}}',
        ])
        creds = {"access_token": "not-a-jwt", "base_url": "https://chatgpt.com/backend-api/codex"}

        with patch("providers_codex.codex_auth.runtime_credentials", return_value=creds), patch(
            "providers_codex.httpx.stream", return_value=stream
        ) as mock_stream:
            result = provider.chat("gpt-5.6-terra", [{"role": "system", "content": "Be concise."}, {"role": "user", "content": "Hi"}])

        self.assertEqual(result["message"], {"role": "assistant", "content": "Hello Kara"})
        _, kwargs = mock_stream.call_args
        self.assertTrue(kwargs["json"]["stream"])
        self.assertEqual(kwargs["json"]["instructions"], "Be concise.")
        self.assertEqual(kwargs["json"]["input"], [{"role": "user", "content": "Hi"}])

    def test_codex_http_error_reads_stream_before_accessing_error_text(self) -> None:
        provider = self._provider()
        response = MagicMock()
        response.status_code = 400
        loaded = False

        def read() -> bytes:
            nonlocal loaded
            loaded = True
            return b'{"detail":"bad request"}'

        def text() -> str:
            if not loaded:
                raise RuntimeError("Attempted to access streaming response content, without having called read().")
            return '{"detail":"bad request"}'

        type(response).text = property(lambda _self: text())
        response.read.side_effect = read
        stream = MagicMock()
        stream.__enter__.return_value = response
        stream.__exit__.return_value = None
        creds = {"access_token": "not-a-jwt", "base_url": "https://chatgpt.com/backend-api/codex"}

        with patch("providers_codex.codex_auth.runtime_credentials", return_value=creds), patch(
            "providers_codex.httpx.stream", return_value=stream
        ), self.assertRaises(ProviderError) as ctx:
            provider.chat("gpt-5.6-terra", [{"role": "user", "content": "Hi"}])

        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("bad request", str(ctx.exception))

    def test_codex_chat_normalizes_streamed_function_call(self) -> None:
        provider = self._provider()
        stream = self._stream([
            'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_123","name":"search_memory","arguments":"{\\"query\\":\\"Kara\\"}"}}',
            'data: {"type":"response.completed","response":{"status":"completed"}}',
        ])
        creds = {"access_token": "not-a-jwt", "base_url": "https://chatgpt.com/backend-api/codex"}
        tools = [{"type": "function", "function": {"name": "search_memory", "description": "Search", "parameters": {"type": "object"}}}]

        with patch("providers_codex.codex_auth.runtime_credentials", return_value=creds), patch(
            "providers_codex.httpx.stream", return_value=stream
        ) as mock_stream:
            result = provider.chat("gpt-5.6-terra", [{"role": "user", "content": "Recall Kara"}], tools=tools)

        call = result["message"]["tool_calls"][0]
        self.assertEqual(call["id"], "call_123")
        self.assertEqual(call["function"]["name"], "search_memory")
        self.assertEqual(call["function"]["arguments"], '{"query":"Kara"}')
        _, kwargs = mock_stream.call_args
        self.assertEqual(kwargs["json"]["tools"][0]["name"], "search_memory")


if __name__ == "__main__":
    unittest.main()

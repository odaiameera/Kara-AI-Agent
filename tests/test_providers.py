"""Unit tests for the provider abstraction (no live API calls)."""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from provider_base import ChatProvider, ProviderError
from providers import Provider, to_chat_provider
from providers_ollama import OllamaProvider


class MockChatProvider:
    """Minimal ChatProvider for structural typing tests."""

    id = "mock"
    name = "Mock"
    type = "mock"
    host = "http://mock"
    api_key_env = None

    def __init__(self) -> None:
        self.chat_calls: list[dict[str, Any]] = []

    @property
    def has_credentials(self) -> bool:
        return True

    def is_reachable(self) -> bool:
        return True

    def list_models(self) -> list[str]:
        return ["mock-model"]

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        self.chat_calls.append(
            {"model": model, "messages": messages, "tools": tools, "temperature": temperature}
        )
        return {"message": {"role": "assistant", "content": "ok"}}

    def embed(self, text: str, model: str | None = None) -> list[float]:
        return [0.1, 0.2]

    def embed_batch(
        self, texts: list[str], model: str | None = None
    ) -> list[list[float]]:
        return [[0.1, 0.2] for _ in texts]


class TestChatProviderProtocol(unittest.TestCase):
    def test_mock_satisfies_protocol(self) -> None:
        provider = MockChatProvider()
        self.assertIsInstance(provider, ChatProvider)


class TestOllamaProvider(unittest.TestCase):
    def _local_config(self) -> Provider:
        return Provider(
            id="ollama-local",
            name="Ollama Local",
            type="ollama",
            host="http://localhost:11434",
        )

    def test_exposes_config_fields(self) -> None:
        provider = OllamaProvider(self._local_config())
        self.assertEqual(provider.id, "ollama-local")
        self.assertEqual(provider.type, "ollama")
        self.assertTrue(provider.has_credentials)

    def test_cloud_requires_credentials(self) -> None:
        cloud = Provider(
            id="ollama-cloud",
            name="Ollama Cloud",
            type="ollama",
            host="https://ollama.com",
            api_key=None,
            api_key_env="OLLAMA_API_KEY",
        )
        provider = OllamaProvider(cloud)
        self.assertFalse(provider.has_credentials)

    @patch("providers_ollama.ollama_client.list_models", return_value=["a", "b"])
    def test_list_models_delegates(self, mock_list: MagicMock) -> None:
        cfg = self._local_config()
        provider = OllamaProvider(cfg)
        self.assertEqual(provider.list_models(), ["a", "b"])
        mock_list.assert_called_once_with(cfg)

    @patch("providers_ollama.ollama_client.chat", return_value={"message": {}})
    def test_chat_delegates(self, mock_chat: MagicMock) -> None:
        cfg = self._local_config()
        provider = OllamaProvider(cfg)
        messages = [{"role": "user", "content": "hi"}]
        provider.chat("llama3", messages, temperature=0.5)
        mock_chat.assert_called_once_with(
            cfg, "llama3", messages, tools=None, temperature=0.5
        )

    @patch("providers_ollama.ollama_client.embed", return_value=[1.0])
    def test_embed_delegates(self, mock_embed: MagicMock) -> None:
        cfg = self._local_config()
        provider = OllamaProvider(cfg)
        self.assertEqual(provider.embed("hello", model="nomic"), [1.0])
        mock_embed.assert_called_once_with(cfg, "hello", model="nomic")


class TestProviderFactory(unittest.TestCase):
    def test_to_chat_provider_ollama(self) -> None:
        cfg = Provider(id="ollama-local", name="Local", type="ollama", host="http://x")
        provider = to_chat_provider(cfg)
        self.assertIsInstance(provider, OllamaProvider)

    def test_unsupported_type_raises(self) -> None:
        cfg = Provider(id="x", name="X", type="unknown", host="http://x")
        with self.assertRaises(RuntimeError):
            to_chat_provider(cfg)


class TestProviderError(unittest.TestCase):
    def test_ollama_error_is_provider_error(self) -> None:
        from ollama_client import OllamaError

        self.assertTrue(issubclass(OllamaError, ProviderError))


if __name__ == "__main__":
    unittest.main()

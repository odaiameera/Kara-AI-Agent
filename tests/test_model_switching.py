"""Unit tests for native provider/model switching commands."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from providers import models
from providers.registry import Provider


class TempSettingsMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patches = [
            patch.object(config, "BRAIN_DIR", self.tmp_path),
            patch.object(models, "SETTINGS_FILE", self.tmp_path / "settings.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()


class TestModelSwitching(TempSettingsMixin, unittest.TestCase):
    def _provider_records(self) -> list[Provider]:
        return [
            Provider(id="ollama-cloud", name="Ollama Cloud", type="ollama", host="https://ollama.com", api_key="x", api_key_env="OLLAMA_API_KEY"),
            Provider(id="openai-codex", name="OpenAI Codex", type="openai-codex", host="https://chatgpt.com/backend-api/codex"),
        ]

    @patch("providers.models.providers.load_providers")
    def test_format_providers_list_marks_active_provider(self, mock_load) -> None:
        mock_load.return_value = self._provider_records()
        models.set_active("openai-codex", "gpt-5.5")

        text = models.format_providers_list()

        self.assertIn("openai-codex", text)
        self.assertIn("OpenAI Codex", text)
        self.assertIn("*", text)
        self.assertIn("/provider openai-codex", text)

    @patch("providers.models.providers.get_provider")
    def test_select_provider_uses_default_model_when_model_omitted(self, mock_get) -> None:
        mock_get.return_value = Provider(
            id="openai-codex",
            name="OpenAI Codex",
            type="openai-codex",
            host="https://chatgpt.com/backend-api/codex",
        )

        provider_id, model = models.select_provider("openai-codex")

        self.assertEqual(provider_id, "openai-codex")
        self.assertEqual(model, "gpt-5.5")
        self.assertEqual(models.get_active_provider_id(), "openai-codex")
        self.assertEqual(models.get_current_model(), "gpt-5.5")

    @patch("providers.models.providers.get_provider")
    def test_select_provider_accepts_explicit_model(self, mock_get) -> None:
        mock_get.return_value = Provider(id="ollama-cloud", name="Ollama Cloud", type="ollama", host="https://ollama.com")

        provider_id, model = models.select_provider("ollama-cloud", "gpt-oss:120b")

        self.assertEqual((provider_id, model), ("ollama-cloud", "gpt-oss:120b"))

    @patch("providers.models.providers.get_provider")
    def test_parse_model_target_switches_provider_slash_model(self, mock_get) -> None:
        mock_get.return_value = Provider(
            id="openai-codex",
            name="OpenAI Codex",
            type="openai-codex",
            host="https://chatgpt.com/backend-api/codex",
        )

        provider_id, model = models.parse_model_target("openai-codex/gpt-5.4-mini", current_provider_id="ollama-cloud")

        self.assertEqual((provider_id, model), ("openai-codex", "gpt-5.4-mini"))

    @patch("providers.models.providers.get_provider")
    def test_parse_model_target_keeps_current_provider_for_plain_model(self, mock_get) -> None:
        mock_get.return_value = Provider(id="ollama-cloud", name="Ollama Cloud", type="ollama", host="https://ollama.com")

        provider_id, model = models.parse_model_target("gpt-oss:120b", current_provider_id="ollama-cloud")

        self.assertEqual((provider_id, model), ("ollama-cloud", "gpt-oss:120b"))


if __name__ == "__main__":
    unittest.main()

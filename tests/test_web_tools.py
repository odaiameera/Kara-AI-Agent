from __future__ import annotations

import unittest

from unittest.mock import patch

import tools.web_tools as web_tools
from tools.web_tools import _parse_brave_html, _parse_duckduckgo_html


class WebToolsTests(unittest.TestCase):
    def test_parses_brave_server_rendered_result(self) -> None:
        html = """
        <div class="snippet card" data-pos="1" data-type="web">
          <a href="https://example.com/" class="result-link">
            <div class="title search-snippet-title" title="Example">Example Result</div>
          </a>
          <div class="generic-snippet"><div class="content desktop-default">Useful <b>result</b>.</div></div>
        </div>
        """
        results = _parse_brave_html(html, 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/")
        self.assertEqual(results[0]["title"], "Example Result")
        self.assertEqual(results[0]["content"], "Useful result.")

    def test_parses_duckduckgo_html_and_unwraps_redirect(self) -> None:
        html = """
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Example Docs</a>
          <a class="result__snippet">A useful <b>documentation</b> result.</a>
        </div>
        """
        results = _parse_duckduckgo_html(html, 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/docs")
        self.assertEqual(results[0]["title"], "Example Docs")
        self.assertIn("useful documentation", results[0]["content"])


class UnconfiguredSearxngTests(unittest.TestCase):
    """SEARXNG_URL ships empty, so the unset path is the one a fresh clone takes."""

    def test_search_uses_public_fallback_when_no_instance_is_configured(self) -> None:
        with patch.object(web_tools, "SEARXNG_URL", ""), patch.object(
            web_tools, "_fallback_search", return_value="RESULTS"
        ) as fallback:
            self.assertEqual(web_tools.web_search("kara"), "RESULTS")
        fallback.assert_called_once()

    def test_no_request_is_built_against_an_empty_base_url(self) -> None:
        with patch.object(web_tools, "SEARXNG_URL", ""), patch.object(
            web_tools, "_fallback_search", return_value="RESULTS"
        ), patch.object(web_tools, "get_client") as client:
            web_tools.web_search("kara")
        client.assert_not_called()

    def test_unreachable_fallback_names_the_setting_to_configure(self) -> None:
        with patch.object(web_tools, "SEARXNG_URL", ""), patch.object(
            web_tools, "_fallback_search", return_value=None
        ):
            message = web_tools.web_search("kara")
        self.assertIn("SEARXNG_URL", message)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()

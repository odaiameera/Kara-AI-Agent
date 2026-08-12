"""Web search (SearXNG) and web fetch tools.

STUDY GUIDE
-----------
* ``web_search`` queries SearXNG (JSON or HTML fallback); ``web_fetch`` extracts page text.
* Custom HTMLParser subclass strips tags without third-party libraries.
* Key concepts: class inheritance, ``HTMLParser``, shared HTTP client, regex parsing, urlparse.
"""
from __future__ import annotations

import json
import os
import re
from html import unescape
from html.parser import HTMLParser
from io import StringIO
from urllib.parse import parse_qs, unquote, urlparse

import httpx

import config
from tools.http_client import default_headers, get_client

# LEARN: Reuse the central config value instead of re-reading the env var here.
SEARXNG_URL = config.SEARXNG_URL
MAX_SEARCH_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "8"))
MAX_FETCH_CHARS = int(os.getenv("WEB_FETCH_MAX_CHARS", "8000"))
BRAVE_SEARCH_URL = os.getenv(
    "WEB_SEARCH_FALLBACK_URL", "https://search.brave.com/search"
).strip()
DUCKDUCKGO_HTML_URL = os.getenv(
    "WEB_SEARCH_SECONDARY_FALLBACK_URL", "https://html.duckduckgo.com/html/"
).strip()


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text extractor (stdlib only)."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self._buf = StringIO()

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag.lower() in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self._buf.write("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if text:
            self._buf.write(text + " ")

    def text(self) -> str:
        raw = self._buf.getvalue()
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _validate_url(url: str) -> str:
    # LEARN: urlparse splits a URL into scheme, netloc, path — we reject non-http(s) schemes.
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid URL (http/https only): {url}")
    return url.strip()


def _truncate(text: str, limit: int = MAX_FETCH_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... (truncated, {len(text) - limit} more chars)"


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", clean)


def _is_cloudflare_access_page(html: str) -> bool:
    lower = html.lower()
    return (
        "cloudflareaccess.com" in lower
        or "cf-access-login" in lower
        or "sign in with" in lower and "cloudflare" in lower
    )


def _parse_searxng_html(html: str, max_results: int) -> list[dict[str, str]]:
    """Parse SearXNG HTML results when JSON is unavailable."""
    results: list[dict[str, str]] = []
    # LEARN: re.findall with re.S (DOTALL) lets . match newlines inside HTML blocks.
    blocks = re.findall(
        r'<article[^>]*class="[^"]*\bresult\b[^"]*"[^>]*>(.*?)</article>',
        html,
        flags=re.S | re.I,
    )
    for chunk in blocks:
        link_m = re.search(
            r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            chunk,
            flags=re.S | re.I,
        )
        if not link_m:
            continue
        url = link_m.group(1).strip()
        title = _strip_html(link_m.group(2))
        content_m = re.search(
            r'<p[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>(.*?)</p>',
            chunk,
            flags=re.S | re.I,
        )
        snippet = _strip_html(content_m.group(1)) if content_m else ""
        if url and title:
            results.append({"title": title, "url": url, "content": snippet})
        if len(results) >= max_results:
            break
    return results


def _format_search_results(
    query: str, results: list[dict], *, via: str = "json"
) -> str:
    if not results:
        return f"No web results for '{query}'."
    labels = {
        "json": "Web search results",
        "html": "Web search results (SearXNG HTML fallback)",
        "brave": "Web search results (Brave Search fallback)",
        "duckduckgo": "Web search results (DuckDuckGo fallback)",
    }
    label = labels.get(via, "Web search results")
    lines = [f"{label} for '{query}' ({len(results)} shown):\n"]
    for i, item in enumerate(results, 1):
        title = (item.get("title") or "Untitled").strip()
        link = (item.get("url") or "").strip()
        snippet = (item.get("content") or item.get("snippet") or "").strip()
        snippet = re.sub(r"\s+", " ", snippet)
        if len(snippet) > 320:
            snippet = snippet[:320] + "..."
        lines.append(f"{i}. {title}\n   URL: {link}\n   {snippet}")
    return "\n\n".join(lines)


def _duckduckgo_result_url(raw_url: str) -> str:
    """Resolve DuckDuckGo redirect links to their external destination."""
    value = unescape(raw_url.strip())
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = "https://duckduckgo.com" + value
    parsed = urlparse(value)
    if parsed.netloc.casefold().endswith("duckduckgo.com"):
        destination = parse_qs(parsed.query).get("uddg", [""])[0]
        if destination:
            value = unquote(destination)
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _parse_duckduckgo_html(html: str, max_results: int) -> list[dict[str, str]]:
    """Parse the stable no-JavaScript DuckDuckGo HTML result surface."""
    pattern = re.compile(
        r'<a(?=[^>]*class=["\'][^"\']*\bresult__a\b[^"\']*["\'])'
        r'(?=[^>]*href=["\']([^"\']+)["\'])[^>]*>(.*?)</a>',
        flags=re.I | re.S,
    )
    links = list(pattern.finditer(html))
    results: list[dict[str, str]] = []
    for index, match in enumerate(links):
        url = _duckduckgo_result_url(match.group(1))
        title = unescape(_strip_html(match.group(2)))
        if not url or not title:
            continue
        end = links[index + 1].start() if index + 1 < len(links) else len(html)
        nearby = html[match.end() : end]
        snippet_match = re.search(
            r'<(?:a|div)[^>]*class=["\'][^"\']*\bresult__snippet\b[^"\']*["\'][^>]*>(.*?)</(?:a|div)>',
            nearby,
            flags=re.I | re.S,
        )
        snippet = unescape(_strip_html(snippet_match.group(1))) if snippet_match else ""
        results.append({"title": title, "url": url, "content": snippet})
        if len(results) >= max_results:
            break
    return results


def _duckduckgo_search(query: str, max_results: int) -> str | None:
    """Best-effort public fallback when the configured SearXNG is unavailable."""
    if not DUCKDUCKGO_HTML_URL:
        return None
    try:
        response = get_client().get(
            DUCKDUCKGO_HTML_URL,
            params={"q": query.strip()},
            headers=default_headers(accept="text/html"),
        )
        response.raise_for_status()
        results = _parse_duckduckgo_html(response.text, max_results)
    except (httpx.HTTPError, ValueError):
        return None
    return _format_search_results(query, results, via="duckduckgo") if results else None


def _parse_brave_html(html: str, max_results: int) -> list[dict[str, str]]:
    """Parse Brave Search's server-rendered web result cards."""
    starts = list(
        re.finditer(
            r'<div[^>]*class=["\'][^"\']*\bsnippet\b[^"\']*["\'][^>]*data-type=["\']web["\'][^>]*>',
            html,
            flags=re.I,
        )
    )
    results: list[dict[str, str]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(html)
        block = html[start.start() : end]
        link_match = re.search(r'<a[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>', block, re.I)
        title_match = re.search(
            r'<div[^>]*class=["\'][^"\']*\bsearch-snippet-title\b[^"\']*["\'][^>]*>(.*?)</div>',
            block,
            flags=re.I | re.S,
        )
        if not link_match or not title_match:
            continue
        snippet_match = re.search(
            r'<div[^>]*class=["\'][^"\']*\bcontent\b[^"\']*["\'][^>]*>(.*?)</div>',
            block,
            flags=re.I | re.S,
        )
        results.append(
            {
                "url": unescape(link_match.group(1)),
                "title": unescape(_strip_html(title_match.group(1))),
                "content": unescape(_strip_html(snippet_match.group(1))) if snippet_match else "",
            }
        )
        if len(results) >= max_results:
            break
    return results


def _brave_search(query: str, max_results: int) -> str | None:
    if not BRAVE_SEARCH_URL:
        return None
    try:
        response = get_client().get(
            BRAVE_SEARCH_URL,
            params={"q": query.strip(), "source": "web"},
            headers=default_headers(accept="text/html"),
        )
        response.raise_for_status()
        results = _parse_brave_html(response.text, max_results)
    except (httpx.HTTPError, ValueError):
        return None
    return _format_search_results(query, results, via="brave") if results else None


def _fallback_search(query: str, max_results: int) -> str | None:
    """Try public HTML providers without requiring an API key."""
    return _brave_search(query, max_results) or _duckduckgo_search(query, max_results)


def web_search(query: str, max_results: int = MAX_SEARCH_RESULTS) -> str:
    """
    Search the web using the configured SearXNG instance. Use for current events, documentation, or facts not in memory.

    Args:
        query: The search query.
        max_results: Maximum number of results to return (default 8).

    Returns:
        A formatted list of search results with titles, URLs, and snippets.
    """
    if not query.strip():
        return "Error: search query cannot be empty."

    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = MAX_SEARCH_RESULTS
    max_results = max(1, min(max_results, 15))

    url = f"{SEARXNG_URL}/search"
    params = {"q": query.strip(), "format": "json"}

    try:
        # LEARN: get_client() returns a shared, pooled client — don't close it.
        resp = get_client().get(
            url,
            params=params,
            headers=default_headers(accept="application/json, text/html"),
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").lower()
        body = resp.text

        if "json" in content_type:
            try:
                data = resp.json()
            except json.JSONDecodeError:
                data = {}
            results = data.get("results") or []
            return _format_search_results(
                query, results[:max_results], via="json"
            )

        if _is_cloudflare_access_page(body):
            fallback = _fallback_search(query, max_results)
            if fallback:
                return fallback
            return (
                "SearXNG returned a Cloudflare Access login page. "
                "Set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET in .env "
                "(or CF_ACCESS_COOKIE for browser SSO)."
            )

        html_results = _parse_searxng_html(body, max_results)
        if html_results:
            return _format_search_results(query, html_results, via="html")

        fallback = _fallback_search(query, max_results)
        if fallback:
            return fallback
        return (
            "SearXNG returned HTML but no results could be parsed. "
            "Check SEARXNG_URL and Cloudflare Access credentials."
        )

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            try:
                resp2 = get_client().get(
                    url,
                    params={"q": query.strip()},
                    headers=default_headers(accept="text/html"),
                )
                if resp2.status_code == 200:
                    html_results = _parse_searxng_html(resp2.text, max_results)
                    if html_results:
                        return _format_search_results(
                            query, html_results, via="html"
                        )
            except Exception:
                pass
            fallback = _fallback_search(query, max_results)
            if fallback:
                return fallback
            return (
                "SearXNG returned 403. JSON search may be disabled on the instance, "
                "or Cloudflare Access credentials (CF_ACCESS_CLIENT_ID/SECRET) are missing or invalid."
            )
        fallback = _fallback_search(query, max_results)
        if fallback:
            return fallback
        return f"SearXNG HTTP error {e.response.status_code}: {e.response.text[:200]}"
    except httpx.RequestError as e:
        fallback = _fallback_search(query, max_results)
        if fallback:
            return fallback
        return f"Could not reach SearXNG at {SEARXNG_URL}: {e}"


def web_fetch(url: str, max_chars: int = MAX_FETCH_CHARS) -> str:
    """
    Fetch a URL and return its readable text content. Use after web_search to read a specific page.

    Args:
        url: The http(s) URL to fetch.
        max_chars: Maximum characters to return (default 8000).

    Returns:
        The page title and extracted text, or an error message.
    """
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = MAX_FETCH_CHARS
    max_chars = max(500, min(max_chars, 20000))

    try:
        target = _validate_url(url)
    except ValueError as e:
        return f"Error: {e}"

    try:
        resp = get_client().get(
            target,
            headers=default_headers(accept="text/html,application/json,text/plain,*/*"),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return f"HTTP error {e.response.status_code} fetching {target}"
    except httpx.RequestError as e:
        return f"Could not fetch {target}: {e}"

    content_type = resp.headers.get("content-type", "").lower()
    body = resp.text

    if "application/json" in content_type:
        try:
            parsed = json.loads(body)
            body = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        title = target
        text = body
    elif "text/html" in content_type:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else target
        # LEARN: Subclass HTMLParser — feed() drives parsing; callback methods collect visible text.
        parser = _TextExtractor()
        try:
            parser.feed(body)
            text = parser.text()
        except Exception:
            text = re.sub(r"<[^>]+>", " ", body)
            text = re.sub(r"\s+", " ", text).strip()
    else:
        title = target
        text = body

    if not text.strip():
        return f"Fetched {target} but no readable text was extracted."

    return _truncate(f"Title: {title}\nURL: {target}\n\n{text}", max_chars)

# --- Registry declaration ------------------------------------------------------
# Consumed by tools.registry; this is the single source of truth for which
# functions in this module are exposed to the model and which of them are safe
# for unattended scheduled runs.
TOOL_GROUP = "web"

TOOLS = [
    web_search,
    web_fetch,
]

SCHEDULED_SAFE = {
    "web_search",
    "web_fetch",
}

# Tools with no side effects. Used to decide what may run concurrently; a
# superset of SCHEDULED_SAFE, which is a separate policy about unattended runs.
READ_ONLY = {
    "web_search",
    "web_fetch",
}

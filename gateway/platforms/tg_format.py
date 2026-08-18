"""Markdown -> Telegram-safe HTML conversion.

Telegram renders only a small, strict subset of HTML (``<b>``, ``<i>``,
``<code>``, ``<pre>``, ``<a>`` ...). LLMs, however, emit *standard* Markdown
(``**bold**``, ``- bullets``, ``## headings``), which Telegram shows verbatim —
so ``**bold**`` appears WITH the asterisks instead of rendering as bold.

This module converts the model's Markdown into Telegram's HTML subset so replies
look properly formatted, and splits long replies under Telegram's 4096-character
limit without slicing through a tag.

Why HTML and not MarkdownV2? HTML is Telegram's most forgiving parse mode: only
``<``, ``>`` and ``&`` must be escaped, whereas MarkdownV2 requires escaping ~18
characters and hard-fails on the smallest slip. The caller also keeps a
plain-text fallback, so a malformed message can never block a reply.
"""
from __future__ import annotations

import html as _html
import re

# LEARN: Private-use Unicode chars fence off placeholders — they can't occur in
# real text and survive escaping, splitting, and the Telegram API unchanged.
_PH_OPEN = "\ue000"
_PH_CLOSE = "\ue001"

# LEARN: re.DOTALL makes "." also match newlines so a fenced block can span lines.
_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
# LEARN: \1 is a backreference — the same rule char must repeat (--- / *** / ___).
_HR_RE = re.compile(r"^[ \t]*([-*_])\1{2,}[ \t]*$", re.MULTILINE)
_HEADER_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*([^\n]+?)\*\*|__([^\n]+?)__")
# LEARN: Lookarounds keep "a * b" and "snake_case" from being misread as italics.
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_ITALIC_US_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_STRIKE_RE = re.compile(r"~~([^\n]+?)~~")
_LINK_RE = re.compile(r"\[([^\]\n]+?)\]\((https?://[^)\s]+|tg://[^)\s]+)\)")

TELEGRAM_MAX = 4096
# LEARN: Split source a bit under the hard cap so HTML tag expansion still fits.
SOURCE_LIMIT = 3800

def to_telegram_html(text: str) -> str:
    """Convert a Markdown string into Telegram's supported HTML subset."""
    blocks: list[str] = []
    inlines: list[str] = []

    def _stash_block(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        # LEARN: ```code``` on one line lands in the lang group — treat it as body.
        if not code.strip() and lang:
            code, lang = lang, ""
        esc = _html.escape(code.strip("\n"), quote=False)
        if lang:
            rendered = f'<pre><code class="language-{_html.escape(lang, quote=False)}">{esc}</code></pre>'
        else:
            rendered = f"<pre>{esc}</pre>"
        blocks.append(rendered)
        return f"{_PH_OPEN}B{len(blocks) - 1}{_PH_CLOSE}"

    def _stash_inline(m: re.Match) -> str:
        inlines.append(f"<code>{_html.escape(m.group(1), quote=False)}</code>")
        return f"{_PH_OPEN}I{len(inlines) - 1}{_PH_CLOSE}"

    # 1. Protect code first so Markdown inside it stays literal.
    text = _CODE_BLOCK_RE.sub(_stash_block, text)
    text = _INLINE_CODE_RE.sub(_stash_inline, text)

    # 2. Escape everything else — after this the only real tags are ones we add.
    #    quote=False leaves apostrophes/quotes alone (Telegram doesn't need them
    #    escaped in text), keeping normal prose clean.
    text = _html.escape(text, quote=False)

    # 3. Block level: drop horizontal rules, bold-ify headings, unify bullets.
    text = _HR_RE.sub("", text)
    text = _HEADER_RE.sub(
        lambda m: f"<b>{m.group(1).replace('**', '').replace('__', '')}</b>", text
    )
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}\u2022 ", text)

    # 4. Inline emphasis — bold before italic so ** isn't read as two * italics.
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_US_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)

    # 5. Links: [label](url). URL chars were escaped above; that's valid in href.
    text = _LINK_RE.sub(_link_sub, text)

    # 6. Restore protected code (inline first, then blocks).
    for i, val in enumerate(inlines):
        text = text.replace(f"{_PH_OPEN}I{i}{_PH_CLOSE}", val)
    for i, val in enumerate(blocks):
        text = text.replace(f"{_PH_OPEN}B{i}{_PH_CLOSE}", val)
    return text

def _link_sub(m: re.Match) -> str:
    label = m.group(1)
    # LEARN: A stray double-quote would break the href attribute — encode it.
    url = m.group(2).replace('"', "%22")
    return f'<a href="{url}">{label}</a>'

def split_source(text: str, limit: int = SOURCE_LIMIT) -> list[str]:
    """Split Markdown at line boundaries so each part's HTML fits Telegram.

    Fenced code blocks are kept balanced by closing ``` at the end of one chunk
    and reopening it (with the same language) at the start of the next.
    """
    text = text.rstrip("\n")
    if len(text) <= limit:
        return [text] if text.strip() else []

    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0
    fence: str | None = None

    def _flush(reopen: bool) -> None:
        nonlocal current, cur_len
        block = current[:]
        if fence is not None:
            block.append("```")
        joined = "\n".join(block)
        if joined.strip():
            chunks.append(joined)
        current = []
        cur_len = 0
        if reopen and fence is not None:
            current.append(fence)
            cur_len = len(fence) + 1

    for line in text.split("\n"):
        add = len(line) + 1
        if current and cur_len + add > limit:
            _flush(reopen=True)
        current.append(line)
        cur_len += add
        if line.lstrip().startswith("```"):
            fence = line if fence is None else None
    _flush(reopen=False)

    # LEARN: Safety net for a pathological single line with no break points —
    # hard-slice so every chunk (and its plain-text fallback) stays under the cap.
    result: list[str] = []
    for chunk in chunks:
        while len(chunk) > TELEGRAM_MAX:
            result.append(chunk[:limit])
            chunk = chunk[limit:]
        result.append(chunk)
    return result

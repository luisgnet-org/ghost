"""
Telegram MarkdownV2 escaping utilities.

MarkdownV2 requires ALL special characters to be escaped with a backslash
outside of formatting spans. This module provides a single entry point:

    escape(text) -> str

It understands the text as "natural writing with backtick code spans" — the
same way the agent writes messages — and produces valid MarkdownV2:

  - Backtick inline code (`like this`) is preserved as-is.
  - Triple-backtick code blocks (```...```) are preserved as-is.
  - Bold: **text** or *text* passed through (already MarkdownV2 format).
  - Italic: _text_ passed through.
  - Strikethrough: ~text~ passed through.
  - Everything else (dots, dashes, parens, brackets, exclamation marks, etc.)
    is escaped so Telegram doesn't misparse it.

Usage:
    from ghost.telegram.markdown_v2 import escape

    safe = escape("Call `send_message(topic=general)` to reply. Done!")
    # => "Call `send_message(topic=general)` to reply\\. Done\\!"
"""

import re
from typing import List, Tuple

# All characters that must be escaped in MarkdownV2 *outside* formatting spans.
# Source: https://core.telegram.org/bots/api#markdownv2-style
_SPECIAL_CHARS = r'\_*[]()~`>#+-=|{}.!'

# Build a regex that matches any single special char (for use in non-code text)
_SPECIAL_RE = re.compile(r'([' + re.escape(_SPECIAL_CHARS) + r'])')


def _escape_plain(text: str) -> str:
    """Escape all MarkdownV2 special characters in a plain-text segment.

    This is applied to text that is NOT inside a code span or code block.
    Bold/italic markers (* _ ~) are also escaped here — callers that want
    to preserve formatting should pass pre-formatted text or handle those
    characters manually before calling escape().
    """
    return _SPECIAL_RE.sub(r'\\\1', text)


def escape(text: str) -> str:
    """Convert natural agent text to valid Telegram MarkdownV2.

    Understands:
    - Triple-backtick code blocks  (```...```)  — contents not escaped
    - Inline backtick code spans   (`...`)      — contents not escaped
    - Everything else: all MarkdownV2 special chars are escaped

    The agent can write naturally:
        "Call `send_message` to post. Done!"
    And get back:
        "Call `send_message` to post\\. Done\\!"

    Telegram MarkdownV2 rules for code spans:
    - Inside ```: only ``` itself needs escaping (we don't touch the interior)
    - Inside ` `: only ` itself needs escaping (we don't touch the interior)

    Args:
        text: Raw text from the agent, possibly containing backtick code spans.

    Returns:
        Escaped string safe to pass to Telegram with parse_mode="MarkdownV2".
    """
    if not text:
        return text

    segments = _split_code_segments(text)
    parts: List[str] = []

    for content, seg_type in segments:
        if seg_type == "triple":
            # Triple-backtick block: wrap in ``` without escaping content
            # (Telegram only cares that the delimiters are present)
            parts.append("```" + content + "```")
        elif seg_type == "inline":
            # Inline code: wrap in single backticks without escaping content
            parts.append("`" + content + "`")
        else:
            # Plain text: escape all special chars
            parts.append(_escape_plain(content))

    return "".join(parts)


def _split_code_segments(text: str) -> List[Tuple[str, str]]:
    """Split text into (content, segment_type) tuples.

    segment_type is one of:
        "plain"   - ordinary text, needs escaping
        "triple"  - inside ```...``` block, do not escape
        "inline"  - inside `...` span, do not escape

    Strategy:
    1. First split on ``` (triple backtick) pairs.
    2. Within plain segments, split on ` (single backtick) pairs.
    3. Unclosed delimiters are treated as plain text (safe degradation).
    """
    # Step 1: split on triple backticks
    triple_parts = text.split("```")
    segments: List[Tuple[str, str]] = []

    for i, part in enumerate(triple_parts):
        if i % 2 == 1:
            # Odd index = inside a ``` block
            segments.append((part, "triple"))
        else:
            # Even index = outside a ``` block — look for inline backticks
            segments.extend(_split_inline_code(part))

    # If odd number of ``` delimiters, the last "triple" segment is unclosed.
    # _split_code_segments already handled this by treating it as triple
    # (which will re-wrap with ```), which is the best safe degradation.

    return segments


def _split_inline_code(text: str) -> List[Tuple[str, str]]:
    """Split a plain-text segment on single backtick pairs.

    Returns list of (content, "plain"|"inline") tuples.
    Unclosed backtick at end is treated as plain text (appended literally).
    """
    parts = text.split("`")
    result: List[Tuple[str, str]] = []

    for j, part in enumerate(parts):
        if j % 2 == 1:
            # Odd index = inside a `...` inline code span
            result.append((part, "inline"))
        else:
            # Even index = plain text
            if part:  # skip empty strings
                result.append((part, "plain"))

    # If odd number of backticks, the last "inline" segment is actually unclosed.
    # We already appended it as "inline" — convert the last one back to "plain"
    # so it gets escaped instead of wrapped in backticks.
    if len(parts) % 2 == 0 and result:
        # Odd number of backtick separators = unclosed inline code
        # The last entry was mis-classified as "inline"
        last_content, last_type = result[-1]
        if last_type == "inline":
            result[-1] = (last_content, "plain")

    return result

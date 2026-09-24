"""Normalising LLM responses to plain text.

Newer Gemini models return `AIMessage.content` as a LIST of content blocks
(``[{'type': 'text', 'text': ..., 'extras': {'signature': ...}}]``) rather than
a plain string. Code that assumes a string ends up stringifying the whole
structure — which leaked the block wrapper and a multi-KB base64 signature into
user-facing answers, and made the completeness judge's float() parse fail so it
silently fell back to its 0.5 default.
"""
from typing import Any


def as_text(content: Any) -> str:
    """Return the plain text of an LLM response, whatever shape it arrived in."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)

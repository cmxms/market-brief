"""The reader-facing Markdown shared by validation and Discord chunking."""
from __future__ import annotations

import re

_COMMENT = re.compile(r"\n?<!--(.*?)-->\n?", re.DOTALL)


def visible_text(markdown: str) -> str:
    """Normalize newlines and remove only well-formed, non-nested HTML comments.

    Discord prints comments literally, so we remove them before either checking
    or splitting a brief. Reject ambiguous/unsupported constructs rather than
    allowing one opener to swallow protected footer content.
    """
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")

    def remove(match: re.Match) -> str:
        body = match.group(1)
        if ("<!" in body or "<?" in body or "--" in body
                or body.startswith(">") or body.endswith("-")):
            raise ValueError("malformed or unsupported HTML comment")
        return "\n"

    text = _COMMENT.sub(remove, text)
    if "<!" in text or "-->" in text or "<?" in text:
        raise ValueError("unmatched or unsupported HTML comment construct")
    return text.strip("\n")

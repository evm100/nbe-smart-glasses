"""Markdown for the lab board: safe HTML for posts / replies, and a plain-text excerpt for previews.

markdown-it-py (already installed with `rich`) with raw HTML switched off, so whatever anyone
types or uploads is escaped; markdown-it also refuses javascript:/vbscript:/file:/data: links.
Images are off (no remote content pulled into everybody's browser), tables and ~~strike~~ on,
single line breaks kept (plain-text posts look as they were typed), and bare http(s) addresses
become links. Every link opens in a new tab.
"""
from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.token import Token
from markupsafe import Markup

_URL = re.compile(r"https?://[^\s<>\"'`]+")
_TRAIL = ".,;:!?)]}'\""

_md = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False}).enable(["table", "strikethrough"])
_md.disable("image")


def _bare_urls(state) -> None:
    """Turn http(s) addresses in plain text (not already inside a link) into links."""
    for blk in state.tokens:
        if blk.type != "inline" or not blk.children:
            continue
        out, depth = [], 0
        for t in blk.children:
            if t.type == "link_open":
                depth += 1
            elif t.type == "link_close":
                depth -= 1
            if t.type != "text" or depth or "http" not in t.content:
                out.append(t)
                continue
            s, pos = t.content, 0
            for m in _URL.finditer(s):
                url = m.group(0).rstrip(_TRAIL)
                if m.start() > pos:
                    out.append(_text(s[pos:m.start()]))
                a = Token("link_open", "a", 1)
                a.attrs = {"href": url}
                out += [a, _text(url), Token("link_close", "a", -1)]
                pos = m.start() + len(url)
            if pos < len(s):
                out.append(_text(s[pos:]))
        blk.children = out


def _text(s: str) -> Token:
    t = Token("text", "", 0)
    t.content = s
    return t


def _link_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


def _table_open(self, tokens, idx, options, env):  # wide tables scroll sideways instead of breaking the page
    return '<div class="md-table">' + self.renderToken(tokens, idx, options, env)


def _table_close(self, tokens, idx, options, env):
    return self.renderToken(tokens, idx, options, env) + "</div>"


_md.core.ruler.push("bare_urls", _bare_urls)
_md.add_render_rule("link_open", _link_open)
_md.add_render_rule("table_open", _table_open)
_md.add_render_rule("table_close", _table_close)


def render(text: str | None) -> Markup:
    """Markdown (or plain text) -> safe HTML."""
    return Markup(_md.render(text or ""))


_STRIP = [
    (re.compile(r"```.*?```", re.S), " "),                      # code blocks
    (re.compile(r"^\s*\|?\s*:?-{3,}.*$", re.M), " "),           # table separator rows
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),               # heading marks
    (re.compile(r"^\s*>\s?", re.M), ""),                        # quotes
    (re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M), ""),        # list marks
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),            # [text](url) -> text
    (re.compile(r"[*_~`|]+"), " "),                            # emphasis, code, table bars
    (re.compile(r"\s+"), " "),
]


def excerpt(text: str | None, n: int = 240) -> str:
    """The first n characters as plain text (Markdown marks removed), for collapsed posts and lists."""
    s = text or ""
    for rx, rep in _STRIP:
        s = rx.sub(rep, s)
    s = s.strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def first_heading(text: str | None) -> tuple[str, str]:
    """('Title', rest) when the text starts with a '# Title' line, else ('', text). Used for dropped .md files."""
    m = re.match(r"\s*#\s+(.+?)\s*#*\s*(?:\n|$)", text or "")
    return (m.group(1).strip(), (text or "")[m.end():].lstrip("\n")) if m else ("", text or "")


def reading_minutes(text: str | None) -> int:
    return max(1, round(len((text or "").split()) / 220))

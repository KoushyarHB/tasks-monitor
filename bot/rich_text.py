"""Rich-text conversion: Plane TipTap HTML → Telegram HTML.

- Preserves structure: headings, lists, blockquotes, code, bold/italic, links.
- Isolates RTL runs (Farsi/Arabic) with Unicode bidi controls so mixed
  Farsi+English text renders correctly in Telegram.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from .messages import esc

# Unicode bidi isolates: RLI ... PDI
RLI = "\u2067"
PDI = "\u2069"

# Characters that force RTL rendering (Arabic block + Farsi-specific + punctuation)
_RTL_RE = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")

# block tags → newline-separated layout
_BLOCK = {"h1", "h2", "h3", "h4", "h5", "p", "ul", "ol", "li", "blockquote", "pre", "div"}
# inline tags → wrap with formatting
_INLINE = {
    "b": ("<b>", "</b>"),
    "strong": ("<b>", "</b>"),
    "i": ("<i>", "</i>"),
    "em": ("<i>", "</i>"),
    "u": ("<u>", "</u>"),
    "ins": ("<u>", "</u>"),
    "s": ("<s>", "</s>"),
    "strike": ("<s>", "</s>"),
    "del": ("<s>", "</s>"),
    "code": ("<code>", "</code>"),
    "kbd": ("<code>", "</code>"),
    "sup": ("", ""),
    "sub": ("", ""),
}


def _isolate_rtl(text: str) -> str:
    """Wrap each RTL run in RLI/PDI so it renders right-to-left even inside
    an LTR paragraph. Spaces between RTL words stay inside the run (they are
    part of the RTL phrase). Pure-ASCII text is returned unchanged."""
    if not _RTL_RE.search(text):
        return text
    out: list[str] = []
    buf = ""
    in_rtl = False
    for i, ch in enumerate(text):
        is_rtl = bool(_RTL_RE.match(ch))
        is_space = ch.isspace()
        if is_rtl:
            if not in_rtl:
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(RLI)
                in_rtl = True
            buf += ch
        elif in_rtl and is_space:
            # space between RTL words: keep in the RTL run (unless next
            # non-space is LTR — then this space is the boundary)
            nxt = ""
            for j in range(i + 1, len(text)):
                if not text[j].isspace():
                    nxt = text[j]
                    break
            if nxt and _RTL_RE.match(nxt):
                buf += ch  # space inside the RTL phrase
            else:
                out.append(buf)
                out.append(PDI)
                buf = ch
                in_rtl = False
        else:
            if in_rtl:
                out.append(buf)
                out.append(PDI)
                buf = ""
                in_rtl = False
            buf += ch
    if in_rtl:
        out.append(buf)
        out.append(PDI)
    else:
        out.append(buf)
    return "".join(out)


class _TipTapToTelegram(HTMLParser):
    """Convert Plane description_html (TipTap/ProseMirror) to Telegram HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._list_stack: list[str] = []   # 'ul' or 'ol'
        self._li_index: list[int] = []
        self._skip_depth = 0

    # ── helpers ─────────────────────────────────────
    def _emit_block_sep(self) -> None:
        if self._out and not self._out[-1].endswith("\n"):
            self._out.append("\n")

    def _emit_bullet(self) -> None:
        parent = self._list_stack[-1] if self._list_stack else "ul"
        if parent == "ol":
            idx = (self._li_index[-1] if self._li_index else 0) + 1
            self._li_index[-1] = idx
            self._out.append(f"{idx}. ")
        else:
            self._out.append("• ")

    # ── parser hooks ─────────────────────────────────
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            self._skip_depth += 1
            return
        if tag in ("image", "img"):
            # images can't render inline — mark them
            self._out.append("[🖼 image]")
            return
        if tag == "mention":
            # Plane mentions: <mention data-id="@user"> or label text follows
            return
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                self._out.append(f'<a href="{esc(href)}">')
            return
        if tag in _BLOCK:
            if tag in ("ul", "ol"):
                self._list_stack.append(tag)
                self._li_index.append(0)
                self._emit_block_sep()
            elif tag == "li":
                self._emit_block_sep()
                self._emit_bullet()
            elif tag == "blockquote":
                self._emit_block_sep()
                self._out.append("<blockquote>")
            elif tag == "pre":
                self._emit_block_sep()
                self._out.append("<pre>")
            else:  # headings / paragraphs / div
                self._emit_block_sep()
            if tag in ("h1", "h2", "h3", "h4"):
                self._out.append("<b>")
            return
        if tag in _INLINE:
            self._out.append(_INLINE[tag][0])
            return
        # unknown tag → ignore but keep content

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "image" or tag == "img":
            return
        if tag == "mention":
            return
        if tag == "a":
            self._out.append("</a>")
            return
        if tag in _BLOCK:
            if tag in ("ul", "ol"):
                if self._list_stack:
                    self._list_stack.pop()
                if self._li_index:
                    self._li_index.pop()
                self._emit_block_sep()
            elif tag == "blockquote":
                self._out.append("</blockquote>")
                self._emit_block_sep()
            elif tag == "pre":
                self._out.append("</pre>")
                self._emit_block_sep()
            else:
                if tag in ("h1", "h2", "h3", "h4"):
                    self._out.append("</b>")
                self._emit_block_sep()
            return
        if tag in _INLINE:
            self._out.append(_INLINE[tag][1])
            return

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        # collapse whitespace runs inside block text (TipTap emits many spaces)
        if data.strip() == "":
            return
        self._out.append(_isolate_rtl(esc(data)))


def html_to_telegram(html: str) -> str:
    """Convert Plane description_html → Telegram-HTML with RTL isolation."""
    if not html or not html.strip():
        return ""
    parser = _TipTapToTelegram()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # fallback: strip tags, keep text
        plain = re.sub(r"<[^>]+>", " ", html)
        plain = re.sub(r"\s+", " ", plain).strip()
        return _isolate_rtl(esc(plain))
    text = "".join(parser._out)
    # normalize whitespace: multiple blank lines → single
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

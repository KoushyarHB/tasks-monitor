"""Tests for bot.rich_text — TipTap HTML → Telegram HTML with RTL isolation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.rich_text import RLI, PDI, _isolate_rtl, html_to_telegram


def test_plain_paragraph():
    html = '<p class="editor-paragraph-block">Wire the frontend to the API.</p>'
    out = html_to_telegram(html)
    assert "Wire the frontend to the API." in out
    assert "<p" not in out and "editor-paragraph" not in out  # no raw tags


def test_heading_is_bold():
    html = '<h2 class="editor-heading-block">What</h2><p>Body</p>'
    out = html_to_telegram(html)
    assert "<b>What</b>" in out
    assert "Body" in out


def test_list_markers():
    html = "<ul><li>alpha</li><li>beta</li></ul>"
    out = html_to_telegram(html)
    assert "• alpha" in out
    assert "• beta" in out


def test_ordered_list_numbers():
    html = "<ol><li>first</li><li>second</li></ol>"
    out = html_to_telegram(html)
    assert "1. first" in out
    assert "2. second" in out


def test_inline_formatting():
    html = "<p>a <strong>bold</strong> and <em>italic</em> and <code>code</code></p>"
    out = html_to_telegram(html)
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "<code>code</code>" in out


def test_blockquote():
    html = "<blockquote><p>quote here</p></blockquote>"
    out = html_to_telegram(html)
    assert "<blockquote>" in out and "</blockquote>" in out


def test_escapes_html_chars():
    html = "<p>5 < 6 &amp; 7 &gt; 2</p>"
    out = html_to_telegram(html)
    assert "5 &lt; 6 &amp; 7 &gt; 2" in out


# ── RTL / Farsi ───────────────────────────────────────
def test_rtl_isolated_segment():
    text = "فارسی"
    out = _isolate_rtl(text)
    assert out.startswith(RLI) and out.endswith(PDI)


def test_rtl_mixed_with_english():
    text = "Wire frontend فارسی to API"
    out = _isolate_rtl(text)
    assert RLI in out and PDI in out
    assert "Wire frontend" in out and "to API" in out


def test_ascii_unchanged():
    assert _isolate_rtl("plain ascii") == "plain ascii"


def test_farsi_description_renders():
    html = "<h2>شرح</h2><p>این یک تست فارسی است with English mixed in</p>"
    out = html_to_telegram(html)
    assert "شرح" in out
    assert "این یک تست فارسی است" in out
    assert "English mixed in" in out
    assert RLI in out  # RTL isolation applied


def test_image_marker():
    html = "<p>see <img src=\"x.png\"> below</p>"
    out = html_to_telegram(html)
    assert "[🖼 image]" in out


def test_long_description_no_truncation():
    html = "<p>" + "word " * 1000 + "</p>"
    out = html_to_telegram(html)
    assert "word " * 900 in out  # full content, not truncated at 900


def test_mention_handled():
    html = '<p>hi <mention data-id="@user"></mention> there</p>'
    out = html_to_telegram(html)
    assert "hi" in out and "there" in out

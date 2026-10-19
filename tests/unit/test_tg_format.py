"""Markdown -> Telegram HTML."""

from __future__ import annotations

from law.tg_format import to_telegram_html as conv


def test_bold_italic_code_and_links():
    out = conv("**$1,956.07 per month** with *fixed* rate, see `amortise()` and [docs](https://example.com)")
    assert "<b>$1,956.07 per month</b>" in out and "<i>fixed</i>" in out
    assert "<code>amortise()</code>" in out and '<a href="https://example.com">docs</a>' in out


def test_html_is_escaped():
    assert conv("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_table_becomes_aligned_preformatted_block():
    md = "| Item | Value |\n|---|---|\n| Loan amount | 400,000 |\n| **Monthly payment** | **1,956.07** |"
    out = conv(md)
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert "|" not in out and "**" not in out
    lines = out[5:-6].splitlines()
    assert lines[0].startswith("Item") and set(lines[1]) <= {"-", " "}
    assert lines[3].startswith("Monthly payment") and lines[3].endswith("1,956.07")


def test_code_block_kept_verbatim():
    out = conv("Run:\n```python\nprint(1 < 2)\n```\nDone")
    assert "<pre>print(1 &lt; 2)</pre>" in out and "Done" in out


def test_headings_and_bullets():
    out = conv("## Recommendation\n- Use the API\n* Keep data portable")
    assert out.splitlines() == ["<b>Recommendation</b>", "• Use the API", "• Keep data portable"]


def test_snake_case_and_math_are_not_italicised():
    assert "<i>" not in conv("use invoice_total and 2 * 3 * 4")


def test_wide_table_becomes_records_readable_on_a_phone():
    md = ("| Dimension | Leasing (24-36 mo) | Buying (cash or finance) |\n|---|---|---|\n"
          "| Upfront cash | Low (first month + deposit) | High (full price or large down payment) |\n"
          "| Mileage | Capped (e.g. 10,000-15,000/yr) | No cap |")
    out = conv(md)
    assert "<pre>" not in out
    assert "<b>Upfront cash</b>\n• Leasing (24-36 mo): Low (first month + deposit)" in out
    assert "• Buying (cash or finance): No cap" in out


def test_quotes_become_blockquotes():
    out = conv("Answer.\n\n> *Caveat:* check local tax rules\n> before committing.")
    assert "<blockquote><i>Caveat:</i> check local tax rules\nbefore committing.</blockquote>" in out
    assert "&gt;" not in out

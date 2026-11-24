"""law.web: page text, passages around a word, and the tools every role gets by default."""

from __future__ import annotations

from law import web


def test_html_text_drops_scripts_and_keeps_blocks() -> None:
    page = ("<html><head><style>p{}</style><script>x=1</script></head><body><nav>menu</nav>"
            "<p>Deposits are protected up to RM250,000 per depositor.</p><p>Second &amp; last.</p></body></html>")
    assert web.html_text(page) == "Deposits are protected up to RM250,000 per depositor.\nSecond & last."


def test_passages_find_the_words_and_say_when_absent() -> None:
    text = "a" * 2000 + " the limit is RM250,000 " + "b" * 2000
    out = web.passages(text, "limit", width=20)
    assert "RM250,000" in out and len(out) < 100
    assert web.passages("nothing here", "deposit").startswith("(none of 'deposit' on this page)")


async def test_fetch_refuses_a_non_url() -> None:
    assert (await web.fetch("pidm.gov.my")).startswith("error: give a full http(s) URL")


def test_every_answer_offers_search_and_fetch() -> None:
    names = [t["function"]["name"] for t in web.TOOLS]
    assert names == ["web_search", "fetch_page"]

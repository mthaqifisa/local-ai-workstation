from law.mail_format import markdown_blocks, plain, to_lines


def test_plain_removes_inline_markdown() -> None:
    assert plain("**Bold** and *it* `code` [site](https://x.io)") == "Bold and it code site (https://x.io)"


def test_markdown_blocks_styles_headings_lists_and_tables() -> None:
    md = "# Top\n\nSome **text**\nmore.\n\n## Sub\n- one\n2. two\n\n| Item | Cost |\n|---|---|\n| A | 1 |\n: caption\n> quote\n```\ncode\n```"
    assert markdown_blocks(md) == [
        ("h2", "Top"), ("p", "Some text more."), ("h3", "Sub"), ("li", "•  one"), ("li", "2.  two"),
        ("li", "Item: A · Cost: 1"), ("note", "caption"), ("note", "quote"),
    ]


def test_to_lines_adds_spacers_before_headings_and_flattens_text() -> None:
    assert to_lines([("title", "T"), ("h2", "H"), ("p", "a\tb\nc")]) == "title\tT\nspace\t\nh2\tH\np\ta b c\n"

from mail_dock.infrastructure.parsing.html_to_text import html_to_text


def test_html_to_text_removes_non_content_and_compresses_whitespace() -> None:
    html = """
    <p>  Hello   <strong>world</strong>  </p>
    <script>window.secret = true;</script>
    <style>.secret { display: none; }</style>
    <!-- hidden comment -->
    <p>First<br><br><br>Second\tline</p>
    """

    assert html_to_text(html) == "Hello\nworld\n\nFirst\nSecond line"


def test_html_to_text_handles_multimegabyte_html() -> None:
    html = f"<div>{'payload ' * 300_000}</div><script>discarded</script>"

    result = html_to_text(html)

    assert result.startswith("payload payload payload")
    assert "discarded" not in result
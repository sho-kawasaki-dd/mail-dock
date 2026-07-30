from mail_dock.infrastructure.parsing.normalize import normalize_for_search


def test_normalize_for_search_applies_nfkc_and_casefold() -> None:
    assert normalize_for_search(" \uff21\uff22\uff23\u3000\uff11\uff12\uff13 ") == "abc 123"


def test_normalize_for_search_compresses_whitespace_and_strips() -> None:
    assert normalize_for_search("  Subject\twith\nspaces  ") == "subject with spaces"


def test_normalize_for_search_keeps_hiragana_and_katakana_distinct() -> None:
    assert normalize_for_search("かな") != normalize_for_search("カナ")
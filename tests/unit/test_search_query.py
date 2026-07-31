import pytest

from mail_dock.domain.errors import SearchQueryError
from mail_dock.usecases.search_query import parse_query


def test_parse_query_splits_ascii_and_fullwidth_spaces() -> None:
    plan = parse_query("alpha　 beta   gamma")

    assert plan.match_terms == ('"alpha"', '"beta"', '"gamma"')
    assert plan.has_slow_path is False


def test_parse_query_normalizes_before_routing_by_length() -> None:
    plan = parse_query("\uff34\uff25\uff33\uff34\u3000\uff21\uff22")

    assert plan.match_terms == ('"test"',)
    assert plan.like_terms == ("ab",)
    assert plan.has_slow_path is True


def test_parse_query_supports_phrases_and_internal_quotes() -> None:
    plan = parse_query('"hello world" "say ""hi"""')

    assert plan.match_terms == ('"hello world"', '"say ""hi"""')


def test_parse_query_separates_exclusions_and_supports_or_mode() -> None:
    plan = parse_query('alpha -beta　-"gamma delta"', mode="or")

    assert plan.match_terms == ('"alpha"',)
    assert plan.exclude_match_terms == ('"beta"', '"gamma delta"')
    assert plan.mode == "or"


def test_parse_query_escapes_like_terms_including_backslash_first() -> None:
    plan = parse_query(r"\%")

    assert plan.like_terms == (r"\\\%",)
    assert plan.has_slow_path is True


@pytest.mark.parametrize(
    "query",
    ["", "　", "-alpha", '""', '"unfinished', "-", 'alpha"beta'],
)
def test_parse_query_rejects_invalid_queries(query: str) -> None:
    with pytest.raises(SearchQueryError):
        parse_query(query)


def test_parse_query_escapes_fts5_syntax_characters() -> None:
    plan = parse_query("*^:NEAR")

    assert plan.match_terms == ('"*^:near"',)

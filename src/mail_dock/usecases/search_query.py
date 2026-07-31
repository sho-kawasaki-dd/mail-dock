"""Parse user search text into an executable, normalized search plan."""

from __future__ import annotations

from typing import Literal

from mail_dock.domain.errors import SearchQueryError
from mail_dock.domain.normalize import normalize_for_search
from mail_dock.domain.search import SearchPlan


def parse_query(
    text: str,
    *,
    mode: Literal["and", "or"] = "and",
) -> SearchPlan:
    """Parse a search query using whitespace, phrases, and exclusions.

    Terms are normalized with the same function used while indexing. FTS
    terms are quoted so FTS5 syntax characters are treated as literals, while
    LIKE terms are escaped for an eventual ``%term%`` pattern.
    """

    if mode not in ("and", "or"):
        raise SearchQueryError("search mode must be 'and' or 'or'")

    tokens = _tokenize(text)
    if not tokens:
        raise SearchQueryError("search query must contain a term")

    match_terms: list[str] = []
    like_terms: list[str] = []
    exclude_match_terms: list[str] = []
    exclude_like_terms: list[str] = []

    for excluded, raw_term in tokens:
        term = normalize_for_search(raw_term)
        if not term:
            raise SearchQueryError("search query contains an empty term")

        if len(term) >= 3:  # trigram routing follows Python string length.
            parsed_term = _escape_fts_term(term)
            target = exclude_match_terms if excluded else match_terms
        else:
            parsed_term = _escape_like_term(term)
            target = exclude_like_terms if excluded else like_terms
        target.append(parsed_term)

    return SearchPlan(
        match_terms=tuple(match_terms),
        like_terms=tuple(like_terms),
        exclude_match_terms=tuple(exclude_match_terms),
        exclude_like_terms=tuple(exclude_like_terms),
        mode=mode,
        has_slow_path=bool(like_terms or exclude_like_terms),
    )


def _tokenize(text: str) -> list[tuple[bool, str]]:
    tokens: list[tuple[bool, str]] = []
    position = 0
    length = len(text)

    while position < length:
        while position < length and text[position].isspace():
            position += 1
        if position == length:
            break

        excluded = text[position] == "-"
        if excluded:
            position += 1
            if position == length or text[position].isspace():
                raise SearchQueryError("search query contains an invalid exclusion")

        if text[position] == '"':
            raw_term, position = _read_phrase(text, position)
        else:
            start = position
            while position < length and not text[position].isspace():
                if text[position] == '"':
                    raise SearchQueryError("search query contains an invalid quote")
                position += 1
            raw_term = text[start:position]

        if not raw_term:
            raise SearchQueryError("search query contains an empty phrase")
        tokens.append((excluded, raw_term))

    if tokens and all(excluded for excluded, _ in tokens):
        raise SearchQueryError("search query must contain an included term")
    return tokens


def _read_phrase(text: str, start: int) -> tuple[str, int]:
    position = start + 1
    characters: list[str] = []

    while position < len(text):
        character = text[position]
        if character != '"':
            characters.append(character)
            position += 1
            continue

        if position + 1 < len(text) and text[position + 1] == '"':
            characters.append('"')
            position += 2
            continue

        position += 1
        if position < len(text) and not text[position].isspace():
            raise SearchQueryError("quoted phrases must be separated by whitespace")
        return "".join(characters), position

    raise SearchQueryError("search query contains an unterminated quote")


def _escape_fts_term(term: str) -> str:
    return f'"{term.replace(chr(34), chr(34) * 2)}"'


def _escape_like_term(term: str) -> str:
    escaped = term.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%")
    return escaped.replace("_", "\\_")

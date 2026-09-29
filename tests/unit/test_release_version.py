import pytest

from tools.verify_version import tag_matches_version


@pytest.mark.parametrize(
    ("tag", "version", "matches"),
    [
        ("v0.1.0", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("v0.1.1", "0.1.0", False),
    ],
)
def test_tag_matches_version(tag: str, version: str, matches: bool) -> None:
    assert tag_matches_version(tag, version) is matches

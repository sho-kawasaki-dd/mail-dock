import argparse
import sys

from mail_dock import __version__


def tag_matches_version(tag: str, version: str) -> bool:
    return tag == f"v{version}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a release tag against the app version.")
    parser.add_argument("tag", help="Release tag, for example v0.1.0")
    tag = parser.parse_args().tag

    expected_tag = f"v{__version__}"
    if not tag_matches_version(tag, __version__):
        print(f"Release tag mismatch: expected {expected_tag}, got {tag}", file=sys.stderr)
        return 1

    print(f"Release tag {tag} matches mail-dock {__version__}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

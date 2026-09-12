r"""Phase 4.5 グループA PoC 用: readpst の出力ディレクトリ名生成をWindows上で再現して実測する。

libpst 0.6.76 の src/readpst.c から、-e (MODE_SEPARATE) 経路で使われる次の2関数を忠実に移植する。

    void check_filename(char *fname) {
        while ((t = strpbrk(t, "/\\\\:"))) *t = '_';
    }

    void mk_separate_dir(char *dir) {
        do {
            if (y == 0) snprintf(dir_name, "%s", dir);
            else        snprintf(dir_name, "%s%i", dir, y);
            check_filename(dir_name);
            if (D_MKDIR(dir_name)) {
                if (errno != EEXIST) DIE(...);   // 変換全体が異常終了する
            } else break;
            y++;
        } while (overwrite == 0);
        if (chdir(dir_name)) DIE(...);
    }

つまりサニタイズされるのは `/` `\` `:` のみで、それ以外の名前はそのまま `_mkdir` へ渡る。
本スクリプトは各候補名について「置換後の名前」「mkdirの成否とerrno」「最終ディレクトリ名」
「stagingルート外へ出たか」を実測する。

使い方:
    python tools/pst_poc/simulate_readpst_dirnames.py <work_dir>
"""

from __future__ import annotations

import errno
import shutil
import sys
import unicodedata
from pathlib import Path

_SEPARATORS = "/\\:"


def check_filename(name: str) -> str:
    return "".join("_" if ch in _SEPARATORS else ch for ch in name)


def mk_separate_dir(parent: Path, raw_name: str) -> tuple[str | None, str, str]:
    """readpst の mk_separate_dir を再現する。戻り値は (作成名, 結果, 詳細)。"""
    y = 0
    while True:
        candidate = raw_name if y == 0 else f"{raw_name}{y}"
        candidate = check_filename(candidate)
        target = parent / candidate
        try:
            target.mkdir()
        except FileExistsError:
            y += 1
            if y > 20:
                return None, "loop", "EEXIST loop exceeded"
            continue
        except OSError as exc:
            # readpst はここで DIE() し、変換全体が終了コード1で異常終了する
            error_number = exc.errno
            name = (
                errno.errorcode.get(error_number, str(error_number))
                if error_number is not None
                else "UNKNOWN"
            )
            return None, "DIE", f"{name}: {exc.strerror}"
        return candidate, "ok", ""


CANDIDATES: list[tuple[str, str]] = [
    ("colon", "a:b"),
    ("backslash", "a\\b"),
    ("slash", "a/b"),
    ("asterisk", "a*b"),
    ("question", "a?b"),
    ("quote", 'a"b'),
    ("lt", "a<b"),
    ("gt", "a>b"),
    ("pipe", "a|b"),
    ("reserved_con", "CON"),
    ("reserved_prn", "PRN"),
    ("reserved_nul", "NUL"),
    ("reserved_aux", "AUX"),
    ("reserved_com1", "COM1"),
    ("reserved_lpt1", "LPT1"),
    ("reserved_ext", "CON.txt"),
    ("trail_dot", "trail."),
    ("trail_space", "trail "),
    ("lead_space", " lead"),
    ("dot", "."),
    ("dotdot", ".."),
    ("traversal", "..\\..\\evil"),
    ("abs_path", "C:\\Windows\\Temp\\evil"),
    ("drive_rel", "C:evil"),
    ("unc", "\\\\server\\share\\evil"),
    ("ads", "note.txt:hidden"),
    ("long_name", "N" * 250),
    ("nfc", "\u304c" + "test"),
    ("nfd", "\u304b\u3099" + "test"),
    ("dup_1", "SameName"),
    ("dup_2", "SameName"),
    ("control_char", "a\tb"),
]


def main() -> int:
    # NFD など CP932 で表現できない名前を出力する
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="backslashreplace")
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    root = Path(sys.argv[1]).resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    rows: list[tuple[str, str, str, str, str, str]] = []
    for label, raw in CANDIDATES:
        sanitized = check_filename(raw)
        created, status, detail = mk_separate_dir(root, raw)
        if created is None:
            escaped = "-"
            actual = "-"
        else:
            resolved = (root / created).resolve()
            escaped = "NO" if resolved.is_relative_to(root) else "YES"
            # Windows が末尾ドット/空白を除去した場合などに実際の名前を拾う
            actual = next(
                (p.name for p in root.iterdir() if p.resolve() == resolved),
                created,
            )
        shown_raw = raw if len(raw) <= 32 else f"{raw[:29]}...({len(raw)})"
        shown_san = sanitized if len(sanitized) <= 32 else f"{sanitized[:29]}...({len(sanitized)})"
        shown_act = actual if len(actual) <= 32 else f"{actual[:29]}...({len(actual)})"
        rows.append((label, shown_raw, shown_san, f"{status} {detail}".strip(), shown_act, escaped))

    header = ("id", "raw", "sanitized", "result", "actual", "escaped")
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(6)]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    print("  ".join("-" * widths[i] for i in range(6)))
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(6)))

    print()
    print("NFC/NFD on-disk entries:")
    for p in sorted(root.iterdir()):
        if "test" in p.name:
            print(f"  {p.name!r} NFC={unicodedata.is_normalized('NFC', p.name)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

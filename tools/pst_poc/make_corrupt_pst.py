"""Phase 4.5 グループA PoC 用: 破損PST・非対応形式PSTのサンプルを生成する。

原本PSTは読み取り専用で開き、先頭数MiBだけを複製して各種の破損を注入する。
生成物は readpst / lspst の終了コード・stderr を観測するためだけに使う。

使い方:
    python tools/pst_poc/make_corrupt_pst.py <source.pst> <output_dir>
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# MS-PST ヘッダ: 0x00 dwMagic("!BDN") / 0x08 wMagicClient("SM") / 0x0A wVer
_OFF_MAGIC = 0x00
_OFF_MAGIC_CLIENT = 0x08
_OFF_VER = 0x0A

_PREFIX_BYTES = 4 * 1024 * 1024


def _read_prefix(source: Path) -> bytearray:
    with source.open("rb") as fh:
        return bytearray(fh.read(_PREFIX_BYTES))


def _write(out_dir: Path, name: str, data: bytes) -> Path:
    path = out_dir / name
    path.write_bytes(data)
    return path


def generate(source: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = _read_prefix(source)
    if len(prefix) < 512:
        raise SystemExit(f"source too small: {source}")

    created: list[Path] = []

    # 1. ヘッダのみ残して切り詰め: 正当なヘッダ + データ欠落
    created.append(_write(out_dir, "truncated_header.pst", bytes(prefix[:512])))

    # 2. 途中で切り詰め: BTree の参照先が欠落
    created.append(_write(out_dir, "truncated_mid.pst", bytes(prefix)))

    # 3. シグネチャ破壊
    bad_magic = bytearray(prefix)
    bad_magic[_OFF_MAGIC : _OFF_MAGIC + 4] = b"XXXX"
    created.append(_write(out_dir, "bad_magic.pst", bytes(bad_magic)))

    # 4. wMagicClient 破壊
    bad_client = bytearray(prefix)
    bad_client[_OFF_MAGIC_CLIENT : _OFF_MAGIC_CLIENT + 2] = b"ZZ"
    created.append(_write(out_dir, "bad_magic_client.pst", bytes(bad_client)))

    # 5. 未知のフォーマットバージョン
    unknown_ver = bytearray(prefix)
    unknown_ver[_OFF_VER : _OFF_VER + 2] = struct.pack("<H", 0x0000)
    created.append(_write(out_dir, "unknown_version.pst", bytes(unknown_ver)))

    # 6. Unicode PST を ANSI 14 と偽装: バージョンと実体の不一致
    ansi_ver = bytearray(prefix)
    ansi_ver[_OFF_VER : _OFF_VER + 2] = struct.pack("<H", 14)
    created.append(_write(out_dir, "version_mismatch_ansi.pst", bytes(ansi_ver)))

    # 7. ヘッダ直後を0埋め: BREF が全滅した状態
    zeroed = bytearray(prefix)
    zeroed[0x100:0x400] = b"\x00" * 0x300
    created.append(_write(out_dir, "zeroed_bref.pst", bytes(zeroed)))

    # 8. 空ファイル
    created.append(_write(out_dir, "empty.pst", b""))

    # 9. 非対応形式: PSTではないファイルに .pst 拡張子
    created.append(_write(out_dir, "not_a_pst.pst", b"This is a plain text file.\r\n" * 64))

    # 10. 非対応形式: ZIP シグネチャ
    created.append(_write(out_dir, "zip_as_pst.pst", b"PK\x03\x04" + b"\x00" * 1024))

    return created


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    with source.open("rb") as fh:
        head = fh.read(16)
    print(f"source header: {head.hex(' ')}")
    print(f"source wVer  : {struct.unpack_from('<H', head, _OFF_VER)[0]}")
    for path in generate(source, out_dir):
        print(f"created {path.name} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

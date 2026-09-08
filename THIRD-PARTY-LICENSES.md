# Third-Party Licenses

mail-dock uses the following third-party packages. License names and project links are recorded here for release and source-distribution review. Exact dependency versions are resolved in `uv.lock`.

| Dependency | License | Project |
| --- | --- | --- |
| PySide6 / Qt for Python | LGPL-3.0-only, GPL-2.0-only, GPL-3.0-only, or commercial (Qt licensing options) | https://doc.qt.io/qtforpython/ |
| Qt WebEngine / Chromium (via PySide6) | Qt WebEngine is covered by the applicable Qt LGPL/GPL or commercial license; Chromium and bundled third-party components retain their respective upstream licenses | https://doc.qt.io/qt-6/qtwebengine-licenses.html |
| keyring | MIT | https://github.com/jaraco/keyring |
| beautifulsoup4 | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| charset-normalizer | MIT | https://github.com/jawah/charset_normalizer |
| platformdirs | MIT | https://github.com/platformdirs/platformdirs |
| readpst / libpst 0.6.76 | GPL-2.0-or-later | https://github.com/buggins/libpst |
| MSYS2 UCRT64 runtime DLLs bundled with readpst | See the corresponding upstream package licenses | https://packages.msys2.org/ |

The application is distributed under GPL-3.0-or-later. The license terms of each dependency apply to that dependency; this file is an inventory, not a replacement for the upstream license texts.

## Bundled readpst artifacts

The Windows converter was obtained from the MSYS2 package
`mingw-w64-ucrt-x86_64-libpst` in the UCRT64 environment. The observed
converter version is `readpst / libpst v0.6.76` and the companion utility
reports `lspst / libpst v0.6.76`. The package identifies the license as
GPL-2.0-or-later. `vendor/readpst/COPYING` is the corresponding license text.

`readpst.exe` and `lspst.exe` load the following MSYS2 UCRT64 runtime DLLs.
Windows system DLLs reported by `ldd` are not bundled.

| Artifact | Role | License/source | SHA-256 |
| --- | --- | --- | --- |
| `vendor/readpst/readpst.exe` | PST to EML converter | GPL-2.0-or-later / [libpst](https://github.com/buggins/libpst) | `727EB1EB629A3FAB2B0CFBE261762D1AD0892A6D8C3740D3731DB84A5FC7897B` |
| `vendor/readpst/lspst.exe` | PST inspection utility | GPL-2.0-or-later / [libpst](https://github.com/buggins/libpst) | `E40672A956D82E699BCD0D164777B55B62395273DF3DF1D55636853AC3659433` |
| `vendor/readpst/libpst-4.dll` | libpst runtime | GPL-2.0-or-later / [libpst](https://github.com/buggins/libpst) | `63DD6EEC7D8A5498B5DEABCC45E41938A1A9185E788558FBE5A30FC38487C0C5` |
| `vendor/readpst/libgcc_s_seh-1.dll` | GCC runtime | [MSYS2 package repository](https://packages.msys2.org/) | `80940372431CC76224DFDA06E2D33F01E49AF3B4E7C499C535BE856EBCADD273` |
| `vendor/readpst/libgsf-1-114.dll` | Structured file format runtime | [MSYS2 package repository](https://packages.msys2.org/) | `2F580738C80ED218E17283DAA3ECC140F4866CAEF3468B4E9A773DAE7E98B882` |
| `vendor/readpst/libgobject-2.0-0.dll` | GLib object runtime | [MSYS2 package repository](https://packages.msys2.org/) | `469D2E3B60F5307D00809F1EFC0AB494F7C230E58A93CD2CBF75FB64AA2AA786` |
| `vendor/readpst/libsystre-0.dll` | TRE compatibility runtime | [MSYS2 package repository](https://packages.msys2.org/) | `672F96C284704F0CE74B2E105F5DBE8AA29CB0B822537102114EBAF0D1672686` |
| `vendor/readpst/libwinpthread-1.dll` | POSIX thread runtime | [MSYS2 package repository](https://packages.msys2.org/) | `CD5FC7573F1ECF9A157BB62FBC35F8B97862DDE6057FFABD6CEBB3FC9FE6FF1D` |
| `vendor/readpst/zlib1.dll` | Compression runtime | [MSYS2 package repository](https://packages.msys2.org/) | `11CD91171765CD65F1C3362E3DF67E5F5A618A6B88C8CDEC899B802EAF1C7B0D` |
| `vendor/readpst/libiconv-2.dll` | Character conversion runtime | [MSYS2 package repository](https://packages.msys2.org/) | `94EC68FB8D342B6C91B6B48A8D95092819CDCD006A7166E0B68062F946AFD08A` |
| `vendor/readpst/libbz2-1.dll` | bzip2 compression runtime | [MSYS2 package repository](https://packages.msys2.org/) | `1790BF6473C9D48DB14C1F0EE8124142327556D281299EA2A72EE361118EE01A` |
| `vendor/readpst/libintl-8.dll` | gettext runtime | [MSYS2 package repository](https://packages.msys2.org/) | `0E5A178731E6BE6D115442A6ADE3DF7F03265CD1FC4338F8B6F86A8001CBF582` |
| `vendor/readpst/libglib-2.0-0.dll` | GLib runtime | [MSYS2 package repository](https://packages.msys2.org/) | `052E1D5E9CB072C7D2FB9DB3EA6C29C1889B05110B1228E4FB846884EED387DD` |
| `vendor/readpst/libstdc++-6.dll` | C++ runtime | [MSYS2 package repository](https://packages.msys2.org/) | `69200D6D96B903DAC4C873031813485A0455828BE8101479EFC56008862420CC` |
| `vendor/readpst/libffi-8.dll` | Foreign-function interface runtime | [MSYS2 package repository](https://packages.msys2.org/) | `F96CF79B75F33A10023D001673B564538023395C94B3C58E1DB53114976A7C25` |
| `vendor/readpst/libtre-5.dll` | TRE regular expression runtime | [MSYS2 package repository](https://packages.msys2.org/) | `1014AFB8649E6EBA839748CC0AD5DC542249DF831EE483F7A93E465E223F7BAE` |
| `vendor/readpst/libgio-2.0-0.dll` | GIO runtime | [MSYS2 package repository](https://packages.msys2.org/) | `DA8ABBCBE4EA8CE650C6CB5953F775699A6CD2126D3291FAA35B83CC0659CC12` |
| `vendor/readpst/libxml2-16.dll` | XML runtime | [MSYS2 package repository](https://packages.msys2.org/) | `7533A35F8095467CCCFEC6259BAD20394EFD3C47B7406481AEA454B85F769A54` |
| `vendor/readpst/libgmodule-2.0-0.dll` | GLib module runtime | [MSYS2 package repository](https://packages.msys2.org/) | `702E4C3A331D116CF96AC4FB4F04DEC64AEA7F8236220A639E123EC033B7D4B7` |
| `vendor/readpst/libpcre2-8-0.dll` | PCRE2 regular expression runtime | [MSYS2 package repository](https://packages.msys2.org/) | `752F04BB7A60D2A049788FFAEC14B45602C4DEC4C29534001DFCF434D26DA19C` |
| `vendor/readpst/COPYING` | GPL license text for libpst | GPL-2.0-or-later / [libpst](https://github.com/buggins/libpst) | `8177F97513213526DF2CF6184D8FF986C675AFB514D4E68A404010521B880643` |

The hashes above were calculated on 2026-09-08 with PowerShell
`Get-FileHash -Algorithm SHA256` against the files currently present in
`vendor/readpst/`. Recalculate them whenever the MSYS2 package or any bundled
runtime DLL is refreshed. The exact package versions and licenses of the
transitive runtime DLLs must be recorded from their corresponding MSYS2
package metadata before a release.

## Release review

Before a release, confirm the license metadata and notices against the exact versions selected by `uv.lock`, including transitive dependencies and any bundled Qt components.
For Qt WebEngine, also review the Qt WebEngine Licenses and Attributions page and the notices shipped with the exact Qt distribution used to build the application. This covers the Chromium engine and its bundled third-party components.

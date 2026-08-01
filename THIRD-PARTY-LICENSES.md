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

The application is distributed under GPL-3.0-or-later. The license terms of each dependency apply to that dependency; this file is an inventory, not a replacement for the upstream license texts.

<!-- The readpst dependency and its license notice will be added in Phase 4.5 when PST archive support is introduced. -->

## Release review

Before a release, confirm the license metadata and notices against the exact versions selected by `uv.lock`, including transitive dependencies and any bundled Qt components.
For Qt WebEngine, also review the Qt WebEngine Licenses and Attributions page and the notices shipped with the exact Qt distribution used to build the application. This covers the Chromium engine and its bundled third-party components.

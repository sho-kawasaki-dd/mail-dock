
# mail-dock

mail-dock is a desktop application for backing up mail from IMAP servers to a local external drive as `.eml` files and browsing the archive offline. The local EML files and persistent manifests are the source of truth; the SQLite database is a rebuildable metadata cache.

## Storage and backup prerequisites

- Block-level encryption is recommended for the storage root, but encryption is not a hard requirement. The setup wizard records one of `encrypted`, `unencrypted`, or `unknown` as a user declaration and keeps that status visible. The application does not attempt to detect the encryption product or prove that the storage is encrypted.
- Keep mail credentials on the PC side in the approved OS credential store (or in the process-only `session_only` mode). Never put credentials under the storage root.
- Follow the [3-2-1 backup rule](docs/ローカルメールバックアップand閲覧アプリ開発計画書.md#57): keep at least three copies, on two different media, with one copy off-site.
- For a device-encrypted volume, a normal file copy of the mounted storage root is sufficient. Keep the root structure together, including the EML files, manifests, and metadata database. For a VeraCrypt file container, stop mail-dock, unmount the volume, and copy the container file in full. Do not use differential backups or copy a container while it is mounted. If the backup destination has weaker encryption than the source, keep `db_backup_to_local_disk` disabled unless you explicitly accept the warning.

## Storage encryption guide

### Three storage safety levels

| Level | Recommendation | Examples | Operational meaning |
| --- | --- | --- | --- |
| Supported | Recommended | BitLocker To Go, VeraCrypt, LUKS, encrypted APFS | The mounted volume is a normal file system. The application still runs a storage compatibility self-test for locking, replacement, fsync, WAL, case behavior, and long paths. |
| Unsupported | Do not use unless the self-test reports otherwise | Cryptomator, gocryptfs, rclone crypt, Boxcryptor, and similar virtual file systems | Atomic replacement, exclusive locks, fsync, and SQLite behavior depend on the implementation. The product name is not detected; a failed capability test is reported as `UNSUPPORTED` or `DEGRADED`. |
| Unencrypted | Self-responsibility | An unencrypted local or removable volume | Explicitly declare `unencrypted`. mail-dock permits the choice, shows the warning continuously, and asks for confirmation once immediately before the first sync. |

The self-test is a compatibility probe, not a security guarantee. A successful one-off I/O operation cannot prove full atomicity, durability, or WAL safety. The test uses temporary files under the storage root's `tmp/` directory and never modifies the production lock, database, EML, or manifest files.

### OS-specific setup

- **Windows Pro:** Use BitLocker To Go for a removable drive. Turn on BitLocker for the volume, store the recovery key separately from the drive, and unlock the volume before starting mail-dock.
- **Windows Home:** Use VeraCrypt or another block-level encryption option that provides a normal mounted file system. Windows Home can unlock and read/write a drive that was already encrypted with BitLocker, but it cannot create or manage BitLocker encryption in the same way as Pro.
- **macOS:** Use an encrypted APFS external volume, created with Disk Utility or the equivalent system workflow. Unlock and mount it before starting mail-dock, and keep the recovery information separate from the archive drive.
- **Linux:** Use LUKS for the device or volume, then mount a normal file system inside the unlocked volume. VeraCrypt is also supported when its mounted volume behaves as a normal local file system.

### VeraCrypt requirements

For a dedicated external SSD, encrypt the whole device rather than using a file container. If a file container is needed to share the drive with other uses, all four conditions are mandatory:

1. Use a fixed-size container. Do not use a dynamic container because the host file system can run out of space without the application seeing the true limit.
2. Keep the container outside cloud-synchronization folders and network shares.
3. Disable automatic unmounting, including unmount-on-screen-saver or idle-timeout behavior.
4. Back up the VeraCrypt volume header separately and verify that the recovery procedure works.

Disable vault idle auto-lock and VeraCrypt automatic unmount while mail-dock is running. A multi-hour initial sync can be interrupted by either event just like a physical drive removal. Before shutting down or transporting the drive, stop synchronization, close mail-dock, and explicitly unmount the encrypted volume.

## Safely ejecting the storage drive

Use the "Storage" menu's "Safely eject storage" action before physically removing the drive. It waits for the running sync/verify worker to stop at a batch boundary, checkpoints the WAL, closes every SQLite connection and log handle, releases the storage lock, and then reports that the drive can be removed. Do not pull the drive while a sync or verify is in progress; wait for the action to reach the "safe to remove" state.

In Windows, set the removable drive's policy to **Quick removal** (Disk Management / Device Manager policy tab) rather than "Better performance". Quick removal disables the Windows write cache for the device, which keeps `os.replace` and fsync behavior consistent with what mail-dock assumes. Avoid running the archive drive through a USB hub or on bus power, since power drops on those paths are a common cause of unexpected detachment.

## Gmail accounts and Google OAuth verification status

mail-dock does not ship or proxy its own Google OAuth client. Each user creates their own Google Cloud project, configures its OAuth consent screen, and registers their own Gmail account(s) as test users (step-by-step console instructions land with the Phase 5.2a client-setup task; the verification procedure used to validate this policy is in [手順書_Phase5_GroupG_Gmail-OAuth2-PoC.md](docs/手順書_Phase5_GroupG_Gmail-OAuth2-PoC.md)).

Google's own guidance on ["when verification is not needed"](https://support.google.com/cloud/answer/13464323) draws a hard line between self/known-user operation and public release:

- **Self-use or a small number of known users (mail-dock's intended usage):** keep the consent screen's publishing status at **"Testing"**, and add only yourself (and any other trusted users, up to 100) as test users. This falls under Google's own exemption categories ("Personal Use apps" and "Development/Testing/Staging apps"), so **no Google verification review and no CASA security assessment (paid, annual) are required**.
- **General public release:** publishing the consent screen as "In production" for unknown/unlimited users would require Google's verification review and, for the restricted `https://mail.google.com/` scope, the CASA assessment. mail-dock does not pursue this path — it is out of scope for this project.

Trade-offs of staying in "Testing" status:

- Every sign-in shows Google's "Google hasn't verified this app" warning screen (click through "Advanced" → "Go to (app name)").
- A refresh token for the restricted `https://mail.google.com/` scope expires after **7 days** while the consent screen is in "Testing" status. IMAP connection attempts after expiry fail with `invalid_grant`, at which point the account needs to be re-authorized with Google.
- The consent screen is capped at 100 test users while in "Testing" status.

## Microsoft 365 and Outlook.com accounts

Microsoft IMAP uses OAuth2 with the user's own Microsoft Entra application registration. In the Azure portal, create an app registration and choose the account types that match the intended accounts (work/school accounts, personal Microsoft accounts, or both). Under **Authentication**, add the **Mobile and desktop applications** platform with the redirect URI `http://localhost`; mail-dock uses that registered loopback URI with a dynamically assigned port. Do not configure a client secret for this public desktop client.

Under **API permissions**, add the delegated permission `IMAP.AccessAsUser.All` for Office 365 Exchange Online and grant consent if the tenant requires it. The app also requests `offline_access` so the account can refresh its access token. In mail-dock, choose OAuth2 and Microsoft 365 / Outlook.com, enter the Application (client) ID, select `organizations` for work/school accounts or `consumers` for personal Outlook.com accounts (or enter the tenant ID), and authenticate in the browser. The default IMAP endpoint is `outlook.office365.com` on port 993 with implicit TLS.

Only the signed-in user's mailbox is supported. Shared mailboxes and delegated access are out of scope; do not enter another mailbox address as a substitute for the authenticating user's account.

## Installing on Windows

Download `mail-dock-{version}-setup.exe` from the project's GitHub Releases and run it. The installer is per-user by default (`%LOCALAPPDATA%\Programs\mail-dock`, no administrator rights needed); choose the all-users option in the privileges dialog, or pass `/ALLUSERS`, to install under `Program Files`. The installer never accesses the network.

The installer and executable are **not code-signed**, so Windows SmartScreen may show an "unknown publisher" warning. Choose "More info" and then "Run anyway" after verifying the file against `SHA256SUMS.txt` from the same release.

The distributed `mail-dock.exe` is the GUI only. The command-line subcommands (`sync`, `verify`, `reindex`, ...) are available only in a development environment through `uv run mail-dock ...`; the installed executable accepts just `gui` (default) and `self-check`. `mail-dock.exe self-check --output result.json` runs a read-only diagnostic of the installed environment, and the same check is available from Help > About.

### Uninstalling

Uninstalling removes the program files. For a per-user install, the uninstaller asks whether to also delete mail-dock's settings file, application log, and its `HKCU\Software\mail-dock\mail-dock` registry key; the default is to keep them. Mail data in the storage root (EML files, `metadata.db`, manifests) and credentials saved in the Windows Credential Manager are never deleted. If the settings directory cannot be verified as safe (for example, it contains a storage root), nothing is deleted. A silent or all-users uninstall leaves every user's settings in place.

## Building from source

Building the installer requires Windows, Python 3.13, uv, MSYS2 (UCRT64 with `zstd`), the Windows SDK (`mt.exe`), and Inno Setup 6.3 or later. Download the locked Qt `qtbase` and `qtwebengine` source archives listed in `packaging/qt/qt-source.lock.json`, then run:

```powershell
pwsh -NoProfile -File .\tools\build_windows.ps1 `
  -QtLicenseSource <path-to-qtbase-everywhere-src-*.tar.xz> `
  -QtWebEngineSource <path-to-qtwebengine-everywhere-src-*.tar.xz> `
  -QtWebEngineSha256 <sha256-from-the-lock-file> `
  -CompileInstaller
```

The script fetches readpst from the pinned MSYS2 packages, collects licenses, builds the PyInstaller onedir image into `dist/mail-dock/`, fetches the Qt corresponding source, verifies the bundle (including `self-check --require-keyring`), and compiles the installer into `dist/`. Pushing a `v{version}` tag (which must equal `mail_dock.__version__`) runs the release workflow and creates a draft GitHub release.

### Corresponding source

mail-dock is GPL-3.0-or-later. Every release provides, next to the installer, `mail-dock-{version}-src.tar.gz` (this repository), `mail-dock-{version}-readpst-corresponding-source.zip` (readpst/libpst and the bundled MSYS2 runtime DLLs), and `mail-dock-{version}-qt-corresponding-source.zip` (Qt and PySide6, listed with hashes in the bundled `QT-SOURCE.md`). The installed `licenses` folder and `THIRD-PARTY-LICENSES.md` contain the license texts and notices.

## Development setup

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), and Git. From the repository root:

```sh
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not docker and not gui"
```

The default test command excludes Docker-based integration tests and GUI tests, and is suitable for the Windows mock-based development path.

## PST converter and licensing

PST import uses the independently executed `readpst` and `lspst` programs from
the MSYS2 UCRT64 package `mingw-w64-ucrt-x86_64-libpst`. The converter is not
built into the Python process and the application does not modify the source
PST. PST-to-EML conversion reconstructs messages, so keep the original PST in
your own long-term backup set after importing it.

On Windows, install MSYS2 with the UCRT64 environment and the Windows SDK
(for `mt.exe`), then fetch the converter and its runtime DLLs with:

```powershell
pwsh -NoProfile -File .\tools\fetch_readpst.ps1 -Msys2Root C:\msys64
```

The script reads the installed MSYS2 package metadata, copies the converter
and its DLL dependencies into `vendor/readpst/`, applies the tracked
`readpst.exe.manifest` (`activeCodePage=UTF-8` and `longPathAware=true`), and
downloads the corresponding libpst source archive pinned to the package
commit. It writes `readpst-artifacts.json` and `SHA256SUMS`; the manifest
contains the package versions, licenses, source URL, and the SHA-256 values
before and after the Windows manifest resource patch.

The GPL-2.0-or-later notice is kept in `vendor/readpst/COPYING`. Do not replace
the source archive with a URL-only reference when preparing a release: the
release workflow checks that the converter binaries, GPL notice, corresponding
source archive, provenance manifest, and checksums are all present, and fails
the release if any required asset is missing.

## GUI

Start the desktop application with either command:

```sh
uv run mail-dock
uv run mail-dock gui
```

The first command starts the GUI when no subcommand is provided. The GUI setup wizard is shown when no valid storage root has been configured.

Run GUI tests locally with the GUI marker enabled. On headless Linux environments, use Qt's offscreen platform:

```sh
MAILDOCK_GUI=1 QT_QPA_PLATFORM=offscreen uv run pytest -m gui
```

To run all tests that do not require Docker, including GUI tests, use:

```sh
MAILDOCK_GUI=1 QT_QPA_PLATFORM=offscreen uv run pytest -m "not docker"
```

## WSL Docker tests

Run the GreenMail and Dovecot integration environments from WSL/Linux:

```sh
docker compose -f tests/docker/compose.yaml up -d
MAILDOCK_DOCKER=1 uv run pytest -m docker
docker compose -f tests/docker/compose.yaml down
```

The services expose GreenMail on IMAP `3143` / IMAPS `3993` and Dovecot on
IMAP `3144` / IMAPS `3994`. Both use the test account `testuser` with password
`password`. Dovecot also provides `Sent`, `Drafts`, and `Trash` SPECIAL-USE
mailboxes plus the Japanese `受信トレイ.請求書` hierarchy with `.` as the
folder delimiter.

Use the application CLI with `uv run mail-dock migrate` or `uv run mail-dock verify`. The `--storage-root` option selects an archive root; `migrate` applies database migrations and `verify` performs read-only integrity checks.

`verify` accepts `--mode quick|range|full|orphans|manifest` (default `quick`):

```sh
uv run mail-dock verify --storage-root D:\mail-archive --mode quick
uv run mail-dock verify --storage-root D:\mail-archive --mode full --account main-onamae
uv run mail-dock verify --storage-root D:\mail-archive --mode orphans
uv run mail-dock verify --storage-root D:\mail-archive --mode manifest
```

`quick` checks that each message's file exists and matches its recorded size. `range` re-hashes only the EML files written since the last checkpoint. `full` re-hashes every EML and accepts `--account` to limit the scope. `orphans` scans for EML files that are not registered in the database. `manifest` validates manifest checksums and repairs an incomplete trailing record.

`reindex` discards `metadata.db` and rebuilds it from the EML files and persistent manifests. It asks for confirmation before running and accepts `--account` to limit the rebuilt accounts:

```sh
uv run mail-dock reindex --storage-root D:\mail-archive
```

Neither `verify` nor `reindex` deletes mail; there is no CLI subcommand for server deletion or local purge. Those destructive actions are available only from the GUI, where they require an explicit confirmation flow.

## FTS PoC checks

The FTS benchmark is manual and is not included in pytest. Generate the
planned corpora and run the A-3 measurements with:

```sh
uv run python tools/bench_fts.py --measure --results tools/.bench_fts/a3.json
```

The default measurement covers 1,000, 5,000, and 10,000 messages. It reports
the database and FTS sizes, MATCH cases for 3/5/10-character terms (single,
AND, OR, and exclusion), the two-character LIKE scan, insert throughput with
and without FTS triggers, first-page and deep keyset paging, and structured
filters. The final section linearly extrapolates p95 latency and FTS size to
50,000 messages. `PASS` means MATCH p95 is at most 300 ms, LIKE p95 is at most
3 seconds, and the FTS-to-search-payload ratio is at most 5x; a ratio below 1x
is valid for the external-content schema because the source text is not
duplicated in the FTS table.

Use `--warmups N` and `--iterations N` to control the timing sample. The full
per-dataset measurements and extrapolation are written to the JSON path passed
with `--results`; the `queries`, `sorting`, `structured_filter`, and
`insert_throughput` objects contain the individual p50/p95 values and hit
counts. A clean rerun can remove previously generated corpora with `--force`:

```sh
uv run python tools/bench_fts.py --measure --force \
	--warmups 2 --iterations 7 \
	--output tools/.bench_fts --results tools/.bench_fts/a3.json
```

Run the A-4 trigram behavior checks, including short-term MATCH behavior,
escaping, LIKE reuse, and `detail=` comparisons, with:

```sh
uv run python tools/bench_fts.py --check-a4 --counts 1000 --results tools/.bench_fts/a4.json
```

Generated EML files, databases, and JSON reports are local benchmark output.

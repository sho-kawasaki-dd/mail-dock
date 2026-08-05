
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

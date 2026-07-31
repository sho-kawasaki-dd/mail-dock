
# mail-dock

mail-dock is a desktop application for backing up mail from IMAP servers to a local external drive as `.eml` files and browsing the archive offline. The local EML files and persistent manifests are the source of truth; the SQLite database is a rebuildable metadata cache.

## Storage and backup prerequisites

- The storage root is intended for an external drive encrypted with BitLocker To Go. Keep the drive encrypted when it is detached or transported.
- Follow the 3-2-1 backup rule: keep at least three copies, on two different media, with one copy off-site.
- The archive is designed so that a drive-wide copy of the storage root is sufficient for backup. Keep the root structure together, including the EML files, manifests, and metadata database.

## Development setup

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/), and Git. From the repository root:

```sh
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m "not docker"
```

The default test command excludes Docker-based integration tests and is suitable for the Windows mock-based development path.

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

Use the application CLI with `uv run mail-dock`. The `--storage-root` option selects an archive root; `migrate` applies database migrations and `verify` performs read-only integrity checks.

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

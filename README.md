
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

Run the GreenMail integration environment from WSL/Linux:

```sh
docker compose -f tests/docker/compose.yaml up -d
MAILDOCK_DOCKER=1 uv run pytest -m docker
docker compose -f tests/docker/compose.yaml down
```

Use the application CLI with `uv run mail-dock`. The `--storage-root` option selects an archive root; `migrate` applies database migrations and `verify` performs read-only integrity checks.

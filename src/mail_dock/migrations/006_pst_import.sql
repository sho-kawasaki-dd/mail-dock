CREATE TABLE IF NOT EXISTS pst_imports (
	id               INTEGER PRIMARY KEY AUTOINCREMENT,
	import_uuid      TEXT NOT NULL UNIQUE,
	account_id       TEXT NOT NULL REFERENCES accounts(id),
	source_filename  TEXT NOT NULL,
	source_sha256    TEXT NOT NULL,
	source_size_bytes INTEGER,
	source_mtime     DATETIME,
	readpst_version  TEXT,
	options_json     TEXT,
	status           TEXT NOT NULL,
	is_active        INTEGER NOT NULL DEFAULT 0,
	replaces_id      INTEGER REFERENCES pst_imports(id),
	superseded_at    DATETIME,
	total_files      INTEGER,
	ingested_count   INTEGER NOT NULL DEFAULT 0,
	failed_count     INTEGER NOT NULL DEFAULT 0,
	staging_path     TEXT,
	started_at       DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
	finished_at      DATETIME,
	error_message    TEXT
);

CREATE INDEX idx_pst_src
ON pst_imports(source_sha256);

CREATE UNIQUE INDEX uq_active_pst_source
ON pst_imports(source_sha256)
WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS pst_import_items (
	import_id            INTEGER NOT NULL REFERENCES pst_imports(id),
	source_item_key      TEXT NOT NULL,
	source_relative_path TEXT NOT NULL,
	folder_relative_path TEXT NOT NULL,
	source_size_bytes    INTEGER,
	source_sha256        TEXT,
	final_relative_path  TEXT,
	message_row_id       INTEGER REFERENCES messages(id),
	status               TEXT NOT NULL,
	error_class          TEXT,
	error_message        TEXT,
	attempt_count        INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY (import_id, source_item_key)
);
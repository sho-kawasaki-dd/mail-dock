ALTER TABLE folders ADD COLUMN backfill_next_uid INTEGER;
ALTER TABLE folders ADD COLUMN initial_sync_completed INTEGER NOT NULL DEFAULT 0;

CREATE TABLE sync_failures_v2 (
	id              INTEGER PRIMARY KEY AUTOINCREMENT,
	account_id      TEXT NOT NULL,
	folder_id       INTEGER NOT NULL,
	uidvalidity     INTEGER NOT NULL DEFAULT 0,
	uid             INTEGER NOT NULL,
	error_class     TEXT NOT NULL,
	error_message   TEXT,
	attempt_count   INTEGER NOT NULL DEFAULT 1,
	first_failed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
	last_failed_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
	UNIQUE(account_id, folder_id, uidvalidity, uid)
);

INSERT INTO sync_failures_v2 (
	id,
	account_id,
	folder_id,
	uidvalidity,
	uid,
	error_class,
	error_message,
	attempt_count,
	first_failed_at,
	last_failed_at
)
SELECT
	id,
	account_id,
	folder_id,
	0,
	uid,
	error_class,
	error_message,
	attempt_count,
	first_failed_at,
	last_failed_at
FROM sync_failures;

DROP TABLE sync_failures;
ALTER TABLE sync_failures_v2 RENAME TO sync_failures;

CREATE INDEX idx_msg_file_hash
ON messages(account_id, file_hash)
WHERE file_hash IS NOT NULL;
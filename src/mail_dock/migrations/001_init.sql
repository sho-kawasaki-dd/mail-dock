CREATE TABLE IF NOT EXISTS accounts (
	id            TEXT PRIMARY KEY,
	provider_type TEXT NOT NULL,
	display_name  TEXT,
	host          TEXT,
	port          INTEGER DEFAULT 993,
	username      TEXT,
	is_enabled    INTEGER NOT NULL DEFAULT 1,
	created_at    DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS folders (
	id             INTEGER PRIMARY KEY AUTOINCREMENT,
	account_id     TEXT NOT NULL REFERENCES accounts(id),
	raw_name       TEXT NOT NULL,
	display_name   TEXT NOT NULL,
	uidvalidity    INTEGER,
	last_seen_uid  INTEGER NOT NULL DEFAULT 0,
	is_sync_target INTEGER NOT NULL DEFAULT 0,
	last_synced_at DATETIME,
	UNIQUE(account_id, raw_name)
);

CREATE TABLE IF NOT EXISTS messages (
	id                  INTEGER PRIMARY KEY AUTOINCREMENT,
	account_id          TEXT NOT NULL REFERENCES accounts(id),
	folder_id           INTEGER NOT NULL REFERENCES folders(id),
	message_id          TEXT,
	content_key         TEXT NOT NULL,
	source_item_key     TEXT NOT NULL,
	uid                 INTEGER,
	uidvalidity         INTEGER,
	remote_state        TEXT NOT NULL DEFAULT 'present',
	moved_to_folder_id  INTEGER REFERENCES folders(id),
	local_state         TEXT NOT NULL DEFAULT 'active',
	trashed_at          DATETIME,
	relative_path       TEXT,
	file_hash           TEXT,
	subject             TEXT,
	sender              TEXT,
	recipient           TEXT,
	cc                  TEXT,
	date_sent           DATETIME,
	internal_date       DATETIME,
	size_bytes          INTEGER,
	has_attachment      INTEGER NOT NULL DEFAULT 0,
	imap_flags          TEXT,
	flags_seen_at       DATETIME,
	in_reply_to         TEXT,
	references_ids      TEXT,
	thread_key          TEXT,
	last_seen_at        DATETIME,
	created_at          DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_msg_key ON messages(account_id, content_key);
CREATE INDEX idx_msg_list ON messages(folder_id, date_sent DESC);
CREATE INDEX idx_msg_thread ON messages(thread_key, date_sent);
CREATE INDEX idx_msg_trash ON messages(local_state, trashed_at);

CREATE UNIQUE INDEX uq_imap_message
ON messages(account_id, folder_id, uidvalidity, uid)
WHERE uid IS NOT NULL;

CREATE UNIQUE INDEX uq_archive_message
ON messages(account_id, folder_id, source_item_key)
WHERE uid IS NULL;

CREATE TABLE IF NOT EXISTS message_contents (
	message_id       INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
	subject_norm     TEXT,
	sender_norm      TEXT,
	body_text        TEXT,
	attachment_names TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
	subject_norm,
	sender_norm,
	body_text,
	attachment_names,
	content='message_contents',
	content_rowid='message_id',
	tokenize='trigram'
);

CREATE TRIGGER mc_ai AFTER INSERT ON message_contents BEGIN
	INSERT INTO messages_fts(rowid, subject_norm, sender_norm, body_text, attachment_names)
	VALUES (new.message_id, new.subject_norm, new.sender_norm, new.body_text, new.attachment_names);
END;

CREATE TRIGGER mc_ad AFTER DELETE ON message_contents BEGIN
	INSERT INTO messages_fts(messages_fts, rowid, subject_norm, sender_norm, body_text, attachment_names)
	VALUES ('delete', old.message_id, old.subject_norm, old.sender_norm, old.body_text, old.attachment_names);
END;

CREATE TRIGGER mc_au AFTER UPDATE ON message_contents BEGIN
	INSERT INTO messages_fts(messages_fts, rowid, subject_norm, sender_norm, body_text, attachment_names)
	VALUES ('delete', old.message_id, old.subject_norm, old.sender_norm, old.body_text, old.attachment_names);
	INSERT INTO messages_fts(rowid, subject_norm, sender_norm, body_text, attachment_names)
	VALUES (new.message_id, new.subject_norm, new.sender_norm, new.body_text, new.attachment_names);
END;

CREATE TABLE IF NOT EXISTS sync_failures (
	id             INTEGER PRIMARY KEY AUTOINCREMENT,
	account_id     TEXT NOT NULL,
	folder_id      INTEGER NOT NULL,
	uid            INTEGER NOT NULL,
	error_class    TEXT NOT NULL,
	error_message  TEXT,
	attempt_count  INTEGER NOT NULL DEFAULT 1,
	first_failed_at DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
	last_failed_at  DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
	UNIQUE(account_id, folder_id, uid)
);

CREATE TABLE IF NOT EXISTS audit_log (
	id          INTEGER PRIMARY KEY AUTOINCREMENT,
	occurred_at DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
	operation   TEXT NOT NULL,
	account_id  TEXT,
	message_id  TEXT,
	subject     TEXT,
	size_bytes  INTEGER,
	detail      TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
	key   TEXT PRIMARY KEY,
	value TEXT
);

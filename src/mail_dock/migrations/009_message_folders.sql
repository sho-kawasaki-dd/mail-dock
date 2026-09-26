CREATE TABLE message_folders (
	message_id         INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
	folder_id          INTEGER NOT NULL REFERENCES folders(id),
	uid                INTEGER,
	uidvalidity        INTEGER,
	remote_state       TEXT NOT NULL DEFAULT 'present',
	moved_to_folder_id INTEGER REFERENCES folders(id),
	imap_flags         TEXT,
	flags_seen_at      DATETIME,
	last_seen_at       DATETIME,
	PRIMARY KEY (message_id, folder_id)
);

CREATE UNIQUE INDEX uq_message_folders_uid
ON message_folders(folder_id, uidvalidity, uid)
WHERE uid IS NOT NULL;

CREATE INDEX idx_message_folders_folder
ON message_folders(folder_id, message_id);

INSERT INTO message_folders (
	message_id,
	folder_id,
	uid,
	uidvalidity,
	remote_state,
	moved_to_folder_id,
	imap_flags,
	flags_seen_at,
	last_seen_at
)
SELECT
	id,
	folder_id,
	uid,
	uidvalidity,
	remote_state,
	moved_to_folder_id,
	imap_flags,
	flags_seen_at,
	last_seen_at
FROM messages;

CREATE TABLE message_identity_aliases (
	account_id                TEXT NOT NULL REFERENCES accounts(id),
	observed_source_item_key  TEXT NOT NULL,
	message_id                INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
	evidence_kind             TEXT NOT NULL,
	UNIQUE (account_id, observed_source_item_key)
);
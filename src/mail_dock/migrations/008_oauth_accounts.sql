ALTER TABLE accounts
	ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'password'
	CHECK (auth_type IN ('password', 'xoauth2'));

ALTER TABLE accounts
	ADD COLUMN oauth_provider TEXT;

ALTER TABLE accounts
	ADD COLUMN oauth_client_id TEXT;

ALTER TABLE accounts
	ADD COLUMN oauth_tenant TEXT;

ALTER TABLE messages
	ADD COLUMN gmail_msgid TEXT;

ALTER TABLE messages
	ADD COLUMN gmail_thrid TEXT;

ALTER TABLE messages
	ADD COLUMN gmail_labels TEXT;

CREATE INDEX idx_msg_gmsgid
ON messages(account_id, gmail_msgid)
WHERE gmail_msgid IS NOT NULL;
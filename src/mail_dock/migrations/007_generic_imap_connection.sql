ALTER TABLE accounts
	ADD COLUMN tls_mode TEXT NOT NULL DEFAULT 'implicit';

ALTER TABLE accounts
	ADD COLUMN ca_cert_path TEXT;
CREATE INDEX idx_audit_recent
ON audit_log(occurred_at DESC);

CREATE INDEX idx_msg_purge
ON messages(account_id, local_state, trashed_at);

CREATE INDEX idx_msg_path
ON messages(account_id, relative_path);
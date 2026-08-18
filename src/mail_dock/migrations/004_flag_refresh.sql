ALTER TABLE folders ADD COLUMN highest_modseq INTEGER;

CREATE INDEX idx_msg_flag_refresh
ON messages(folder_id, uidvalidity, internal_date);
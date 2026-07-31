-- Normalize databases created before the UTC ISO 8601 timestamp policy.
UPDATE accounts
SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', created_at), created_at)
WHERE created_at IS NOT NULL AND instr(created_at, 'T') = 0;

UPDATE messages
SET created_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', created_at), created_at)
WHERE created_at IS NOT NULL AND instr(created_at, 'T') = 0;

UPDATE sync_failures
SET first_failed_at = CASE
        WHEN instr(first_failed_at, 'T') = 0
        THEN COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', first_failed_at), first_failed_at)
        ELSE first_failed_at
    END,
    last_failed_at = CASE
        WHEN instr(last_failed_at, 'T') = 0
        THEN COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', last_failed_at), last_failed_at)
        ELSE last_failed_at
    END
WHERE (first_failed_at IS NOT NULL AND instr(first_failed_at, 'T') = 0)
   OR (last_failed_at IS NOT NULL AND instr(last_failed_at, 'T') = 0);

UPDATE audit_log
SET occurred_at = COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', occurred_at), occurred_at)
WHERE occurred_at IS NOT NULL AND instr(occurred_at, 'T') = 0;

CREATE TRIGGER normalize_accounts_created_at
AFTER INSERT ON accounts
WHEN NEW.created_at IS NOT NULL AND instr(NEW.created_at, 'T') = 0
BEGIN
    UPDATE accounts
    SET created_at = COALESCE(
        strftime('%Y-%m-%dT%H:%M:%SZ', NEW.created_at), NEW.created_at
    )
    WHERE id = NEW.id;
END;

CREATE TRIGGER normalize_messages_created_at
AFTER INSERT ON messages
WHEN NEW.created_at IS NOT NULL AND instr(NEW.created_at, 'T') = 0
BEGIN
    UPDATE messages
    SET created_at = COALESCE(
        strftime('%Y-%m-%dT%H:%M:%SZ', NEW.created_at), NEW.created_at
    )
    WHERE id = NEW.id;
END;

CREATE TRIGGER normalize_sync_failures_timestamps
AFTER INSERT ON sync_failures
WHEN (NEW.first_failed_at IS NOT NULL AND instr(NEW.first_failed_at, 'T') = 0)
  OR (NEW.last_failed_at IS NOT NULL AND instr(NEW.last_failed_at, 'T') = 0)
BEGIN
    UPDATE sync_failures
    SET first_failed_at = CASE
            WHEN instr(NEW.first_failed_at, 'T') = 0
            THEN COALESCE(
                strftime('%Y-%m-%dT%H:%M:%SZ', NEW.first_failed_at),
                NEW.first_failed_at
            )
            ELSE NEW.first_failed_at
        END,
        last_failed_at = CASE
            WHEN instr(NEW.last_failed_at, 'T') = 0
            THEN COALESCE(
                strftime('%Y-%m-%dT%H:%M:%SZ', NEW.last_failed_at),
                NEW.last_failed_at
            )
            ELSE NEW.last_failed_at
        END
    WHERE id = NEW.id;
END;

CREATE TRIGGER normalize_audit_log_occurred_at
AFTER INSERT ON audit_log
WHEN NEW.occurred_at IS NOT NULL AND instr(NEW.occurred_at, 'T') = 0
BEGIN
    UPDATE audit_log
    SET occurred_at = COALESCE(
        strftime('%Y-%m-%dT%H:%M:%SZ', NEW.occurred_at), NEW.occurred_at
    )
    WHERE id = NEW.id;
END;
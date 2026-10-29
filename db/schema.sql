-- STSDS audit schema (PostgreSQL 15+). Append-only and hash-chained.
CREATE TABLE IF NOT EXISTS audit_log (
    txn_id     CHAR(32)    NOT NULL CHECK (txn_id ~ '^[a-f0-9]{32}$'),
    seq        INTEGER     NOT NULL CHECK (seq >= 0),
    ts         TIMESTAMPTZ NOT NULL,
    event      TEXT        NOT NULL,
    data       JSONB       NOT NULL,
    prev_hash  CHAR(64)    NOT NULL,
    hash       CHAR(64)    NOT NULL,
    PRIMARY KEY (txn_id, seq)
);

CREATE INDEX IF NOT EXISTS audit_log_ts_idx ON audit_log (ts);

-- Immutability: the application role can INSERT and SELECT, never UPDATE or DELETE.
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

-- Least privilege: run as a dedicated role.
-- CREATE ROLE stsds_app LOGIN PASSWORD '<from secret manager>';
-- GRANT INSERT, SELECT ON audit_log TO stsds_app;

-- bmem v0.1 authoritative schema.
-- Written once on Store creation; all statements are idempotent so a partially
-- created database is repaired on the next startup.

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    harness         TEXT NOT NULL,
    native_event_id TEXT NOT NULL,
    operation_id    TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    body            TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    ingested_at     TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    project         TEXT NOT NULL DEFAULT '',
    sensitivity     TEXT NOT NULL DEFAULT 'normal',
    trust_tier      TEXT NOT NULL DEFAULT 'T4',
    evidence_hash   TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_native_identity
    ON events(harness, device_id, native_event_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_operation
    ON events(operation_id);
CREATE INDEX IF NOT EXISTS ix_events_occurred
    ON events(occurred_at);
CREATE INDEX IF NOT EXISTS ix_events_harness
    ON events(harness, ingested_at);

CREATE TABLE IF NOT EXISTS memories (
    memory_id       TEXT PRIMARY KEY,
    fact_key        TEXT,
    body            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    revision_no     INTEGER NOT NULL,
    conditions      TEXT NOT NULL DEFAULT '{}',
    sensitivity     TEXT NOT NULL DEFAULT 'normal',
    trust_tier      TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    evidence_count  INTEGER NOT NULL DEFAULT 0,
    occurred_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    supersedes      TEXT,
    harness         TEXT NOT NULL DEFAULT 'manual',
    project         TEXT NOT NULL DEFAULT '',
    model_id        TEXT NOT NULL DEFAULT 'policy:t1'
);

CREATE INDEX IF NOT EXISTS ix_memories_fact_key
    ON memories(fact_key, status);
CREATE INDEX IF NOT EXISTS ix_memories_status
    ON memories(status, updated_at);
CREATE INDEX IF NOT EXISTS ix_memories_kind
    ON memories(kind);
CREATE INDEX IF NOT EXISTS ix_memories_updated
    ON memories(updated_at);
CREATE INDEX IF NOT EXISTS ix_memories_sensitivity
    ON memories(sensitivity, status);

CREATE TABLE IF NOT EXISTS revisions (
    memory_id       TEXT NOT NULL,
    revision_no     INTEGER NOT NULL,
    body            TEXT NOT NULL,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    conditions      TEXT NOT NULL DEFAULT '{}',
    sensitivity     TEXT NOT NULL DEFAULT 'normal',
    trust_tier      TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_event_id TEXT,
    supersedes      INTEGER,
    reason          TEXT NOT NULL DEFAULT '',
    model_id        TEXT NOT NULL DEFAULT 'policy:t1',
    created_at      TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision_no),
    FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_revisions_event
    ON revisions(source_event_id);

CREATE TABLE IF NOT EXISTS sources (
    source_row_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       TEXT NOT NULL,
    event_id        TEXT,
    root_source_id  TEXT NOT NULL,
    derived_from    TEXT NOT NULL DEFAULT '[]',
    source_type     TEXT NOT NULL,
    trust_tier      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    invalidated_at  TEXT,
    UNIQUE(memory_id, root_source_id),
    FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE CASCADE,
    FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_sources_memory
    ON sources(memory_id);
CREATE INDEX IF NOT EXISTS ix_sources_root
    ON sources(root_source_id);
CREATE INDEX IF NOT EXISTS ix_sources_event
    ON sources(event_id);

CREATE TABLE IF NOT EXISTS arbitration (
    decision_id     TEXT PRIMARY KEY,
    memory_id       TEXT,
    decision        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    evidence_ids    TEXT NOT NULL DEFAULT '[]',
    trust_tier      TEXT NOT NULL DEFAULT 'T4',
    confidence      REAL NOT NULL DEFAULT 0.0,
    model_id        TEXT NOT NULL DEFAULT 'policy',
    occurred_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY(memory_id) REFERENCES memories(memory_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_arbitration_memory
    ON arbitration(memory_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_arbitration_occurred
    ON arbitration(occurred_at);

CREATE TABLE IF NOT EXISTS injections (
    injection_id    TEXT PRIMARY KEY,
    harness         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    turn_id         TEXT NOT NULL DEFAULT '',
    task            TEXT NOT NULL DEFAULT '',
    context_epoch   TEXT NOT NULL DEFAULT '',
    target          TEXT NOT NULL DEFAULT 'cloud',
    items           TEXT NOT NULL DEFAULT '[]',
    token_count     INTEGER NOT NULL DEFAULT 0,
    deduplicated    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_injections_context
    ON injections(harness, session_id, task, context_epoch, target, created_at);
CREATE INDEX IF NOT EXISTS ix_injections_created
    ON injections(created_at);

CREATE TABLE IF NOT EXISTS injection_items (
    injection_id    TEXT NOT NULL,
    memory_id       TEXT NOT NULL,
    revision_no     INTEGER NOT NULL,
    PRIMARY KEY(injection_id, memory_id, revision_no),
    FOREIGN KEY(injection_id) REFERENCES injections(injection_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_injection_items_memory
    ON injection_items(memory_id, revision_no);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    event_id        TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    available_at    TEXT NOT NULL,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    finished_at     TEXT,
    FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_jobs_status
    ON jobs(status, available_at, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_event
    ON jobs(event_id);
CREATE INDEX IF NOT EXISTS ix_jobs_recompute_memory
    ON jobs(json_extract(payload, '$.memory_id'))
    WHERE kind='distill' AND status IN ('pending','running','failed')
      AND json_extract(payload, '$.recompute')=1;

CREATE TABLE IF NOT EXISTS import_cursors (
    cursor_row_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    path            TEXT NOT NULL DEFAULT '',
    parser_version  TEXT NOT NULL DEFAULT '',
    discovered      INTEGER NOT NULL DEFAULT 0,
    imported        INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    last_cursor     TEXT NOT NULL DEFAULT '',
    error           TEXT,
    updated_at      TEXT NOT NULL,
    UNIQUE(source, path)
);

CREATE INDEX IF NOT EXISTS ix_import_cursors_source
    ON import_cursors(source, updated_at);

CREATE TABLE IF NOT EXISTS tombstones (
    memory_id       TEXT PRIMARY KEY,
    event_ids       TEXT NOT NULL DEFAULT '[]',
    deleted_at      TEXT NOT NULL,
    reason_type     TEXT NOT NULL DEFAULT 'user_forget',
    detail          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_tombstones_deleted
    ON tombstones(deleted_at);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    body,
    tokenize = "unicode61 tokenchars '._-/@:'"
);

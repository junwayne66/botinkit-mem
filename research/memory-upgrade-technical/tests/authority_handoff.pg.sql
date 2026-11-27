-- X01-10 second-backend protocol. Run in a new isolated PostgreSQL database.
CREATE TABLE handoff_control (
    singleton boolean PRIMARY KEY CHECK (singleton),
    writer text NOT NULL CHECK (writer IN ('none', 'postgres', 'sqlite')),
    checkpoint text NOT NULL,
    replica_through bigint NOT NULL DEFAULT 0
);
CREATE TABLE handoff_origins (
    record_kind text NOT NULL,
    record_key text NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (record_kind, record_key)
);
CREATE TABLE handoff_events (
    native_id text PRIMARY KEY,
    event_id text NOT NULL UNIQUE,
    root_id text NOT NULL,
    content jsonb NOT NULL,
    active boolean NOT NULL
);
CREATE TABLE handoff_tombstones (root_id text PRIMARY KEY);
CREATE TABLE handoff_revisions (
    revision_id text PRIMARY KEY,
    current_value text NOT NULL,
    sequence bigint NOT NULL UNIQUE
);
CREATE TABLE handoff_intents (
    intent text PRIMARY KEY,
    request jsonb NOT NULL,
    receipt jsonb NOT NULL
);
CREATE TABLE handoff_changes (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intent text NOT NULL UNIQUE REFERENCES handoff_intents,
    request jsonb NOT NULL
);

CREATE FUNCTION handoff_seed(snapshot jsonb) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO handoff_control (singleton, writer, checkpoint) VALUES (true, 'none', snapshot->>'checkpoint');
    INSERT INTO handoff_origins SELECT row.kind, row.key, row.body
        FROM jsonb_to_recordset(snapshot->'origins') AS row(kind text, key text, body jsonb);
    INSERT INTO handoff_events SELECT row.native_id, row.event_id, row.root_id, row.content, row.active
        FROM jsonb_to_recordset(snapshot->'events') AS row(native_id text, event_id text, root_id text, content jsonb, active boolean);
    INSERT INTO handoff_tombstones SELECT root_id FROM jsonb_array_elements_text(snapshot->'tombstones') AS q(root_id);
END $$;

CREATE FUNCTION handoff_apply(command jsonb) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    active_writer text;
    prior_request jsonb;
    prior_receipt jsonb;
    applied_receipt jsonb;
    target_root text;
    log_seq bigint;
BEGIN
    SELECT writer INTO STRICT active_writer FROM handoff_control FOR UPDATE;
    IF active_writer <> 'postgres' THEN
        RETURN jsonb_build_object('state', 'writer_fenced');
    END IF;
    SELECT request, handoff_intents.receipt INTO prior_request, prior_receipt
      FROM handoff_intents WHERE intent = command->>'intent';
    IF FOUND THEN
        IF prior_request <> command THEN
            RETURN jsonb_build_object('state', 'intent_conflict');
        END IF;
        RETURN prior_receipt;
    END IF;
    IF command->>'op' = 'forget' THEN
        SELECT root_id INTO STRICT target_root FROM handoff_events WHERE native_id = command->>'id';
    ELSIF command->>'op' <> 'correct' OR nullif(command->>'value', '') IS NULL THEN
        RETURN jsonb_build_object('state', 'invalid_change');
    END IF;
    INSERT INTO handoff_intents VALUES (command->>'intent', command, '{}'::jsonb);
    INSERT INTO handoff_changes (intent, request) VALUES (command->>'intent', command)
        RETURNING sequence INTO log_seq;
    IF command->>'op' = 'correct' THEN
        INSERT INTO handoff_revisions VALUES (command->>'id', command->>'value', log_seq);
    ELSE
        INSERT INTO handoff_tombstones VALUES (target_root) ON CONFLICT DO NOTHING;
        UPDATE handoff_events SET content = '{}'::jsonb, active = false WHERE root_id = target_root;
    END IF;
    applied_receipt := jsonb_build_object('state', 'applied', 'sequence', log_seq, 'intent', command->>'intent');
    UPDATE handoff_intents SET receipt = applied_receipt WHERE intent = command->>'intent';
    RETURN applied_receipt;
END $$;

CREATE FUNCTION handoff_state() RETURNS jsonb LANGUAGE sql AS $$
    SELECT jsonb_build_object(
        'checkpoint', checkpoint, 'writer', writer, 'replica_through', replica_through,
        'head', COALESCE((SELECT max(sequence) FROM handoff_changes), 0),
        'visible_events', COALESCE((SELECT jsonb_agg(native_id ORDER BY native_id) FROM handoff_events WHERE active), '[]'::jsonb),
        'tombstones', COALESCE((SELECT jsonb_agg(root_id ORDER BY root_id) FROM handoff_tombstones), '[]'::jsonb),
        'value', (SELECT current_value FROM handoff_revisions ORDER BY sequence DESC LIMIT 1),
        'origins', COALESCE((SELECT jsonb_agg(jsonb_build_object('kind', record_kind,'key',record_key,'body',body) ORDER BY record_kind,record_key) FROM handoff_origins), '[]'::jsonb))
    FROM handoff_control;
$$;

CREATE FUNCTION handoff_quiesce() RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
    UPDATE handoff_control SET writer = 'none' WHERE writer = 'postgres';
    IF NOT FOUND THEN RAISE EXCEPTION 'writer was not PostgreSQL'; END IF;
    RETURN handoff_state();
END $$;

CREATE FUNCTION handoff_cut_back() RETURNS jsonb LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM handoff_control FOR UPDATE;
    IF EXISTS (SELECT 1 FROM handoff_control WHERE writer = 'none'
            AND replica_through = COALESCE((SELECT max(sequence) FROM handoff_changes), 0)) THEN
        UPDATE handoff_control SET writer = 'sqlite';
        RETURN jsonb_build_object('state','activated', 'writer','sqlite');
    END IF;
    RETURN jsonb_build_object('state','replica_behind');
END $$;

CREATE FUNCTION handoff_acknowledge(snapshot jsonb) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE authority jsonb;
BEGIN
    PERFORM 1 FROM handoff_control WHERE writer = 'none' FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('state','writer_not_quiesced'); END IF;
    authority := handoff_state();
    IF snapshot->'checkpoint' IS DISTINCT FROM authority->'checkpoint'
        OR snapshot->'visible_events' IS DISTINCT FROM authority->'visible_events'
        OR snapshot->'tombstones' IS DISTINCT FROM authority->'tombstones'
        OR snapshot->'value' IS DISTINCT FROM authority->'value'
        OR snapshot->'origins' IS DISTINCT FROM authority->'origins'
        OR snapshot->'through' IS DISTINCT FROM authority->'head' THEN
        RETURN jsonb_build_object('state','readback_mismatch');
    END IF;
    UPDATE handoff_control SET replica_through = (snapshot->>'through')::bigint;
    RETURN jsonb_build_object('state','replica_verified', 'through', snapshot->'through');
END $$;

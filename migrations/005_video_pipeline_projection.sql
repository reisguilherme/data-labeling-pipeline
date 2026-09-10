BEGIN;

CREATE TABLE video_pipeline_projection_events (
    event_seq bigserial PRIMARY KEY,
    object_id text NOT NULL CHECK (object_id <> ''),
    video_id text NOT NULL CHECK (video_id <> ''),
    event_kind text NOT NULL CHECK (event_kind <> ''),
    source_identity jsonb NOT NULL,
    source_digest char(64) NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'superseded')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz,
    UNIQUE (object_id, video_id, event_kind, source_digest)
);

CREATE INDEX video_pipeline_projection_events_pending_idx
    ON video_pipeline_projection_events (event_seq)
    WHERE status = 'pending';

CREATE INDEX video_pipeline_projection_events_video_idx
    ON video_pipeline_projection_events (object_id, video_id, event_seq DESC);

CREATE TABLE video_pipeline_projection (
    object_id text NOT NULL CHECK (object_id <> ''),
    video_id text NOT NULL CHECK (video_id <> ''),
    event_seq bigint NOT NULL
        REFERENCES video_pipeline_projection_events(event_seq),
    source_identity jsonb NOT NULL,
    source_digest char(64) NOT NULL,
    snapshot jsonb NOT NULL,
    projected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (object_id, video_id)
);

CREATE INDEX video_pipeline_projection_event_idx
    ON video_pipeline_projection (event_seq);

COMMIT;

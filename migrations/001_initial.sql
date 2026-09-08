BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE job_state AS ENUM ('queued', 'leased', 'running', 'done', 'error', 'cancelled');
CREATE TYPE review_status AS ENUM ('ok', 'edited');
CREATE TYPE artifact_state AS ENUM ('pending', 'valid', 'invalid', 'empty');

CREATE TABLE projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE classes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id),
    external_id integer NOT NULL,
    name text NOT NULL,
    UNIQUE (project_id, external_id),
    UNIQUE (project_id, name)
);

CREATE TABLE videos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id),
    source_uri text NOT NULL,
    blob_key text,
    sha256 char(64),
    width integer CHECK (width > 0),
    height integer CHECK (height > 0),
    frame_count integer CHECK (frame_count >= 0),
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, source_uri)
);

CREATE TABLE intervals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id uuid NOT NULL REFERENCES videos(id),
    start_frame integer NOT NULL CHECK (start_frame >= 0),
    end_frame integer NOT NULL CHECK (end_frame >= start_frame),
    flags jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (video_id, start_frame, end_frame)
);

CREATE TABLE prompts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    interval_id uuid NOT NULL REFERENCES intervals(id),
    frame_idx integer NOT NULL CHECK (frame_idx >= 0),
    objects jsonb NOT NULL,
    digest char(64) NOT NULL,
    supersedes_id uuid REFERENCES prompts(id),
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (interval_id, digest)
);

CREATE TABLE models (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id text NOT NULL UNIQUE,
    source jsonb NOT NULL,
    checkpoint_key text NOT NULL,
    checkpoint_sha256 char(64) NOT NULL,
    sam3_commit char(40) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE annotation_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    interval_id uuid NOT NULL REFERENCES intervals(id),
    prompt_id uuid NOT NULL REFERENCES prompts(id),
    model_id uuid REFERENCES models(id),
    kind text NOT NULL CHECK (kind IN ('sam3', 'legacy_detection')),
    prompt_digest char(64) NOT NULL,
    checkpoint_sha256 char(64),
    sam3_commit char(40),
    parameters jsonb NOT NULL DEFAULT '{}',
    state text NOT NULL CHECK (state IN ('running', 'complete', 'partial', 'error')),
    expected_frames integer NOT NULL CHECK (expected_frames >= 0),
    completed_frames integer NOT NULL DEFAULT 0 CHECK (completed_frames >= 0),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error text
);

CREATE UNIQUE INDEX annotation_runs_identity_idx
    ON annotation_runs(
        interval_id,
        prompt_id,
        kind,
        prompt_digest,
        COALESCE(model_id, '00000000-0000-0000-0000-000000000000'::uuid),
        md5(parameters::text)
    );

CREATE TABLE artifacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid REFERENCES annotation_runs(id),
    kind text NOT NULL,
    object_key text,
    sha256 char(64),
    byte_size bigint CHECK (byte_size >= 0),
    width integer,
    height integer,
    state artifact_state NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'empty' AND object_key IS NULL) OR state <> 'empty')
);

CREATE TABLE frame_instances (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES annotation_runs(id),
    frame_idx integer NOT NULL CHECK (frame_idx >= 0),
    object_id integer NOT NULL,
    class_id uuid NOT NULL REFERENCES classes(id),
    mask_artifact_id uuid REFERENCES artifacts(id),
    is_empty boolean NOT NULL DEFAULT false,
    bbox real[],
    area_pixels bigint NOT NULL DEFAULT 0 CHECK (area_pixels >= 0),
    UNIQUE (run_id, frame_idx, object_id),
    CHECK ((is_empty AND mask_artifact_id IS NULL AND bbox IS NULL AND area_pixels = 0)
        OR (NOT is_empty AND (mask_artifact_id IS NOT NULL OR bbox IS NOT NULL)))
);

CREATE TABLE revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES annotation_runs(id),
    frame_idx integer NOT NULL CHECK (frame_idx >= 0),
    revision integer NOT NULL CHECK (revision > 0),
    status review_status NOT NULL,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, frame_idx, revision)
);

CREATE TABLE revision_instances (
    revision_id uuid NOT NULL REFERENCES revisions(id),
    object_id integer NOT NULL,
    class_id uuid NOT NULL REFERENCES classes(id),
    mask_artifact_id uuid REFERENCES artifacts(id),
    deleted boolean NOT NULL DEFAULT false,
    bbox real[],
    area_pixels bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (revision_id, object_id),
    CHECK ((deleted AND mask_artifact_id IS NULL) OR NOT deleted)
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid REFERENCES projects(id),
    kind text NOT NULL,
    worker_kind text NOT NULL CHECK (worker_kind IN ('cpu', 'gpu')),
    state job_state NOT NULL DEFAULT 'queued',
    priority integer NOT NULL DEFAULT 100,
    payload jsonb NOT NULL DEFAULT '{}',
    result jsonb,
    error text,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    idempotency_key text UNIQUE,
    worker_id text,
    lease_token uuid,
    lease_expires_at timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    progress jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);

CREATE INDEX jobs_claim_idx
    ON jobs (worker_kind, priority, created_at)
    WHERE state IN ('queued', 'leased', 'running');

CREATE TABLE exports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id),
    format text NOT NULL CHECK (format IN ('yolo', 'coco')),
    task text NOT NULL CHECK (task IN ('detection', 'segmentation')),
    object_key text,
    manifest jsonb NOT NULL DEFAULT '{}',
    sha256 char(64),
    state text NOT NULL CHECK (state IN ('running', 'complete', 'error')),
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE audit_events (
    id bigserial PRIMARY KEY,
    project_id uuid REFERENCES projects(id),
    actor text,
    event text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_immutable_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER annotation_runs_are_immutable
BEFORE UPDATE OR DELETE ON annotation_runs
FOR EACH ROW WHEN (OLD.state = 'complete') EXECUTE FUNCTION reject_immutable_update();

CREATE TRIGGER frame_instances_are_immutable
BEFORE UPDATE OR DELETE ON frame_instances
FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();

COMMIT;

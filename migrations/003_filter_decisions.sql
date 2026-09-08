BEGIN;

CREATE TABLE video_filter_decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id uuid NOT NULL REFERENCES projects(id),
    name text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('keep', 'trash', 'trash_duplicate')),
    source_file text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE INDEX video_filter_decisions_lookup_idx
    ON video_filter_decisions(project_id, decision, name);

COMMIT;

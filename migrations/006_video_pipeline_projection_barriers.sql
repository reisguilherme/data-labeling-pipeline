BEGIN;

-- Lifecycle invalidations allocate from the event sequence introduced in 005.
-- Keep this upgrade separate for databases that have already applied 005.
CREATE TABLE IF NOT EXISTS video_pipeline_projection_barriers (
    object_id text PRIMARY KEY CHECK (object_id <> ''),
    event_seq bigint NOT NULL DEFAULT 0 CHECK (event_seq >= 0)
);

COMMIT;

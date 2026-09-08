BEGIN;

DROP INDEX IF EXISTS annotation_runs_identity_idx;
CREATE UNIQUE INDEX annotation_runs_identity_idx
    ON annotation_runs(
        interval_id,
        prompt_id,
        kind,
        prompt_digest,
        COALESCE(model_id, '00000000-0000-0000-0000-000000000000'::uuid),
        md5(parameters::text)
    );

COMMIT;

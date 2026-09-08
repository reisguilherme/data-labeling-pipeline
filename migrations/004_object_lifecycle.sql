BEGIN;

-- Datasets exportados sobrevivem à remoção do objeto fonte. O manifesto já
-- congela classes/runs; o vínculo operacional pode, portanto, ficar nulo.
ALTER TABLE exports ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE exports DROP CONSTRAINT IF EXISTS exports_project_id_fkey;
ALTER TABLE exports
    ADD CONSTRAINT exports_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;

ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_project_id_fkey;
ALTER TABLE audit_events
    ADD CONSTRAINT audit_events_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;

CREATE OR REPLACE FUNCTION reject_immutable_update() RETURNS trigger AS $$
BEGIN
    IF current_setting('pipeline.allow_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

COMMIT;

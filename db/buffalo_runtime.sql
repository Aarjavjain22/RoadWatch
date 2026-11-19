-- Apply after schema_postgis.sql. Buffalo workspaces mirror into the base tables.
ALTER TABLE frames ADD COLUMN IF NOT EXISTS location_source text;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS clock_known boolean;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS offset_s double precision;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS osm_way_id bigint REFERENCES roads;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS observation_id text;
CREATE UNIQUE INDEX IF NOT EXISTS buffalo_detection_observation_id ON detections (observation_id);
ALTER TABLE hazards ADD COLUMN IF NOT EXISTS buffalo_details jsonb;
CREATE TABLE IF NOT EXISTS buffalo_hazard_members (
    observation_id text PRIMARY KEY REFERENCES detections(observation_id) ON DELETE CASCADE,
    hazard_id text NOT NULL REFERENCES hazards ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS buffalo_311_drafts (
    draft_id text PRIMARY KEY,
    hazard_id text NOT NULL REFERENCES hazards ON DELETE CASCADE,
    payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS buffalo_workspace_hazards (
    hazard_id text PRIMARY KEY REFERENCES hazards ON DELETE CASCADE,
    workspace_id text NOT NULL
);

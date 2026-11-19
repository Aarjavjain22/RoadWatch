-- RoadWatch Buffalo database, PostgreSQL + PostGIS version (for the shared machine).
-- Same tables as roadwatch_buffalo.db (SQLite), plus real geometry columns and the
-- tables for our own data collection (drives, frames, labels, ground-truth survey).
--
--   createdb roadwatch && psql roadwatch -f schema_postgis.sql

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------- data dictionary
CREATE TABLE sources (
  source_id          text PRIMARY KEY,
  name               text NOT NULL,
  provider           text,
  url                text,
  license            text,
  table_name         text,
  rows               integer,
  date_from          date,
  date_to            date,
  location_accuracy  text,
  refresh            text,
  role_in_project    text,
  known_issues       text,
  loaded_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- public sources (layer: raw + clean)
CREATE TABLE roads (
  osm_way_id     bigint PRIMARY KEY,
  name           text,
  highway_class  text,
  length_m       real,
  geom           geometry(LineString, 4326) NOT NULL,
  source_id      text NOT NULL REFERENCES sources,
  loaded_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE city_311_road_requests (
  case_number            text PRIMARY KEY,
  type                   text NOT NULL,
  created_at             timestamptz NOT NULL,
  closed_at              timestamptz,
  days_to_close          real,
  status                 text,
  status_description     text,
  outcome                text,          -- filled | responded, no detail | not needed / completed / referred | open
  reported_location      text,
  location_kind          text,          -- address | intersection | street or other
  street                 text,
  council_district       text,
  neighborhood           text,
  city_flagged_duplicate boolean,
  geom                   geometry(Point, 4326),
  coords_were_swapped    boolean,
  shares_exact_coords    boolean,
  osm_way_id             bigint REFERENCES roads,
  dist_to_street_m       real,
  source_id              text NOT NULL REFERENCES sources,
  loaded_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE city_311_potholes_hist (
  case_reference     text PRIMARY KEY,
  created_at         timestamptz,
  closed_at          timestamptz,
  days_to_close      real,
  status             text,
  address            text,
  council_district   text,
  geom               geometry(Point, 4326),   -- NULL when the city used the placeholder (43.0, -79.0)
  coords_placeholder boolean,
  osm_way_id         bigint REFERENCES roads,
  dist_to_street_m   real,
  source_id          text NOT NULL REFERENCES sources,
  loaded_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events_511ny (
  event_id          bigint PRIMARY KEY,
  event_type        text,
  category          text,      -- crash | debris | disabled vehicle | construction / roadwork | weather / surface | ...
  organization      text,
  facility          text,
  created_at        timestamptz,
  closed_at         timestamptz,
  duration_min      real,
  geom              geometry(Point, 4326),
  osm_way_id        bigint REFERENCES roads,
  dist_to_street_m  real,
  source_id         text NOT NULL REFERENCES sources,
  loaded_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE crashes_nys (
  crash_id         bigint PRIMARY KEY,
  year             smallint,
  crash_date       date,
  crash_time       time,
  day_of_week      text,
  severity         text,
  collision_type   text,
  event            text,
  road_surface     text,
  weather          text,
  lighting         text,
  road_descriptor  text,
  traffic_control  text,
  vehicles         smallint,
  source_id        text NOT NULL REFERENCES sources,
  loaded_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE weather_hourly (
  observed_at    timestamptz PRIMARY KEY,
  temp_f         real,
  dewpoint_f     real,
  precip_in      real,
  visibility_mi  real,
  wind_kt        real,
  weather_codes  text,
  condition      text,          -- dry | rain | snow / ice | freezing | fog / mist
  source_id      text NOT NULL REFERENCES sources,
  loaded_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bridges_erie (
  bin                     text PRIMARY KEY,
  location                text,
  feature_carried         text,
  feature_crossed         text,
  owner                   text,
  year_built_or_replaced  smallint,
  last_inspection         date,
  poor_status             text,
  source_id               text NOT NULL REFERENCES sources,
  loaded_at               timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- our own data collection (Weeks 4-7)
CREATE TABLE drives (
  drive_id        text PRIMARY KEY,              -- e.g. 2026-11-03_mainst_day_01
  driver          text,
  vehicle         text,
  device          text,                          -- phone or dashcam model
  route           text,
  started_at      timestamptz NOT NULL,
  ended_at        timestamptz,
  conditions      text,                          -- day | night | rain | snow
  video_path      text NOT NULL,                 -- file in shared storage (videos are not stored in the database)
  gps_log_path    text,
  sync_offset_s   real,                          -- video clock minus GPS clock, from the sync clap
  privacy_blurred boolean NOT NULL DEFAULT false,
  notes           text,
  loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE gps_points (
  drive_id      text NOT NULL REFERENCES drives,
  ts            timestamptz NOT NULL,
  geom          geometry(Point, 4326) NOT NULL,
  speed_kph     real,
  heading_deg   real,
  accuracy_m    real,                            -- as reported by the phone
  matched_geom  geometry(Point, 4326),           -- snapped to the road (map matching)
  osm_way_id    bigint REFERENCES roads,
  PRIMARY KEY (drive_id, ts)
);

CREATE TABLE frames (
  frame_id        text PRIMARY KEY,              -- drive_id + frame number
  drive_id        text NOT NULL REFERENCES drives,
  ts              timestamptz NOT NULL,
  image_path      text NOT NULL,
  geom            geometry(Point, 4326),         -- car position at this frame (interpolated from gps_points)
  heading_deg     real,
  speed_kph       real,
  weather_hour    timestamptz REFERENCES weather_hourly(observed_at),
  split           text CHECK (split IN ('train', 'val', 'test')),   -- assigned per drive, never per frame
  loaded_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE labels (
  label_id     bigserial PRIMARY KEY,
  frame_id     text NOT NULL REFERENCES frames,
  labeler      text NOT NULL,
  class        text NOT NULL,                    -- longitudinal | transverse | alligator | pothole | debris | animal | crash
  x1 real, y1 real, x2 real, y2 real,            -- pixel box
  labeled_at   timestamptz NOT NULL DEFAULT now(),
  tool         text                              -- CVAT | Label Studio
);

CREATE TABLE survey_potholes (                   -- the answer key: measured by hand
  survey_id      text PRIMARY KEY,
  surveyed_by    text,
  surveyed_at    timestamptz NOT NULL,
  geom           geometry(Point, 4326) NOT NULL, -- GPS averaged on the spot
  gps_accuracy_m real,
  class          text,
  length_cm      real,
  width_cm       real,
  depth_cm       real,
  photo_path     text,
  nearest_311    text REFERENCES city_311_road_requests(case_number),
  dist_to_311_m  real,
  notes          text
);

CREATE TABLE detections (                        -- model output (Rishi)
  detection_id   bigserial PRIMARY KEY,
  frame_id       text NOT NULL REFERENCES frames,
  model_version  text NOT NULL,
  class          text NOT NULL,
  confidence     real NOT NULL,
  x1 real, y1 real, x2 real, y2 real,
  est_geom       geometry(Point, 4326),          -- estimated hazard position (not the car)
  uncertainty_m  real,
  weather_tag    jsonb                           -- visibility, condition, trust_adjust (Aditya)
);

CREATE TABLE hazards (                           -- one pin per real-world hazard (Aarjav)
  hazard_id        text PRIMARY KEY,
  class            text NOT NULL,
  state            text NOT NULL,                -- candidate | confirmed | reported | resolved | dropped
  geom             geometry(Point, 4326) NOT NULL,
  uncertainty_m    real,
  n_observations   integer NOT NULL,
  n_vehicles       integer NOT NULL,
  max_confidence   real,
  severity         text,
  first_seen       timestamptz,
  last_seen        timestamptz,
  osm_way_id       bigint REFERENCES roads,
  linked_311_case  text REFERENCES city_311_road_requests(case_number),
  survey_id        text REFERENCES survey_potholes   -- set when checked against the answer key
);

CREATE TABLE data_quality_checks (
  check_id    bigserial PRIMARY KEY,
  table_name  text,
  check_name  text,
  value       real,
  unit        text,
  result      text,                              -- pass | warn | info | fixed
  note        text,
  checked_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- spatial + lookup indexes
CREATE INDEX ON roads USING gist (geom);
CREATE INDEX ON city_311_road_requests USING gist (geom);
CREATE INDEX ON city_311_road_requests (type, created_at);
CREATE INDEX ON city_311_potholes_hist USING gist (geom);
CREATE INDEX ON events_511ny USING gist (geom);
CREATE INDEX ON events_511ny (category);
CREATE INDEX ON gps_points USING gist (geom);
CREATE INDEX ON frames USING gist (geom);
CREATE INDEX ON detections USING gist (est_geom);
CREATE INDEX ON hazards USING gist (geom);
CREATE INDEX ON survey_potholes USING gist (geom);

-- Example: how far is each surveyed pothole from the nearest city 311 report?
-- SELECT s.survey_id, c.case_number, ST_Distance(s.geom::geography, c.geom::geography) AS meters
-- FROM survey_potholes s
-- CROSS JOIN LATERAL (SELECT case_number, geom FROM city_311_road_requests
--                     WHERE type = 'Pothole Issue' ORDER BY geom <-> s.geom LIMIT 1) c;

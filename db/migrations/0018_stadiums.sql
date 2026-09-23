-- P4 weather: static per-venue reference, loaded from the checked-in, hand-reviewed
-- reference/stadiums.csv (built by scripts/build_stadiums_csv.py from OpenStreetMap +
-- review decisions; see docs/phases/P4.md "Weather design decisions"). Data lives in the
-- CSV, not in this migration, because known_names grows as sponsors rename venues and an
-- applied migration can never be edited.
--
-- Keyed on nflverse's games.stadium_id (a physical place -- MetLife/SoFi are one row
-- each, not one per tenant team). No FK from games.stadium_id: games carries historical
-- ids (OAK00, LAX97, ...) that are deliberately not in this table, and a future id
-- missing here must surface as an auditor alert, not a failed games upsert.
--
-- Only structural facts live here. Per-game roof state (open/closed) and surface stay on
-- games -- retractable roofs change game to game and surfaces get replaced.
--
-- known_names: every games.stadium string confirmed to mean this place. The weather
-- collector's name guard fetches only when games.stadium = ANY(known_names); a mismatch
-- (e.g. 2026_05_PHI_JAX: JAX00 but "Tottenham Hotspur Stadium") is skipped and alerted,
-- never fetched with this row's coordinates.
--
-- field_bearing: compass degrees of the field's long axis, [0, 180) -- a field is
-- symmetric end to end. Null unless bearing_basis says an NFL field polygon was reviewed;
-- never substituted with the building's axis.

CREATE TABLE stadiums (
    stadium_id text PRIMARY KEY,
    name text NOT NULL,
    known_names text[] NOT NULL CHECK (cardinality(known_names) > 0),
    lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
    coord_basis text NOT NULL CHECK (coord_basis IN ('osm_field_centroid', 'osm_stadium_center')),
    coord_osm text NOT NULL,                 -- e.g. 'way/145797338' -- the citation
    roof_type text NOT NULL CHECK (roof_type IN ('fixed', 'retractable', 'open')),
    roof_source text NOT NULL,               -- URL cited for roof_type
    field_bearing real CHECK (field_bearing >= 0 AND field_bearing < 180),
    field_osm_way_id bigint,
    field_length_m real,
    bearing_basis text NOT NULL CHECK (bearing_basis IN (
        'osm_field_agrees',
        'osm_field_outline_disagrees',
        'null_no_field',
        'null_soccer_pitch_only',
        'null_fixed_roof'
    )),
    outline_axis_deg real,                   -- stadium-outline long axis, cross-check only
    source_note text NOT NULL DEFAULT '',
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- a bearing exists exactly when its basis says one was reviewed, and always with the
    -- OSM way it came from
    CONSTRAINT stadiums_bearing_matches_basis CHECK (
        (bearing_basis IN ('osm_field_agrees', 'osm_field_outline_disagrees'))
        = (field_bearing IS NOT NULL AND field_osm_way_id IS NOT NULL)
    )
);

ALTER TABLE stadiums ENABLE ROW LEVEL SECURITY;

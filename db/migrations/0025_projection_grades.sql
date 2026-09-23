-- P5 grader (pipeline/orchestration/grader.py): grades of the immutable projection_log
-- locks, in their own tables. projection_log is never UPDATEd (0023's trigger forbids it).
--
-- projection_grades: one row per in-scope game that has kicked off, whether it locked or
-- not, so the no-lock denominator stays visible. Derived state, not a claim: a row is
-- re-upserted (hash-diffed) if nflverse later corrects a score or line.
-- result_first_seen_at is kept from the first write that saw a final score, which is how
-- the result lag (result_first_seen_at - kickoff) is measured.
--
-- Line conventions: every *_spread column is home-negative market convention (the odds
-- tables' and projection_log's), nflverse's home-positive spread_line already negated.

CREATE TABLE projection_grades (
    game_id text PRIMARY KEY REFERENCES games (game_id),
    season int NOT NULL,
    week int NOT NULL,
    -- graded: locked and scored; awaiting_result: locked, no final score yet;
    -- no_lock: kicked off without a projection_log row
    grade_status text NOT NULL CHECK (grade_status IN ('graded', 'awaiting_result', 'no_lock')),
    projection_log_id bigint REFERENCES projection_log (id),
    kickoff timestamptz NOT NULL,                 -- current kickoff from games
    kickoff_at_lock timestamptz,
    kickoff_moved boolean NOT NULL,               -- kickoff != kickoff_at_lock
    outside_fit_scope boolean NOT NULL,           -- season_type POST (the fit is REG only)
    neutral boolean,
    div_game boolean,
    last_card_status smallint,                    -- matchup_cards.projection_status (no_lock rows)

    -- lock context (from the lock; for no_lock rows, stability from the frozen card if it
    -- was projected)
    model_version text,
    efficiency_fingerprint text,
    stability_min real,
    stability_bucket text CHECK (stability_bucket IN ('low', 'mid', 'high')),
    lock_lead_hours real,
    market_status_at_lock smallint,
    lock_line_lead_hours real,                    -- how old the lock line was at kickoff
    flag_spread_key_straddle boolean,
    flag_spread_within_book_range boolean,
    flag_total_within_book_range boolean,
    flag_single_book_market boolean,
    flag_market_lookahead_only boolean,

    -- projection and result
    projected_spread real,
    projected_total real,
    spread_sd real,
    total_sd real,
    home_score int,
    away_score int,
    margin_error real,                            -- projected margin - actual margin
    total_error real,                             -- projected total - actual total
    in_band_spread boolean,                       -- |margin_error| <= spread_sd
    in_band_total boolean,

    -- vs. the lock line (our consensus at lock)
    lock_spread real,
    lock_total real,
    edge_spread_lock real,
    edge_total_lock real,
    ats_lock text CHECK (ats_lock IN ('win', 'loss', 'push', 'no_pick')),
    ou_lock text CHECK (ou_lock IN ('win', 'loss', 'push', 'no_pick')),
    lock_line_parity boolean,                     -- recomputed lock-time line == stored

    -- our own last pre-kickoff capture ("own close")
    own_close_spread real,
    own_close_total real,
    own_close_as_of timestamptz,
    own_close_lead_hours real,
    own_close_after_lock boolean,
    clv_own_spread real,                          -- null unless own_close_after_lock
    clv_own_total real,

    -- nflverse's final line (mixed-source vs. the lock line)
    nflv_close_spread real,
    nflv_close_total real,
    edge_spread_nflv real,
    edge_total_nflv real,
    ats_nflv text CHECK (ats_nflv IN ('win', 'loss', 'push', 'no_pick')),
    ou_nflv text CHECK (ou_nflv IN ('win', 'loss', 'push', 'no_pick')),
    clv_nflv_spread real,
    clv_nflv_total real,

    games_updated_at timestamptz NOT NULL,        -- games.updated_at this grade was built on
    grader_version text NOT NULL,
    content_hash text NOT NULL,
    result_first_seen_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX projection_grades_season_week_idx ON projection_grades (season, week);

-- grade_summary: every (slice, metric) aggregate, rebuilt on each grader run. n, a 95%
-- CI, and a verdict ride on every row, so a small slice can't read as a finding.
CREATE TABLE grade_summary (
    grader_version text NOT NULL,
    slice text NOT NULL,
    metric text NOT NULL,
    n int NOT NULL,
    estimate double precision,
    ci_low double precision,
    ci_high double precision,
    null_value double precision,                  -- the no-skill value tested against
    min_n int,
    -- insufficient_n | descriptive | exploratory | ci_spans_null | supported | against
    verdict text NOT NULL,
    computed_at timestamptz NOT NULL,
    PRIMARY KEY (grader_version, slice, metric)
);

-- Default-deny for the anon key, like every other table (0007).
ALTER TABLE projection_grades ENABLE ROW LEVEL SECURITY;
ALTER TABLE grade_summary ENABLE ROW LEVEL SECURITY;

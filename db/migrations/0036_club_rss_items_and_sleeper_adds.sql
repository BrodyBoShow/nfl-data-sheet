-- P8 step 3: the two Intel collectors' tables (docs/phases/P8.md, "Step 3 plan", D7,
-- D8, D11, D15, D18). Each collector mirrors its source: club_rss.py makes a club's rows
-- exactly what its feed holds (D8, D15), and sleeper_adds.py keeps the latest fetch only.
-- Neither is a by-age delete; retention.py stays the only one (CLAUDE.md).
--
-- D11: no table, column or view name contains trending, buzz, breakout, hot, news,
-- signal or momentum. No image columns: only a headline and a link are shown (D4).
--
-- RLS on, no policies: nothing but the owner reads these until 0037's grants.

CREATE TABLE club_rss_items (
    team         text NOT NULL REFERENCES teams (team_abbr), -- from the feed URL (D5), never the item
    link         text NOT NULL,          -- the item's <link>; the dedupe key (done-when "deduped")
    title        text NOT NULL,          -- <title> as published, never edited (D4)
    published_at timestamptz,            -- <pubDate>; NULL if missing or unparseable, never filled
    feed_url     text NOT NULL,          -- the feed the row came from: its attribution comes from the row
    content_hash text NOT NULL,          -- title, published_at, feed_url
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (team, link)
);
ALTER TABLE club_rss_items ENABLE ROW LEVEL SECURITY;

CREATE TABLE sleeper_adds (
    sleeper_id   text PRIMARY KEY,       -- Sleeper's player_id as sent: numeric, or a team code
                                         -- (team defense; 'LAR' stays 'LAR', D19)
    resolution   text NOT NULL CHECK (resolution IN
                     ('player', 'team_defense', 'no_players_row', 'unresolved')),
    player_id    text,                   -- gsis via crosswalk.sleeper_id; set for 'player' and 'no_players_row'
    team         text REFERENCES teams (team_abbr), -- an ACTIVE team or NULL ("placed" = NOT NULL):
                                         -- 'player': players.latest_team at fetch, if active;
                                         -- 'team_defense': the code through team_aliases, if active
    add_count    integer NOT NULL CHECK (add_count >= 0), -- Sleeper's `count`
    fetched_at   timestamptz NOT NULL,   -- provenance only; the label reads sleeper_adds_fetch.fetched_at
    content_hash text NOT NULL,          -- every column above but sleeper_id
    updated_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sleeper_adds ENABLE ROW LEVEL SECURITY;

COMMENT ON COLUMN sleeper_adds.fetched_at IS
    'Which fetch this row came from. Provenance only: the page label''s "as of" reads '
    'sleeper_adds_fetch.fetched_at, which is written in the same transaction and always '
    'equal. Not granted to anon.';

-- D18: one row, the latest fetch, replaced in the same transaction as sleeper_adds. The
-- page label reads only this row, so it says "top N" only for the N that came back, and
-- names a gap only when placed_count < returned_count.
CREATE TABLE sleeper_adds_fetch (
    id             smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    fetched_at     timestamptz NOT NULL, -- the label's "as of" (D6). Authoritative, not sleeper_adds.fetched_at
    lookback_hours smallint NOT NULL,    -- as requested: the label's "last 24h"
    fetch_limit    smallint NOT NULL,    -- as requested; diagnostic, never granted (D18)
    returned_count smallint NOT NULL,    -- rows Sleeper sent: the label's "top N"
    placed_count   smallint NOT NULL,    -- rows with a team; returned - placed = "couldn't be matched"
    content_hash   text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (placed_count BETWEEN 0 AND returned_count)
);
ALTER TABLE sleeper_adds_fetch ENABLE ROW LEVEL SECURITY;

COMMENT ON COLUMN sleeper_adds_fetch.fetched_at IS
    'The authoritative "as of" for the Sleeper label (P8 D18). sleeper_adds.fetched_at '
    'repeats it per row for provenance.';

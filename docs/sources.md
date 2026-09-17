# Sources

Every source below is **unverified** until a live call has been made, a trimmed response
saved to `tests/fixtures/`, and this entry updated with real URL/params/shape/limits.
**Never guess a URL or field name.** If a source doesn't behave as documented here, stop
and tell the user — don't silently work around it.

Status legend: **UNVERIFIED** (not yet called live) → **VERIFIED** (fixture saved, shape
documented) → **BROKEN** (verified once, later found dead — note date and what changed).

---

## nflverse bulk (via nflreadpy)

- **Status:** VERIFIED. ID-spine functions (`load_schedules`, `load_teams`,
  `load_players`, `load_ff_playerids`) live-called 2026-09-16. Bulk-stats functions
  (`load_pbp`, `load_player_stats`, `load_team_stats`, `load_snap_counts`,
  `load_nextgen_stats`, `load_ftn_charting`, `load_depth_charts`, `load_rosters`,
  used by the P2 nflverse bulk collector) live-called 2026-09-17.
- **License:** CC-BY 4.0 (nflverse); FTN charting data CC-BY-SA 4.0. Attribution required
  in UI footer.
- **Reliability:** Open data, actively maintained.
- **Freshness:** Play-by-play updates nightly after game days. NFL stat corrections land
  Mon–Wed, so the **Thursday re-pull is authoritative** for the prior week.
- **IMPORTANT — freshness gate is on us, not nflreadpy.** nflreadpy has its own client
  cache (`nflreadpy/cache.py`), but it's a plain TTL cache keyed by URL, in-memory or on
  a local filesystem dir — it does **not** check nflverse's release metadata, and it
  gives us nothing cross-run anyway since every GitHub Actions run is a fresh process.
  The `timestamp.json` freshness gate described in `CLAUDE.md` has to be implemented in
  our own collector code:
  - Most nflverse-data datasets are published as **GitHub Releases** under
    `nflverse/nflverse-data`, one release per dataset, tagged by dataset name. Each such
    release carries a `timestamp.json` asset at
    `https://github.com/nflverse/nflverse-data/releases/download/<tag>/timestamp.json`,
    shaped `{"last_updated": "2026-09-16 09:00:57 EDT"}`. Fetch that (tiny) file first,
    compare `last_updated` to what's stored from the prior run (`source_freshness`), and
    skip the real download if unchanged.
  - **The release tag is not always the function's `stat_type`/name — verify it from
    nflreadpy's own download path, don't guess.** Confirmed live 2026-09-17 by reading
    each loader's source (`inspect.getsource`) for the exact `path = f"<tag>/..."` it
    downloads, then hitting that tag's `timestamp.json`:
    - `schedules`, `players`, `teams` — tag matches the function name. Confirmed live
      2026-09-16.
    - `load_player_stats` → tag **`stats_player`**, NOT `player_stats`. There is a
      same-named `player_stats` release in the repo, but it's a stale/legacy tag last
      updated 2025-05-07 — using it as the freshness gate would silently never detect new
      data. (This corrects a wrong claim from the original P1 verification pass, which
      tested the `player_stats` tag and found it returned 200 without checking it was
      actually the tag nflreadpy downloads from.)
    - `load_team_stats` → tag **`stats_team`** (the plain `team_stats` tag doesn't exist
      at all — 404).
    - `load_pbp` → tag `pbp`. `load_snap_counts` → tag `snap_counts`. `load_ftn_charting`
      → tag `ftn_charting`. `load_depth_charts` → tag `depth_charts`. `load_rosters` →
      tag `rosters`. `load_nextgen_stats` (all three `stat_type`s share one release) →
      tag `nextgen_stats`. `load_pfr_advstats` (all four `stat_type`s share one release)
      → tag `pfr_advstats`. All confirmed live 2026-09-17, all return 200 with a fresh
      `last_updated`.
  - **`load_ff_playerids` is the exception** — it pulls a raw CSV from
    `dynastyprocess/data` on the `master` branch (`load_ffverse.py`), not a GitHub
    Release, so there is **no `timestamp.json` for it**. Gate this one with an HTTP
    `HEAD` request's `Last-Modified`/`ETag` header instead, or just accept refetching it
    every ID-spine run — it's small (~a few hundred KB) and ID spine is only T2.
- **Functions to use:** `load_schedules`, `load_teams`, `load_players`, `load_ff_playerids`
  (ID spine); `load_pbp`, `load_player_stats`, `load_snap_counts`, `load_nextgen_stats`,
  `load_ftn_charting`, `load_depth_charts`, `load_pfr_advstats` (bulk collector, P2 —
  `load_team_stats` and `load_rosters` were verified but aren't staged; see the
  collector's docstring for why).
- **Staged tables span two phases' analysts, one collector.** The `player_week`,
  `team_week`, `ngs`, and `depth` staged tables (db/migrations/0006) feed the Phase 2
  Efficiency analyst; `snaps`, `ftn`, and `pfr_advstats` feed the Phase 7 Usage/role and
  Scheme analysts. They're all populated by the same `pipeline/collectors/nflverse_bulk.py`
  run rather than split across two collectors, since they share one source family and one
  freshness gate.
- **Verified shapes (ID spine, as of 2026-09-16):**
  - `load_schedules(seasons=[2025])` → 285 rows × 46 cols. Key columns: `game_id`,
    `season`, `game_type`, `week`, `gameday`, `away_team`/`home_team`,
    `away_score`/`home_score`, `result`, `spread_line`, `total_line`, `roof`, `surface`,
    `temp`, `wind`, `away_qb_id`/`home_qb_id`, `stadium_id`. Also carries a **game-level**
    ID crosswalk: `old_game_id`, `gsis`, `nfl_detail_id`, `pfr`, `pff`, `espn`, `ftn`.
    `seasons` accepts `int | list[int] | bool | None`; `True` (default) loads all seasons.
  - `load_teams()` → 36 rows × 16 cols (32 current teams + historical relocated
    franchises, e.g. OAK/LV). No season param — always the full table. Key: `team_abbr`,
    `team_name`, `team_conf`, `team_division`, plus colors/logos (not needed by the spine).
  - `load_players()` → 24,824 rows × 39 cols. Canonical key is `gsis_id`. Carries its own
    partial crosswalk: `esb_id`, `nfl_id`, `pfr_id`, `pff_id`, `otc_id`, `espn_id`,
    `smart_id`. No Sleeper ID — that only comes from `load_ff_playerids`. No season
    param — full historical player table every call.
  - `load_ff_playerids()` → 12,494 rows × 35 cols, from DynastyProcess. Has `gsis_id`,
    `sleeper_id`, `espn_id`, `pfr_id`, `yahoo_id`, `mfl_id`, and others — this is the
    join point that adds **Sleeper** to the crosswalk (`load_players` doesn't have it).
    No season param — full current table every call. Not every row has a `gsis_id`
    (undrafted/practice-squad-only players sometimes lack one) — filter nulls before
    joining to the spine.
  - Fixtures (trimmed): `tests/fixtures/nflreadpy_schedules_sample.parquet`,
    `nflreadpy_teams_sample.parquet`, `nflreadpy_players_sample.parquet`,
    `nflreadpy_ff_playerids_sample.parquet`, `nflverse_timestamp_sample.json`. Regenerate
    with `uv run python scripts/make_id_spine_fixtures.py`.
- **Verified shapes (P2 nflverse bulk, as of 2026-09-17, `seasons=[2025]`):**
  - `load_pbp()` → 48,771 rows × 372 cols/season — **confirms the "aggregates only,
    never raw pbp in Postgres" rule is load-bearing, not stylistic.** Has everything the
    Efficiency analyst needs to derive its own aggregates: `epa`, `success`, `posteam`/
    `defteam`, `down`, `play_type`/`play_type_nfl`, `yardline_100`, `goal_to_go` (red
    zone), `drive`/`fixed_drive`/`drive_play_count`/`fixed_drive_result`/`series_result`
    (three-and-out = drive with `drive_play_count == 3` ending in a punt), `wp`,
    `score_differential`, `game_seconds_remaining`/`half_seconds_remaining`, `qtr` (for
    deriving garbage time — there's **no precomputed `garbage_time_play` flag**, contrary
    to what the phase doc's mention of "garbage time filtered" might imply; the filter has
    to be defined from `wp`/`score_differential`/time remaining).
  - `load_player_stats()` → 19,422 rows × 150 cols/season. Already carries
    `target_share`, `air_yards_share`, `wopr`, `racr`, `pacr`, and per-play-type `epa`
    pre-aggregated to player-week — the Usage analyst mostly reads this directly rather
    than re-deriving shares from pbp. Keyed by `player_id` (gsis), `season`, `week`,
    `team`.
  - `load_team_stats()` → 570 rows × 138 cols/season. Team-week box-score aggregates
    (yardage/EPA/turnovers by pass/rush), but **no success rate, explosive-play rate,
    points/drive, three-and-out rate, or down splits** — those are pbp-derived, not in
    this table, despite it being the obvious-looking source.
  - `load_snap_counts()` → 26,612 rows × 16 cols/season. **Keyed by `pfr_player_id` and
    a `player` display name — no `gsis_id` at all.** Must join through
    `player_id_crosswalk.pfr_id` to reach the canonical `player_id`; rows that don't
    resolve (crosswalk gap) should be counted and left unmatched, not guessed.
  - `load_nextgen_stats(stat_type=...)` → one call per `stat_type` in
    `{"passing", "rushing", "receiving"}`, sharing one release (tag `nextgen_stats`).
    Passing 605 / rushing 648 / receiving 1,402 rows per season. All three carry
    `player_gsis_id` directly (no crosswalk join needed). Advanced-tracking fields:
    passing `completion_percentage_above_expectation`, `aggressiveness`,
    `avg_air_yards_differential`; rushing `rush_yards_over_expected(_per_att)`,
    `percent_attempts_gte_eight_defenders`; receiving `avg_separation`, `avg_yac_above_expectation`.
  - `load_ftn_charting()` → 47,316 rows × 29 cols/season, **play-level, not player- or
    team-level** — keys are `nflverse_game_id`/`nflverse_play_id`, meant to be joined onto
    `load_pbp()` rows, not aggregated standalone. Carries `n_offense_backfield`,
    `n_defense_box` (approximate personnel/box count), `is_motion`, `is_play_action`,
    `is_no_huddle`, `is_rpo`, `is_screen_pass`, `n_blitzers`, `n_pass_rushers` — this is
    the Scheme sector's (Phase 7) main input, staged here since it's the same collector.
  - `load_depth_charts()` → 554,215 rows × 12 cols/season. **Not one row per player per
    week** — `dt` is a scrape timestamp and there are 221 distinct values across the
    2025 season (near-daily snapshots per team), so this needs de-duplication (e.g. last
    `dt` on or before each game's kickoff, per team) before it's usable as a weekly
    signal input; storing it raw as staged would multiply row count ~30x for no benefit.
    Has `gsis_id` directly.
  - `load_rosters()` → 3,137 rows × 36 cols/season (~98/team). Has `gsis_id` plus a full
    provider crosswalk (`espn_id`, `sportradar_id`, `yahoo_id`, `rotowire_id`, `pff_id`,
    `pfr_id`, `sleeper_id`, `esb_id`) — broader than `load_players`'/`load_ff_playerids`'
    combined crosswalk, but out of scope to fold into the ID spine now; note for a future
    crosswalk-completeness pass.
  - `load_pfr_advstats(seasons=..., stat_type=...)` → one call per `stat_type` in
    `{"pass", "rush", "rec", "def"}`, tag `pfr_advstats` (confirmed live 2026-09-17,
    verified via `inspect.getsource` on the internal `_load_pfr_advstats_week` helper the
    same way the other bulk-tag corrections were verified). Pass 684 / rush 2,355 /
    rec 4,533 / def 7,926 rows/season. **Keyed by `pfr_player_id`, no `gsis_id`** — same
    crosswalk-join requirement as `load_snap_counts`. Columns differ per `stat_type`
    (pass: `passing_bad_throw_pct`, `passing_drop_pct`, `times_pressured_pct`,
    `times_blitzed`, `times_hurried`, `times_hit`; rush: `rushing_yards_before_contact_avg`,
    `rushing_yards_after_contact_avg`, `rushing_broken_tackles`; rec: `receiving_drop_pct`,
    `receiving_broken_tackles`, `receiving_int`, `receiving_rat`; def: `def_pressures`,
    `def_missed_tackle_pct`, `def_passer_rating_allowed`, plus ~20 more raw def columns
    not staged — see `pipeline/collectors/nflverse_bulk.py` for the exact trimmed set).
    Named in this phase's collector scope but not consumed by any Phase 2 analyst yet.
  - Fixtures (trimmed): `tests/fixtures/nflreadpy_pbp_sample.parquet` (one full game, all
    372 cols), `nflreadpy_player_stats_sample.parquet`, `nflreadpy_team_stats_sample.parquet`,
    `nflreadpy_snap_counts_sample.parquet`, `nflreadpy_ftn_charting_sample.parquet`,
    `nflreadpy_depth_charts_sample.parquet`, `nflreadpy_rosters_sample.parquet`,
    `nflreadpy_nextgen_{passing,rushing,receiving}_sample.parquet`,
    `nflreadpy_pfr_advstats_{pass,rush,rec,def}_sample.parquet`. Regenerate with
    `uv run python scripts/make_nflverse_bulk_fixtures.py`.
- **Verified live for the Efficiency analyst (P2, as of 2026):**
  - `load_pbp(seasons=[2023])`'s `fixed_drive_result` has exactly 10 distinct values:
    `Touchdown`, `Field goal`, `Punt`, `Turnover`, `Turnover on downs`, `Missed field
    goal`, `End of half`, `Safety`, `Opp touchdown`, and `null`. Only `Touchdown` (6) and
    `Field goal` (3) score points for the possessing offense; `Safety` and `Opp
    touchdown` award points to the *other* team, which this schema (one row per team's
    own offense-drive) has no clean way to attribute — documented as a limitation in
    `docs/signals.md` rather than fixed.
  - **Team abbreviations across a relocation year are inconsistent between sources.**
    Checked `posteam`/`team`/`team_abbr` for 2019 (Oakland→Las Vegas Raiders), 2016 (San
    Diego→LA Chargers), 2015 (St. Louis→LA Rams): `load_pbp`, `load_player_stats`, and
    `load_nextgen_stats` already show the **current** code (`LV`/`LAC`/`LA`) even for the
    old season. `load_snap_counts` and `load_pfr_advstats` (both PFR-sourced) still show
    the **old** code (`OAK`/`SD`/`STL`) for the same seasons. `nflverse_bulk.py`
    normalizes `snaps`/`pfr_advstats` via `_TEAM_ABBR_ALIASES` to compensate.
  - **`load_teams()` returns 36 rows, not 32** — every current franchise code plus the
    retired `OAK`/`SD`/`STL`/`LAR` aliases (it's a static "every code nflverse has ever
    used" reference, not a "currently active" list). P1's id_spine collector stores this
    wholesale into `teams`, so `SELECT * FROM teams` is **not** a safe way to enumerate
    "the current 32 teams" for any future code. The Efficiency analyst instead derives
    its team list from which codes actually appear in `team_week`.
  - `load_snap_counts()`'s `position` column uses `C`/`G`/`T` for O-line **only for teams
    PFR breaks the line out individually** — some teams' snaps are tagged with the
    generic `OL` instead, with zero `C`/`G`/`T` rows at all. Checked 2025 and 2026 live:
    ARI/CHI/JAX/LA report 100% of their O-line snaps as generic `OL` in both seasons;
    most other teams mix granular `C`/`G`/`T` rows with some generic `OL` rows (e.g.
    backups); full position set seen: `C`, `CB`, `DB`, `DE`, `DL`, `DT`, `FB`, `FS`, `G`,
    `HB`, `K`, `LB`, `LS`, `NT`, `OL`, `P`, `QB`, `RB`, `S`, `SS`, `T`, `TE`, `WR`. The
    Efficiency analyst's `_OL_SNAP_POSITIONS` must include `OL` alongside `C`/`G`/`T` or
    it silently empties the O-line group for the generic-only teams.
    `load_depth_charts()`'s `pos_abb` instead uses
    side-specific O-line slots — `C`, `LG`, `LT`, `RG`, `RT` — among a larger set
    including `FB`, `FS`, `H`, `KR`, `LCB`, `LDE`, `LDT`, `LILB`, `LS`, `MLB`, `NB`,
    `NT`, `P`, `PK`, `PR`, `QB`, `RB`, `RCB`, `RDE`, `RDT`, `RILB`, `SLB`, `SS`, `TE`,
    `WLB`, `WR`. **`load_depth_charts()`'s `pos_grp` is not an offense/defense flag** —
    observed values are formation labels (`3WR 1TE`, `Base 3-4 D`, `Base 4-3 D`, `Special
    Teams`), so O-line filtering must go through `pos_abb`, never `pos_grp`.
  - `nflreadpy.get_current_season()` is pure local date arithmetic (no network call) —
    safe for L2 code to call directly. `nflreadpy.get_current_week()`'s **default**
    (`use_date=False`) path calls `load_schedules()` internally — a network fetch, **not**
    safe from L2 code (`CLAUDE.md`: analysts "never call external sources"). Its
    `use_date=True` variant is pure local date math instead (documented by nflreadpy
    itself as a "rough approximation," not schedule-exact) — used by the Efficiency
    analyst's `_depth_fallback_allowed` for exactly that reason.
- **Known traps:**
  - No 2025+ injury data — the source that fed nflverse injuries died after 2024. Use the
    Availability collector instead.
  - Participation (true personnel groupings) is only released after the postseason, not
    in-season. Approximate personnel from snap shares + FTN charting and label it as
    approximate.
  - The player crosswalk is split across two sources (`load_players` has PFR/PFF/ESPN;
    `load_ff_playerids` adds Sleeper) — the ID spine collector must join both, not just one.

## Live game data (ESPN scoreboard / game summary)

- **Status:** UNVERIFIED
- **License:** Unofficial, no published terms — use conservatively, don't republish raw.
- **Reliability:** Can break without notice; not an official API.
- **Freshness:** T0 (live, every few minutes during game windows).
- **Known traps:** Endpoint shapes can change without warning — auditor must watch for
  drift.
- **Params/shape/limits:** TBD on first verification call.

## Odds (The Odds API + ESPN embedded lines)

- **Status:** UNVERIFIED
- **License:** The Odds API free tier terms apply.
- **Reliability:** Credit-limited.
- **Freshness:** T1.
- **Known traps:**
  - Free tier: 500 credits/month. Cost per call = markets × regions. h2h + spreads +
    totals in `us` region = 3 credits/call. Budget ≈ 120 credits/month across the season
    (see calendar in `docs/architecture.md`).
  - Historical endpoints cost 10× a normal call — never use for backfill. Use nflverse
    schedule closing lines for historical backtests instead.
  - ESPN embedded lines used as a free fill-in source, not a replacement.
- **Params/shape/limits:** TBD on first verification call.

## Weather (Open-Meteo)

- **Status:** UNVERIFIED
- **License:** Free tier is **non-commercial only**.
- **Reliability:** Open data, no API key required.
- **Freshness:** T1 (T0/hourly on Sundays for outdoor games).
- **Known traps:** Skip dome and closed-roof games entirely — no weather signal applies.
  Requires a static stadium coords/roof/surface table (source TBD, likely maintained by
  hand or derived from nflverse `load_teams`/stadium metadata).
- **Params/shape/limits:** TBD on first verification call.

## Availability (ESPN injuries, Sleeper players, NFL.com injury reports)

- **Status:** UNVERIFIED
- **License:** Unofficial (ESPN, NFL.com); Sleeper API terms apply to player dump.
- **Reliability:** Can break; page-scrape risk for NFL.com.
- **Freshness:** T1.
- **Known traps:** Sleeper's full player dump is large — fetch **at most once per day**.
  This is the sole source of injury/practice-status data in-season since nflverse's feed
  is dead (see nflverse traps above).
- **Params/shape/limits:** TBD on first verification call.

## Intel / live news (ESPN NFL news feed, team RSS, Sleeper trending)

- **Status:** UNVERIFIED
- **License:** Unofficial (ESPN); RSS terms per team site; Sleeper API terms.
- **Reliability:** Can break.
- **Freshness:** T1.
- **Known traps:** Live news only — no static or manually maintained research files feed
  this system. Tagging is rule-based (keyword + crosswalk name matching) in Phase 7; LLM
  parsing is an optional Phase 8 add-on, never required.
- **Params/shape/limits:** TBD on first verification call.

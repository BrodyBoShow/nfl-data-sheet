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
  - **`location`** (verified live 2026-09-23, P5): `'Home'` or `'Neutral'`, no other
    values. 42 `Neutral` games in 2019–2025: 34 international REG games, all 7 Super
    Bowls, and 1 WC (2024). A "home" team's London/Munich game is `Neutral`, e.g.
    `2019_09_HOU_JAX`. Stored as `games.location` (migration `0022`). This is the only
    sourced neutral-site flag; the P5 synthesizer uses it to drop home-field advantage.
    The fixture `nflreadpy_schedules_sample.parquet` already has it (7 Home, 1 Neutral).
  - **`spread_line` is home-positive** (verified 2026-09-23 against stored `games`):
    corr(`spread_line`, `result`) is +0.39 to +0.51 in every season 2018–2025, where
    `result = home_score − away_score`. So `spread_line = +3` means the **home** team is
    favored by 3. That is the **opposite sign** of `odds_snapshots.spread_home_point`/the
    Market sector's `spread_home_*` (−3 = home favored). Negate it before comparing.
  - **Retired codes stay raw in `games`:** 2018–19 Raiders games use `OAK` in
    `games.home_team`/`away_team` (verified 2026-09-23), while `team_week`/`signals` hold
    `LV` (the nflverse bulk collector normalizes, id_spine doesn't). Any join from `games`
    to team-keyed tables must go through `normalize_team_abbr`
    (`pipeline/core/team_aliases.py`).
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
    wholesale into `teams`. **Fixed in P2's wrap-up**: `teams.is_active` (migration
    `0010_teams_is_active.sql`, set explicitly on every `id_spine` run via
    `_RETIRED_TEAM_CODES`) — any future code enumerating "the current 32 teams" must
    filter `WHERE is_active`, per CLAUDE.md's Canonical keys section. The Efficiency
    analyst still derives its own team list from which codes actually appear in
    `team_week` rather than querying `teams` at all, unaffected by this.
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
    `load_ff_playerids()`'s Sleeper coverage specifically lags current-season
    rookies/UDFAs and some veteran backups (measured live, Phase 3 — see this doc's
    Availability section) — `id_spine.py` now also fills `sleeper_id` gaps directly from
    Sleeper's own self-reported `gsis_id` (fill-null-only) as a secondary source.

## Live game data (ESPN scoreboard / game summary)

- **Status:** UNVERIFIED
- **License:** Unofficial, no published terms — use conservatively, don't republish raw.
- **Reliability:** Can break without notice; not an official API.
- **Freshness:** T0 (live, every few minutes during game windows).
- **Known traps:** Endpoint shapes can change without warning — auditor must watch for
  drift.
- **Params/shape/limits:** TBD on first verification call.

## Odds (The Odds API + ESPN embedded lines)

- **Status:** VERIFIED (live-called 2026-09-18, `/v4/sports/americanfootball_nfl/odds/`
  only — ESPN embedded lines still unverified, see below).
- **License:** The Odds API free tier terms apply.
- **Reliability:** Credit-limited.
- **Freshness:** T1.
- **Params/shape/limits:**
  - `GET https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/` with query
    params `apiKey`, `regions=us`, `markets=h2h,spreads,totals`, `oddsFormat=american`.
  - Response: a JSON array of event objects (verified live: 29 events for one call), each
    `{id, sport_key, sport_title, commence_time, home_team, away_team, bookmakers}`.
    `commence_time` is ISO 8601 UTC. `id` is The Odds API's own opaque event id — **not**
    the nflverse `game_id` and not stable to compare against it directly.
  - `bookmakers[]`: `{key, title, last_update, markets}`. Verified live, 8 `us`-region
    books present: `betmgm`, `betonlineag`, `betrivers`, `betus`, `bovada`, `draftkings`,
    `fanduel`, `lowvig`.
  - `markets[]`: `{key, last_update, outcomes}`, `key` one of `h2h`/`spreads`/`totals`.
    **Not every bookmaker carries all 3 requested markets for every game** — verified
    live, one game's `betmgm` entry had only `h2h`+`totals`, no `spreads`. Code must treat
    any market as optionally absent per bookmaker, never assume all 3.
  - `outcomes[]`: `h2h` → `{name: <team name>, price}` (moneyline, American odds, no
    `point`). `spreads` → `{name: <team name>, price, point}`. `totals` → `{name:
    "Over"|"Under", price, point}`.
  - **Team names are the full nickname string** (`"Atlanta Falcons"`, `"Carolina
    Panthers"`), not an abbreviation — resolve via `teams.team_name` (exact match,
    `WHERE is_active`) before storing, never guess an abbreviation from the string.
  - **The endpoint returns all upcoming (not-yet-started) games, not scoped to "this
    week"** — the verified call's 29 events spanned commence times from
    2026-09-20T17:00Z through 2026-09-29T00:15Z, i.e. two weeks' worth. A collector must
    filter/match to the week it cares about itself (via `commence_time` + team names
    against `games`, or `pipeline/core/schedule.py`'s `resolve_season_week` the same way
    the Availability collector resolves season/week for a source with no week of its
    own) rather than trusting the response to already be week-scoped. A game that has
    already kicked off drops out of the response entirely.
- **Known traps:**
  - Free tier: 500 credits/month. Cost per call = markets × regions. h2h + spreads +
    totals in `us` region = 3 credits/call — **confirmed live**: response headers on the
    verification call read `x-requests-last: 3`, `x-requests-used: 3`,
    `x-requests-remaining: 497` (month started fresh at 500). Use `x-requests-last` from
    each response as that call's actual cost rather than assuming a constant 3, in case
    the account/plan or requested params ever change. Budget ≈ 120 credits/month across
    the season (see calendar in `docs/architecture.md`).
  - Historical endpoints cost 10× a normal call — never use for backfill. Use nflverse
    schedule closing lines for historical backtests instead.
  - ESPN embedded lines used as a free fill-in source, not a replacement — **not yet
    verified**, TBD on first call to that endpoint.

## Weather (Open-Meteo)

- **Status:** VERIFIED (live-called 2026-09-23 ~00:20Z, `/v1/forecast` only). Fixture:
  `tests/fixtures/open_meteo_forecast_sample.json` — the real kickoff-window response for
  `2026_03_ATL_GB` (Lambeau, kickoff 2026-09-25T00:15Z) taken at T-48h.
- **License:** Free tier is **non-commercial only** — no subscriptions, no ads, no
  integration into commercial products (open-meteo.com/en/terms). Data is **CC BY 4.0**:
  the web app must show an Open-Meteo attribution wherever weather appears.
- **Reliability:** Open data, no API key required.
- **Freshness:** T1, snapshot schedule relative to each game's kickoff (see P4).
- **Limits (open-meteo.com/en/terms, /en/pricing):** 600 calls/min, 5,000/hour,
  10,000/day, 300,000/month. A request counts as >1 call when it asks for **more than 10
  hourly variables** or **more than 2 weeks** of data (fractional: 15 vars = 1.5 calls).
  No rate-limit headers in the response (verified live — only `Date`/`Content-Type`), so
  usage can't be read back per call; stay at ≤10 variables so every request is 1.0 call.
- **Params/shape (verified live):**
  - `GET https://api.open-meteo.com/v1/forecast` with `latitude`, `longitude`,
    `hourly=<comma list>`, `wind_speed_unit=mph`, `temperature_unit=fahrenheit`,
    `precipitation_unit=inch`, `timezone=UTC`, and `start_hour`/`end_hour`
    (`yyyy-mm-ddThh:mm`, inclusive both ends) to fetch only the game window instead of
    whole days.
  - The 10 variables used (exactly 10 → 1.0 call): `temperature_2m`,
    `apparent_temperature`, `precipitation`, `precipitation_probability`, `rain`,
    `snowfall`, `weather_code`, `wind_speed_10m`, `wind_gusts_10m`, `wind_direction_10m`.
    Units come back in `hourly_units` (`°F`, `inch`, `%`, `wmo code`, `mp/h`, `°`) —
    assert them in `validate` rather than trusting the request params.
  - Response: `{latitude, longitude, generationtime_ms, utc_offset_seconds, timezone,
    timezone_abbreviation, elevation, hourly_units, hourly}`; `hourly` is parallel arrays
    keyed by variable name plus `time`. With `timezone=UTC`, `time` values are
    **naive ISO strings with no `Z`** (`"2026-09-25T00:00"`) — parse as UTC explicitly.
  - **Hourly semantics** (docs): `temperature_2m`, `weather_code`, wind speed/direction are
    instantaneous at the hour; `precipitation`, `rain`, `snowfall` are the **preceding-hour
    sum**; `wind_gusts_10m` is the **preceding-hour max**; `precipitation_probability` is
    for the preceding hour. So covering a game that starts in hour H needs hours H through
    H+4 (the H+4 row's sums cover the game's last hour) — the fixture is that 5-row window.
  - Multiple comma-separated `latitude`/`longitude` pairs are accepted; the response
    becomes a **JSON list** of the single-location object (verified live, 2 points). The
    collector uses one request per game anyway — each game needs its own `start_hour`/
    `end_hour`, and per-game requests keep one failure from sinking the batch.
- **Forecast horizon (verified live):**
  - Hourly data runs ~15.8 days from the current UTC midnight: at 2026-09-23T00:20Z, core
    variables were non-null through `2026-10-08T18:00` (379 of 384 hours from
    `forecast_days=16`); `wind_gusts_10m` ended 6h earlier (`T12:00`). Past the last model
    hour but inside the 16-day range, values come back **`null`**, not omitted. Past the
    16-day range, the request **fails with HTTP 400**:
    `{"reason":"Parameter 'start_hour' is out of allowed range from 2026-06-22 to
    2026-10-08","error":true}`.
  - The P4 schedule's earliest snapshot is T-48h, so this is never hit in normal
    operation — but the collector still skips a target (no row stored) if the response is
    a 400 or any hour of the game window is `null` in a wind/temperature field. It never
    carries a value forward or interpolates to fill a gap.
  - Default `models` (`best_match`) at a US venue matched `gfs_seamless` for every hour
    and `ncep_hrrr_conus` for 40 of its 43 hours: the high-res HRRR model is used near-term
    and GFS beyond it. **HRRR only reached ~42h ahead** (last non-null
    `2026-09-24T18:00`), so a T-48h snapshot comes from GFS and T-36h onward mostly from
    HRRR. **Some of the change between the T-48h and T-36h snapshots is a model switch, not
    a change in the weather** — T-48h rows are stored with `model_regime_break = true` and
    excluded from movement comparisons (P4).
    The response does not say which model produced a value, or when that model run was
    issued; neither can be recorded.
  - **HRRR domain, for the Environment analyst's `weather_forecast_domain`** (verified
    2026-09-23): Open-Meteo's GFS/HRRR docs page (`open-meteo.com/en/docs/gfs-api`) lists
    "HRRR Conus" (3 km, hourly) and doesn't document how `best_match` picks HRRR by
    location. So membership is tested against HRRR's own grid, from NOAA's
    `https://rapidrefresh.noaa.gov/hrrr/HRRR_conus.domain.txt`: Lambert conformal, true
    latitude 38.5N (both), standard longitude 97.5W, centered at 38.5N 97.5W, 1799 × 1059
    mass points at 3000 m; corners SW 21.13812,-122.7195 / NW 47.84364,-134.0986 /
    NE 47.84364,-60.90137 / SE 21.13812,-72.28046. Forward-projecting those four corners
    (sphere R = 6370 km, WRF's) lands exactly on ±2697 km / ±1587 km, the grid's
    half-extents — that confirms the parameters. Every US venue in `stadiums` lies ≥ 300 km
    inside the grid (closest: SEA00, 318 km); every international venue is outside
    (closest: MEX00, 584 km beyond the southern edge). Inside the grid means HRRR is
    available there; it does **not** prove `best_match` used HRRR for a given hour.
  - **Venue time zones** (`stadiums.tz`, 2026-09-23): entered by city as IANA zones, then
    cross-checked by calling this endpoint with `timezone=auto` at each stadium's stored
    lat/lon — the response's `timezone` matched all 41 rows. (The collector itself always
    requests `timezone=UTC`; `auto` was used only for this check.)
- **Known traps:**
  - **Wind is an exterior 10 m open-terrain estimate, not field wind.** `wind_speed_10m`
    is the model's wind 10 m above ground for the grid cell, with no stadium in it. Inside a
    bowl the field-level wind is lower and swirls, and the relationship is not linear or
    documented anywhere free. Stored columns carry the height and unit
    (`wind_speed_10m_mph`, `wind_gusts_10m_mph`, `wind_direction_10m_deg`), and the
    Environment analyst treats them as a **relative indicator** ("windier than usual
    outside this stadium"), never as the wind the players will feel. No correction factor
    is applied — there's no sourced one.
  - **Grid snapping:** the response `latitude`/`longitude` is the model grid cell, not the
    requested point (requested 44.50133,-88.06222 → returned 44.50524,-88.049416, ~1 km
    off; `ecmwf_ifs025` snaps to 44.5,-88.0). Store the returned cell alongside the
    requested stadium coords. `elevation` is from a 90 m DEM and drives cell choice +
    statistical downscaling (default `cell_selection=land`).
  - Gusts can read **below** sustained speed in the same row (fixture 00:00 row:
    speed 3.2, gust 2.5 mph) because gust is a preceding-hour max and speed is an instant
    value. Store both as returned; don't "correct" either.
  - Skip fixed-roof venues entirely, and retractable venues whose game-row `roof` is
    `closed`. nflverse `games.roof` is null pre-game for every retractable venue in 2026 and
    wrongly says `dome` for open-air international grounds (MCG, Stade de France, Munich) —
    see the P4 roof rule, which lets the `stadiums` table's structural `roof_type` override
    it.

## Availability (ESPN injuries, Sleeper players)

- **Status:** VERIFIED (live-called 2026-09-17). NFL.com injury-report scraping is
  deliberately out of scope for now — ESPN + Sleeper only; see Known traps.
- **License:** Unofficial (ESPN, no published terms); Sleeper API terms apply to the
  player dump.
- **Reliability:** Can break without notice — neither is an official/documented API.
- **Freshness:** T1. ESPN is fetched every collector run (dispatcher's Wed/Fri
  cadence caps the real-world frequency); Sleeper is throttled to ≤1/day via
  `source_freshness`.
- **Params/shape/limits:**
  - **ESPN**: `GET https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries`,
    no auth, no query params needed. One call returns **all 32 teams'** current
    injuries in one response (800 entries on the day verified) — no pagination. Shape:
    `{timestamp, status, season, injuries: [{id, displayName, injuries: [{id,
    longComment, shortComment, status, date, source{...},
    type{id,name,description,abbreviation}, details{type,location,detail,side,
    returnDate,fantasyStatus}, athlete: {firstName,lastName,displayName,links[]
    (has the athlete id embedded, e.g. `.../id/3051775/...`), headshot (also carries
    the id, e.g. `.../3051775.png`), position{abbreviation}, team{abbreviation},
    notes{items:[...]}, status{name}}}]}]}`. No documented rate limit; CDN
    `Cache-Control: max-age=9`. Untrimmed per-injury payload averages ~11.7KB (mostly
    `athlete.team.logos`/`.links` bloat) — the collector stores a trimmed ~716B/row
    subtree instead (see `injuries.raw`).
  - **Sleeper**: `GET https://api.sleeper.app/v1/players/nfl`, no auth. Full dump,
    ~14.6MB, 12,228 players (verified count) keyed by Sleeper's own player id. No
    documented rate limit; CDN-cached (`s-maxage=600`) — the ≤1/day cap is a courtesy
    we enforce ourselves, not something the API demands. Fields used:
    `injury_status, injury_body_part, injury_notes, injury_start_date,
    practice_participation, practice_description, status, team, gsis_id, espn_id`.
- **Known traps:**
  - **No structured practice-participation data exists in either source.** ESPN gives a
    rolling designation (Questionable/Doubtful/Out/Injured Reserve/Active seen live) plus
    freeform notes text, no per-day (Wed/Thu/Fri) DNP/Limited/Full grid. Sleeper's
    `practice_participation` field is populated on ~0 of 12,228 current players
    (measured live: 1 non-null). `practice_trend_risk` (see `docs/signals.md`) is
    computed from the designation-change sequence instead — the honest substitute, not
    an estimate of real participation. A real Wed/Thu/Fri source (e.g. NFL.com's
    official report) would sharpen this; deliberately deferred rather than scraping HTML
    this phase.
  - **Sleeper's `injury_status` mixes real injuries with non-injury unavailability in
    the same free-text field.** Measured live 2026-09-18: values seen include `NA`,
    `Sus` (suspension), `COV` (COVID), `DNR` (did-not-report) — e.g. a player on the
    Commissioner Exempt list showed `NA`, not a real injury. Sleeper's own roster
    `status` field doesn't reliably separate these either — cross-tabbed live, `Sus`/
    `NA`/`COV`/`DNR` mostly show `status: Active`, same as real injuries. The analyst
    (`pipeline/analysts/availability_impact.py::_classify_designation`) buckets
    `designation` strings into `injury`/`non_injury_unavailable`/`healthy` instead —
    see `docs/signals.md`'s `availability_category` entry — an unrecognized value
    defaults to `injury` (logged), never silently healthy.
  - **ESPN and Sleeper cover different populations, not the same one twice.** Measured
    live: only ~48% of Sleeper's rostered+injured players (402 in scope on the day
    verified) appear in ESPN's feed at all by name+team. ESPN reads like a current-week
    practice-report proxy; Sleeper is the only source for most IR/PUP/Reserve players.
    Both sources are required.
  - **ESPN uses `WSH` for Washington; nflverse and Sleeper both use `WAS`.** Verified
    live by diffing ESPN's 32 team abbreviations against
    `nflreadpy_teams_sample.parquet` — Sleeper's own team values already match nflverse.
    Normalized via the shared `pipeline/core/team_aliases.py` (not a second copy).
  - **Neither source carries its own "week."** Both are rolling snapshots — the
    collector derives `(season, week, season_type)` from the `games` table
    (`pipeline/core/schedule.py::resolve_season_week`) using ESPN's own `timestamp`
    field (for ESPN rows) or the fetch time (for Sleeper rows), never a caller-supplied
    week.
  - **`player_id_crosswalk.sleeper_id` coverage lags current-season players.** Measured
    live against the day's Sleeper injury population: 71.6% resolve via
    `crosswalk.sleeper_id` directly; ~78.3% with two additional read-only fallback joins
    (Sleeper's own self-reported `gsis_id` against `players.player_id`, and
    self-reported `espn_id` against `crosswalk.espn_id` — both exact-ID joins). ESPN, by
    contrast, resolves 99.9% via `crosswalk.espn_id` alone. The gap skews toward
    `years_exp` 0–2 rookies/UDFAs plus veterans `load_ff_playerids()` hasn't caught up
    on. A name+team fallback was tested and rejected — it produced an actual collision
    between two different real players both named "Blake Miller" on the same team, so
    it's never used. `id_spine.py` now self-heals this incrementally (fill-null-only,
    from Sleeper's own self-reported `gsis_id` — see its module docstring). Unresolved
    rows from either source are stored with `player_id` null, never dropped, and resolve
    retroactively as the crosswalk improves; a residual handful per snapshot has no
    crosswalk path at all — mostly practice-squad players plus Sleeper's own "Duplicate
    Player" placeholder rows, a known Sleeper data artifact.
- **Fixtures (trimmed):** `tests/fixtures/espn_injuries_sample.json`,
  `tests/fixtures/sleeper_players_sample.json`.

## Intel / live news (ESPN NFL news feed, team RSS, Sleeper trending)

- **Status:** UNVERIFIED
- **License:** Unofficial (ESPN); RSS terms per team site; Sleeper API terms.
- **Reliability:** Can break.
- **Freshness:** T1.
- **Known traps:** Live news only — no static or manually maintained research files feed
  this system. Tagging is rule-based (keyword + crosswalk name matching) in Phase 7; LLM
  parsing is an optional Phase 8 add-on, never required.
- **Params/shape/limits:** TBD on first verification call.

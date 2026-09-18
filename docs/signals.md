# Signals registry

Every row in `signals` must have a matching entry here defining its formula, filters, and
source columns. Add an entry in the same PR/commit that first writes the signal.

## Schema

| Column | Type | Notes |
|---|---|---|
| id | bigserial PK | |
| game_id | text null FK | null for season-level signals |
| season, week | int | |
| team | text null | |
| player_id | text null | null for team signals |
| sector | text | efficiency, usage, scheme, availability, environment, market |
| signal | text | snake_case, e.g. `epa_per_dropback_adj` |
| value | double precision null | |
| league_pct | real null | 0–100 |
| sample_n | int null | plays or games behind the value |
| stability | real null | 0–1, trust after prior blending |
| as_of | timestamptz | |
| inputs_version | text | source timestamps used, e.g. `pbp@2026-09-17T07:10Z` |

Unique on (`season`, `week`, `game_id`, `team`, `player_id`, `sector`, `signal`) with
nulls handled deliberately (`NULLS NOT DISTINCT` or coalesced keys). Upserts replace the
latest value.

## Prior blending (efficiency sector)

Every efficiency signal is a **three-way blend** of the current season's opponent-
adjusted value, last season's opponent-adjusted value (the prior), and the league
average — not a two-way current/prior blend. This matters because a QB or O-line change
should lower trust in the *prior*, not artificially inflate trust in a still-thin current
sample: the weight a discount takes away from the prior goes to the league average, never
to the current season.

For a team/signal, given `n_cur` (current-season plays/drives/red-zone-trips behind the
value — the same number stored in `sample_n`) and that signal's `k_metric` (a shrinkage
constant in the same units, set per metric below):

- `w_cur = n_cur / (n_cur + k_metric)`
- `w_prior = (1 - w_cur) * prior_discount * r` — `r` is the metric/side's year-over-year
  reliability factor (see "Prior-blend reliability r" below); `prior_discount` is the
  QB/OL-continuity discount below for offense, or `1.0` for defense (no continuity data
  staged for defense — `r` is still applied to defense, just not `prior_discount`).
- `w_league = 1 - w_cur - w_prior`
- `value = w_cur * current + w_prior * prior + w_league * league_avg`
- **`stability = w_cur + w_prior`** — read confidence off `stability`, not `sample_n`;
  `sample_n` is a raw count for transparency/debugging, not a normalized trust score.

`prior_discount` for an **offense** signal comes from two factors, each scaled by how
much that particular signal plausibly depends on it (`qb_sensitivity`/`ol_sensitivity`,
0 = no effect, 1 = full effect — e.g. a QB change shouldn't discount a pure rush split
the way it discounts a pass split):
```
prior_discount = (1 - qb_sensitivity * (1 - qb_factor)) * (1 - ol_sensitivity * (1 - ol_factor))
```
- `qb_factor`: a continuity-*share* discount, not a binary same/different-starter flag —
  a starter who missed half the prior season to injury (or was traded in) isn't penalized
  like a brand-new starter just because a backup led the team in attempts.
  ```
  share = min(1, current_starter_prior_attempts / team_prior_total_attempts)
  continuity = min(1, share / 0.5)
  qb_factor = 1 - (1 - QB_CHANGE_DISCOUNT) * (1 - continuity)
  ```
  `current_starter_prior_attempts` is this season's starter's (by most attempts in the
  most recent game so far) own prior-season pass attempts, summed across **any** team
  they played for (a traded veteran gets full credit for attempts thrown elsewhere);
  `team_prior_total_attempts` is the *current* team's total prior-season pass attempts
  across every QB who played for it. Reaching half the team's prior passing workload
  already earns full continuity (`qb_factor = 1.0`); a share of 0 (brand-new starter, no
  prior attempts anywhere) reduces to the old floor, `qb_factor = QB_CHANGE_DISCOUNT =
  0.6`. `qb_factor = 1.0` (no discount) if the current starter or the team's prior-season
  attempts total is unknown — never guessed, and logged when it happens.
- `ol_factor`: `OL_MIN_FACTOR + (1 - OL_MIN_FACTOR) * continuity_ratio` where
  `continuity_ratio` is the overlap (out of 5) between this season's and last season's
  top-5-by-snaps O-line group; `OL_MIN_FACTOR = 0.7`. 1.0 (no discount) if either group
  is unknown, logged.
- **Defense signals always use `prior_discount = 1.0`** (before `r`) — there's no
  reliable front-seven/secondary continuity data staged yet, so a defense's blend shifts
  weight via `n_cur` growing over the season and via `r` (below), not via a personnel
  discount. **Documented gap**: personnel continuity is a candidate future refinement
  once such data exists, not solved here.
- **Known limitation:** this only catches a *year-over-year* starter change. A QB
  benched/replaced mid-*current*-season isn't specially handled — the current-season
  rating still pools all of that season's plays under one number (old and new starter
  alike) until enough of the new starter's own games accumulate.
- Coordinator changes are out of scope unless a verified live source exists.
- **Debugging:** every `efficiency` run records each team's `qb_factor`, `ol_factor`,
  current starter, prior/team attempts totals, OL overlap count, and both seasons' O-line
  groups to `agent_runs.meta` (`{"teams": {"<team>": {...}}}`) — read that instead of
  re-deriving these factors from `team_week`/`player_week`/`snaps` by hand.
- `QB_CHANGE_DISCOUNT`/`OL_MIN_FACTOR`/every `k_metric` below are pinned judgment-call
  constants, not tuned to any data — flagged as tunable once Phase 5's grader can measure
  whether they help (`docs/architecture.md`'s `GRADE ==> A_EFF` feedback arrow).

### Prior-blend reliability `r`

Unlike `QB_CHANGE_DISCOUNT`/`OL_MIN_FACTOR`/`k_metric` above, `r` **is** estimated from
data, not pinned by judgment call — it answers a different question than the QB/OL
discount: not "did personnel change," but "how much does *this metric* actually carry
over from one season to the next at all," per side. A metric with a weak year-over-year
signal shouldn't lean on the prior much even with the same starter and the same O-line.

**Method** (`scripts/estimate_reliability.py` — re-run and compare against the values
below once each season completes, since a full season's new pairs shifts the pooled
estimate):
1. For each season **2018–2025** independently and each metric, solve full-season
   opponent-adjusted offense/defense ratings via `_solve_ratings()` — the same "plain
   solve" path production uses for its own prior-season solve (`k_metric` shrinkage
   included, since a `_solve_ratings()` output is exactly the kind of value that becomes
   `prior` in the blend above).
2. For each metric/side, correlate season-N ratings against season-N+1 ratings across
   teams, for each of the 7 adjacent pairs (`2018→2019` … `2024→2025`) and pooled across
   all 7.
3. Clip the pooled r to `[0, 1]` (a metric shouldn't get *negative* trust in the prior).
4. **Shrink each metric's clipped r toward its own side's mean** (offense metrics toward
   the mean of all offense r's, defense toward the mean of all defense r's):
   `r_final = 0.5 * r_raw + 0.5 * r_side_mean`. 7 pairs of 32 teams is a small sample, so
   a single metric's pooled r is itself a noisy estimate; shrinking toward the side mean
   damps that noise the same way `k_metric` damps a thin current-season sample.
5. **Exception:** `epa_per_play_down4`/`success_rate_down4` are excluded from both the
   mean and the shrinkage, and pinned at exactly `r = 0` (not shrunk toward the side
   mean like everything else). Their raw pooled r was *negative*
   (`-0.168`/`-0.211` off, `-0.211`/`-0.321` def) before clipping — 4th-down attempts are
   too rare a denominator (a handful of plays a game) for any year-over-year signal to
   survive at all. This is a real finding, not noise: a team's down4 rating this season
   tells you nothing reliable about next season, so the blend should lean on
   current-season + league only for these two, never the prior.
- Two results came in below the review thresholds this run and were accepted as-is,
  not treated as errors: `red_zone_td_rate`'s raw offense r (0.198) sits just under the
  0.2 line, and `explosive_rate_rush`'s raw defense r (0.3249) exceeds its own raw
  offense r (0.3181) by 0.007 — both read as ordinary estimation noise from 7 pairs of
  32 teams, not a sign either metric is backwards.
- Both the shrunk `reliability_off`/`reliability_def` (what actually feeds the blend)
  and the raw `reliability_off_raw`/`reliability_def_raw` (pre-shrinkage, for comparing
  a future re-estimation against this one) live on each `MetricConfig` entry in
  `pipeline/analysts/efficiency.py`, alongside `_RELIABILITY_ESTIMATED_FROM_SEASONS`
  (`"2018-2025"`) and `_RELIABILITY_ESTIMATED_ON` (`"2026-09-17"`).
- `r` is a fixed constant per metric/side, not a per-team or per-week value — it doesn't
  vary with `n_cur` the way `w_cur`/`w_prior` do.

Opponent adjustment itself (before this blend ever runs) solves offense and defense
ratings jointly by iteration for each of current season and prior season separately —
see `pipeline/analysts/efficiency.py`'s module docstring for the exact method
(fixed-point iteration with re-centering to the league average; the current-season solve
additionally references each opponent's own blended rating rather than its raw
current-season rating, so a thin-sample opponent doesn't inject noise).

## Registry template

Copy this block per signal when it's implemented.

```markdown
### `<signal_name>`
- **Sector:** efficiency | usage | scheme | availability | environment | market
- **Scope:** team | player | game (which of team/player_id/game_id are non-null)
- **Formula:** <exact calculation>
- **Filters:** <e.g. garbage time excluded, min plays, situation splits>
- **Source columns:** <nflreadpy function + columns, or collector table + columns>
- **Sample size (`sample_n`):** <what it counts>
- **Stability:** <how stability is derived for this signal, if not the sector default>
- **Added:** <phase, date>
```

## Registry

### Efficiency sector (Phase 2)

**Scope:** team-level, season-to-date "entering this week" (`game_id` null, `player_id`
null, `team` set). Every signal below exists twice, `<name>_off` and `<name>_def` (the
team's own offense, and its defense — see the "Prior blending" note on why defense skips
the QB/OL discount), for **40 signals/team/week** total. All are opponent-adjusted per
the "Prior blending" section above; no-play/garbage-time filtering and the explosive-play
thresholds are applied upstream at collection
(`pipeline/collectors/nflverse_bulk.py`), not re-filtered here. `sample_n` = the
current-season denominator only (prior-season sample size isn't folded in). Source table
for every one of these: `team_week` (`pipeline/collectors/nflverse_bulk.py`), self-joined
on `opponent_team` for the opponent-adjustment solve. **Added:** Phase 2, 2026.

**Play filtering (`_aggregate_team_week`):**
- A penalty no-play still carries the original called play's `pass`/`rush` flag and a
  non-null `epa` — verified live, 2025–2026: 1,622 `no_play`/`qb_kneel`/`qb_spike` rows
  leaked into "plays" this way before this filter existed. Excluded via `play_type !=
  "no_play"` plus a `qb_kneel`/`qb_spike` flag check as a safety net (verified live that
  those flags are never actually set on a `pass==1`/`rush==1` row in this data — the net
  only guards against `no_play`, but is kept in case that ever changes).
- **Garbage time is time-aware**, not a single WP threshold applied to the whole game —
  a flat threshold triggers as early as the 2nd quarter of a blowout (verified live: one
  team's 51 eligible plays got cut to 13 this way). `_garbage_time_expr()`:
  - **Q1–Q2:** never garbage time — win probability swings fastest and least
    meaningfully this early; excluding on it here would discard real plays from both
    teams' normal game plans.
  - **Q3:** excluded only if `wp < _GARBAGE_TIME_Q3_WP_LOW (0.02)` or `> _GARBAGE_TIME_Q3_WP_HIGH
    (0.98)` — a tighter band than Q4, since a comfortable-but-not-yet-decided Q3 lead can
    still reverse.
  - **Q4/OT:** `wp < _GARBAGE_TIME_WP_LOW (0.05)` or `> _GARBAGE_TIME_WP_HIGH (0.95)` — the
    original band; by Q4 a WP this lopsided realistically means the outcome is decided.
  - Verified live across all 2025–2026 games (602 team-games): this raised the minimum
    team-game play count from 8 to 14 and cut the number of team-games under 30 plays
    from 30 to 18, with negligible effect on the median/p90 (within a few plays, from
    the no-play exclusion above, not this rule).
  - All four thresholds are pinned judgment calls, not tuned to any data — like
    `QB_CHANGE_DISCOUNT`/`OL_MIN_FACTOR` above, flagged as tunable once Phase 5's grader
    can measure whether they help (`docs/architecture.md`'s `GRADE ==> A_EFF` feedback
    arrow).
  - This same `_garbage_time_expr()` also governs which drives count as competitive for
    `points_per_drive`/`three_and_out_rate`/`red_zone_td_rate` (see the drive-scope note
    below) — one definition, not two.

No down-split explosive-rate signals exist — `team_week` has no
`down{1-4}_explosive_count` column, so none is fabricated.

**`points_per_drive` scope note:** `points` counts only touchdowns (6) and field goals
(3) scored on a competitive (non-garbage-time) drive, from pbp's own `fixed_drive_result`
— it does **not** include extra points or 2-point conversions (separate plays, outside
`fixed_drive_result`'s scope), so it reads **lower** than publicly-cited points-per-drive
figures that include the point-after. This is a scope choice, not a bug. Safeties and a
drive ending in an "Opp touchdown" (a defensive/return score during this team's drive)
also aren't counted for either team — this per-team offense-drive schema has no clean
place to attribute points that didn't come from the possessing offense; rare enough
league-wide to document rather than re-architect around.

| Signal (`<name>_off`/`<name>_def`) | Numerator | Denominator | k_metric | qb_sens | ol_sens |
|---|---|---|---|---|---|
| `epa_per_play` | `epa_sum` | `plays` | 200 plays | 0.5 | 0.5 |
| `epa_per_play_pass` | `pass_epa_sum` | `pass_plays` | 120 plays | 1.0 | 0.5 |
| `epa_per_play_rush` | `rush_epa_sum` | `rush_plays` | 120 plays | 0.0 | 1.0 |
| `epa_per_play_down1` … `down4` | `down{n}_epa_sum` | `down{n}_plays` | 50 plays | 0.5 | 0.5 |
| `success_rate` | `success_count` | `plays` | 200 plays | 0.5 | 0.5 |
| `success_rate_pass` | `pass_success_count` | `pass_plays` | 120 plays | 1.0 | 0.5 |
| `success_rate_rush` | `rush_success_count` | `rush_plays` | 120 plays | 0.0 | 1.0 |
| `success_rate_down1` … `down4` | `down{n}_success_count` | `down{n}_plays` | 50 plays | 0.5 | 0.5 |
| `explosive_rate` | `explosive_count` | `plays` | 200 plays | 0.5 | 0.5 |
| `explosive_rate_pass` | `pass_explosive_count` | `pass_plays` | 120 plays | 1.0 | 0.5 |
| `explosive_rate_rush` | `rush_explosive_count` | `rush_plays` | 120 plays | 0.0 | 1.0 |
| `points_per_drive` | `points` | `drives` | 15 drives | 0.5 | 0.5 |
| `three_and_out_rate` | `three_and_out_drives` | `drives` | 15 drives | 0.5 | 0.5 |
| `red_zone_td_rate` | `red_zone_tds` | `red_zone_trips` | 8 trips | 0.5 | 0.5 |

(A compact table rather than 40 copies of the registry template above — the formula,
filters, and blend/discount mechanics are identical across every one of these signals
except for the numerator/denominator/`k_metric`/sensitivities shown; repeating the full
template 40 times would just restate the same prose forty times.)

### Availability sector (Phase 3)

No prior-blend or opponent-adjustment for this sector — P3.md scopes it to raw,
point-in-time values from the current week's `injuries`/`snaps`/`depth` tables, not the
efficiency sector's reliability-tuned blend. `stability`/`league_pct` are left null
(nothing computed for them this phase, not fabricated). Source:
`pipeline/analysts/availability_impact.py`. Only emitted for players/teams with a
currently-flagged (non-`Active`, non-null) `injuries` designation this week.

### `snap_share_at_risk`
- **Sector:** availability
- **Scope:** player (`team` null, `game_id` null)
- **Formula:** the flagged player's own mean `snaps.offense_pct` — current season, weeks
  strictly before this one; falls back to the prior season's mean if no current-season
  games exist yet (e.g. week 1). No blending beyond that.
- **Filters:** player has a currently-flagged designation (not `Active`/null) in
  `injuries` this week.
- **Source columns:** `injuries.designation`, `snaps.offense_pct`
- **Sample size (`sample_n`):** not set (null) — a single mean, not a count-based signal.
- **Added:** Phase 3, 2026-09-18.

### `snap_share_redistribution_gain`
- **Sector:** availability
- **Scope:** player (`team` null, `game_id` null)
- **Formula:** for a healthy teammate at the same team + `players.position`, `flagged
  player's snap_share_at_risk × (teammate's own snap share ÷ sum of healthy teammates'
  snap shares in that group)`. When multiple flagged teammates share a group, their
  gains to a given healthy player are summed into one row, not written twice.
- **Filters:** redistribution-eligible positions only (`WR`, `RB`, `TE`) — raw snap
  share is a redistribution-baseline proxy, not a real target/carry share (P3.md scopes
  this to raw snap shares until P7's Usage analyst exists).
- **Source columns:** `injuries.designation`/`.team`, `players.position`,
  `snaps.offense_pct`
- **Sample size (`sample_n`):** not set (null).
- **Added:** Phase 3, 2026-09-18.

### `replacement_depth_rank_delta`
- **Sector:** availability
- **Scope:** player (on the backup, `team` null, `game_id` null)
- **Formula:** `backup's depth.pos_rank − flagged player's depth.pos_rank`, matched on
  the same `(team, depth.pos_abb)` slot, the lowest-ranked healthy teammate ranked below
  the flagged player. **Depth-chart order only — this is explicitly NOT a
  performance-quality or drop-off estimate.** No per-player efficiency data exists yet
  to ground a real quality metric (that's P7 Usage/roles territory); a larger delta
  means the replacement is further down the depth chart, nothing more should be
  inferred from the magnitude.
- **Filters:** both the flagged player and the candidate backup must have a `depth` row
  at the same `(team, pos_abb)` slot; no candidate ranked below the flagged player →
  no signal emitted (not a guess).
- **Source columns:** `injuries.team`, `depth.pos_abb`/`.pos_rank`/`.player_id`
- **Sample size (`sample_n`):** not set (null).
- **Added:** Phase 3, 2026-09-18.

### `ol_cluster_count` / `secondary_cluster_count`
- **Sector:** availability
- **Scope:** team (`player_id` null, `game_id` null)
- **Formula:** count of currently-flagged players on the team whose `players.position`
  is in the OL set (`C`, `G`, `T`, `OL`) or the secondary set (`CB`, `S`, `DB`, `FS`,
  `SS`) respectively. A raw count, not a pre-baked boolean/threshold — where a count
  becomes a "cluster" worth flagging is left to the display/consumer layer.
- **Filters:** only teams with at least one flagged player in that position group are
  emitted (no zero-value rows).
- **Source columns:** `injuries.designation`/`.team`, `players.position`
- **Sample size (`sample_n`):** not set (null).
- **Added:** Phase 3, 2026-09-18.

### `practice_trend_risk`
- **Sector:** availability
- **Scope:** player (`team` null, `game_id` null)
- **Formula:** deterministic ordinal over the ordered sequence of this week's
  `injuries.designation` values for that player (whichever source — ESPN or Sleeper —
  has more snapshots this week, ESPN winning ties): **+1** per step that escalates
  (e.g. `Questionable` → `Doubtful`), **−1** per step that de-escalates, **0** for a flat
  step, a step touching an unrecognized designation string, or only one snapshot so far.
  **This is not an estimate of real practice participation** — neither ESPN nor Sleeper
  exposes a structured Wed/Thu/Fri participation grid (`docs/sources.md`'s Availability
  Known traps); it's the closest honest signal available from what they do provide.
- **Filters:** player has a currently-flagged designation this week.
- **Source columns:** `injuries.designation`, `injuries.as_of`, `injuries.source`
- **Sample size (`sample_n`):** number of snapshots seen this week for that player.
- **Added:** Phase 3, 2026-09-18.

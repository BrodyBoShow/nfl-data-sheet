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

## Player tables (P7): the one exception to the single shape

**Everything team- or game-level lives in `signals`, in the shape above. That includes
everything the model reads.** Player detail from the Usage and Player efficiency
analysts is the one exception. It goes into wide per-analyst tables instead, because one
row per player-week holding every metric is ~28× smaller than one `signals` row per
metric (~19 vs. ~335 MB/season; arithmetic in `docs/phases/P7.md`, "Storage design").
Player rows that other sectors already write to `signals` (Availability's per-player
signals) stay there.

| Table | Written by | Holds |
|---|---|---|
| `player_usage_week` | Usage and role (`usage.py`) | snap/target/air-yards/carry/RZ/GL/EZ shares and WoW deltas, for every player who takes a snap |
| `player_eff_week` | Player efficiency (`player_efficiency.py`) | receiving, rushing, passing, and defense rate stats |

Contract (the migrations in P7 step 3 implement it; nothing here is built yet):
- **Key:** (`player_id`, `season`, `week`). Also `team` and `game_id` (the game this
  row's per-game values come from), `as_of`, `inputs_version` (plain text, the same
  convention as `signals`), and `content_hash` for `filter_changed`.
- **As-of rows:** a row is written for week W only for players who played in week W. To
  read "as of week W", take each player's latest row with `week <= W` in that season.
  A player on bye or injured keeps their last row. No row is ever written for a player
  with no inputs (missing stays missing).
- **Columns per metric:** `<metric>_std` (season-to-date, prior-blended), `<metric>_game`
  (this game), `<metric>_l4` (last 4 games played), all `real`. Plus `<metric>_pct`, a
  `smallint` 0–100 league percentile of the `_std` value. A null means not sourced or no
  sample, never zero-filled.
- **Per family, not per metric:** a sample count and a `stability` (0–1, same meaning as
  in `signals`) for each family: usage, receiving (targets), rushing (carries), passing
  (dropbacks), defense (defensive snaps). The family's sample counts are themselves
  `_std`/`_game`/`_l4` columns.
- **League percentile population:** by default, players in the same position group with
  a row as of that week whose family sample meets the metric's minimum. Each registry
  entry states its own population and minimum.
- **Retention (L4):** after a season completes, only each player's latest-week row for
  that season is kept (`pipeline/orchestration/retention.py`). A past season reads as
  final STD values, with no weekly history.
- **Participation-derived columns** end in `_hist`: multi-season historical tendencies,
  2016–2025, post-season release only. `inputs_version` names the season span, and the
  UI shows the span next to the value. Never presented as current-season behavior.
- **Honesty:** `_game`/`_l4` values for rotational players rest on a handful of plays
  (`docs/phases/P7.md` sample-size table). Anything that displays them shows `stability`
  beside them.
- **Access:** anon can't read these tables until the web player-view migration lands
  (P7 step 9, blocked on the PFR/NGS license quotes).

Registry entries for player-table metrics use this template, grouped by table and family:

```markdown
### `<table>.<metric>` (`_std` / `_game` / `_l4` / `_pct`)
- **Family:** usage | receiving | rushing | passing | defense
- **Formula:** <exact calculation, per window>
- **Filters:** <garbage time, min sample for _pct, situation splits>
- **Source columns:** <staged table + columns>
- **Sample:** <which family count it rests on>
- **Prior blend:** <k_metric, prior source (last season / participation _hist), r>
- **League pct population:** <position group + minimum sample>
- **Added:** <phase, date>
```

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
`pipeline/analysts/availability_impact.py`. Only emitted for players/teams currently
"flagged" this week — see `availability_category` below for what that means and why it's
not just "non-`Active`."

**Designation classification** (`_classify_designation`): `injuries.designation` is a
free-text field that mixes real injury statuses with non-injury unavailability
shorthand in the same field — verified live 2026-09-18: Sleeper's `NA`/`Sus`/`COV`/`DNR`
values (exempt list, suspension, COVID, did-not-report) mostly show Sleeper's own roster
`status` as `Active`, so `status` can't separate them either; the designation string
itself is the only signal available. Every designation buckets into exactly one of:
- **`healthy`** — `Active` or no `injuries` row. Not flagged, no signals emitted.
- **`injury`** — `Questionable`, `Doubtful`, `Out`, `IR`, `Injured Reserve`, `PUP`, **or
  any unrecognized value** (defaults here, logged, so a new designation string is never
  silently treated as healthy).
- **`non_injury_unavailable`** — `NA`, `Sus`, `COV`, `DNR`.

Both `injury` and `non_injury_unavailable` count as "flagged" for
`snap_share_at_risk`/`snap_share_redistribution_gain`/`ol_cluster_count`/
`secondary_cluster_count` (a suspended starter's snaps are just as much at risk as an
injured one's) — only `practice_trend_risk` is restricted to `injury` (a suspension or
exempt-list stint escalating isn't a practice-participation trend).

### `availability_category`
- **Sector:** availability
- **Scope:** player (`team` null, `game_id` null)
- **Formula:** `_classify_designation`'s bucket, encoded as a small numeric code since
  `signals.value` is numeric-only: **`1.0` = `injury`**, **`2.0` =
  `non_injury_unavailable`**. Emitted for every flagged player, one row each — lets a
  consumer tell "exempt/suspended" apart from "hamstring" without reading
  `injuries.designation` text directly (L3 reads `signals` only, per CLAUDE.md's layer
  rules — it can't join back to the staged `injuries` table itself).
- **Filters:** player is currently flagged (either category) this week.
- **Source columns:** `injuries.designation`
- **Sample size (`sample_n`):** not set (null).
- **Added:** Phase 3, 2026-09-18.

### `snap_share_at_risk`
- **Sector:** availability
- **Scope:** player (`team` null, `game_id` null)
- **Formula:** the flagged player's own mean `snaps.offense_pct` — current season, weeks
  strictly before this one; falls back to the prior season's mean if no current-season
  games exist yet (e.g. week 1). No blending beyond that.
- **Filters:** player is currently flagged (either category — see Designation
  classification above) this week.
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
  this to raw snap shares until P7's Usage analyst exists). Either flagged category.
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
  no signal emitted (not a guess). Either flagged category.
- **Source columns:** `injuries.team`, `depth.pos_abb`/`.pos_rank`/`.player_id`
- **Sample size (`sample_n`):** not set (null).
- **Added:** Phase 3, 2026-09-18.

### `ol_cluster_count` / `secondary_cluster_count`
- **Sector:** availability
- **Scope:** team (`player_id` null, `game_id` null)
- **Formula:** count of currently-flagged players (either category) on the team whose
  `players.position` is in the OL set (`C`, `G`, `T`, `OL`) or the secondary set (`CB`,
  `S`, `DB`, `FS`, `SS`) respectively. A raw count, not a pre-baked boolean/threshold —
  where a count becomes a "cluster" worth flagging is left to the display/consumer layer.
- **Filters:** only teams with at least one flagged player in that position group are
  emitted (no zero-value rows).
- **Source columns:** `injuries.designation`/`.team`, `players.position`
- **Sample size (`sample_n`):** not set (null).
- **Added:** Phase 3, 2026-09-18.

### `practice_trend_risk`
- **Sector:** availability
- **Scope:** player (`team` null, `game_id` null)
- **Formula:** deterministic ordinal over the ordered sequence of this week's
  `injuries.designation` values for that player, **one observation per distinct
  calendar day** (same-day snapshots collapse to that day's last observation — verified
  live 2026-09-18: a same-day rerun produced two snapshots 27 seconds apart, which is
  not an independent trend data point). Uses whichever source — ESPN or Sleeper — has
  more distinct days this week, ESPN winning ties: **+1** per day-over-day step that
  escalates (e.g. `Questionable` → `Doubtful`), **−1** per step that de-escalates, **0**
  for a flat step or one touching an unrecognized designation string.
  **Requires at least 2 distinct days of data — a player with only one day's
  observation gets no row at all**, not a `0` (a single day's snapshot can't produce a
  trend, and a `0` meaning "no data yet" would be indistinguishable from one meaning
  "confirmed flat" if emitted anyway). **This is not an estimate of real practice
  participation** — neither ESPN nor Sleeper exposes a structured Wed/Thu/Fri
  participation grid (`docs/sources.md`'s Availability Known traps); it's the closest
  honest signal available from what they do provide.
- **Filters:** player is currently flagged **and in the `injury` category specifically**
  this week (see Designation classification above — a suspension/exempt-list stint
  isn't a practice-participation trend, so `non_injury_unavailable` players never get
  this signal).
- **Source columns:** `injuries.designation`, `injuries.as_of`, `injuries.source`
- **Sample size (`sample_n`):** number of distinct calendar days with data this week for
  that player (not raw snapshot count — same-day reruns don't inflate it).
- **Added:** Phase 3, 2026-09-18. **Revised:** 2026-09-18 (distinct-day dedup + `sample_n
  >= 2` gate, injury-only scope — see this phase's session notes for the live data that
  prompted it: 151/347 rows were single-snapshot 0s indistinguishable from "stable").

### Environment sector (Phase 4)

Source: `pipeline/analysts/environment.py`. Reads `games`, `stadiums`,
`weather_snapshot_targets`, `weather_snapshots`. No prior-blend and no opponent
adjustment; `league_pct` and `stability` are always null (lead time is exposed as its own
signal rather than folded into a made-up `stability` decay). **Added:** Phase 4,
2026-09-23.

**Scope: per game, not per dispatcher week.** Each run covers every game with
`now − 24h < kickoff ≤ now + 7d`, whatever `ctx.week` is, because weather belongs to a
game and a Thursday game's snapshots are captured before nflreadpy's current week
advances. Every row carries **its game's own `season`/`week`**, so a game has the same
unique key whichever dispatcher week computes it. The 24h lookback keeps a game in scope
long enough for `weather_status` to settle after its last target closes at kickoff + 1h.
After a game leaves the window, its last rows stay as they were.

Two scopes, both with `game_id` set and `player_id` null:
- **game** (`team` null): weather, `weather_status`, `venue_roof_code`, `surface_code`.
- **team** (`team` set, one row per side): rest, travel, timezone.

`inputs_version`: weather value rows carry `weather@<snapshot as_of>`, naming the exact
snapshot used. Every other row carries `schedules@<nflverse timestamp>,stadiums_csv@<hash>`.

#### Which snapshot (headline)
The latest captured snapshot taken before kickoff: `lead_hours >= 0`, largest `as_of`.
- A t2 capture taken after kickoff is never the headline. That also means headline values
  stop changing at kickoff.
- t48 counts if it's the only capture, flagged by `weather_model_regime_break = 1`.
- No movement or delta signals yet. If they're added, they start at t36 (P4.md).

#### `weather_status` (game scope, emitted for every game in the window)
Derived from structure first (name guard via `pipeline/core/venue.py`'s `resolve_venue`,
the same function the weather collector uses), then from capture state:

| Value | Meaning | Derived from |
|---|---|---|
| 1 | forecast available | a headline snapshot exists |
| 2 | indoor, weather doesn't apply | `stadiums.roof_type='fixed'`, or retractable with `games.roof='closed'` or a target skipped `roof_closed` |
| 3 | awaiting capture | kickoff ahead, nothing captured: a target is still pending, or the game is beyond the collector's 54h horizon (no target rows yet) |
| 4 | missed | nothing captured and either kickoff passed or every target closed (`missed` / `no_forecast_data`) |
| 5 | venue unresolved | name guard failed (`name_mismatch` / `unknown_stadium`, e.g. `2026_05_PHI_JAX`) |
| 6 | not tracked | no target rows and kickoff already past (games played before the weather collector existed) |

A dome is known from `stadiums` alone, so it never depends on target rows. A missed capture
is known only from targets, so the two can't be confused. Weather value rows exist **only**
for status 1.

**Caveat, a stale 3:** analysts run only on a dispatcher tick where some collector wrote
rows. A target flipping to `missed` writes 0 rows, so the 3 → 4 change waits for the next
tick where something else writes (typically the next weather capture of any game).
**A stored `weather_status = 3` whose game has already kicked off does not mean "still
awaiting capture."** It means the status was computed before the targets closed. Treat it
as unknown until it's recomputed. Once the game drops out of the 24h lookback it's never
recomputed, so such a row can stay 3 permanently.

#### Wind: one shape, speed-only first
`wind_speed_mph` and `wind_direction_mode` are always present with a forecast. The split
rows are added on top of them only when `wind_direction_mode = 3`. A consumer reads the
mode once and knows which rows to expect.

| `wind_direction_mode` | Meaning |
|---|---|
| 1 | speed only: the venue has a field bearing, but no forecast hour reaches 8 mph sustained |
| 2 | speed only: `stadiums.field_bearing` is null (20 of 41 venues), checked first |
| 3 | split available: `wind_along_field_mph` / `wind_crosswind_mph` present |

| Signal | Formula | `sample_n` |
|---|---|---|
| `wind_speed_mph` | mean `wind_speed_10m_mph`, hour offsets 0–4 | 5 |
| `wind_gust_max_mph` | max `wind_gusts_10m_mph`, offsets 1–4, non-null only; no row if all null | non-null hours |
| `wind_along_field_mph` | mean over qualifying hours of \|v·cos(dir − field_bearing)\| | qualifying hours (of 5) |
| `wind_crosswind_mph` | mean over qualifying hours of \|v·sin(dir − field_bearing)\| | qualifying hours (of 5) |

- A qualifying hour has sustained speed ≥ 8 mph (`_DIRECTION_MIN_MPH`, P4.md) and a
  non-null direction. Gusts never qualify an hour.
- There's no minimum count of qualifying hours: `sample_n` says how much of the game the
  split covers.
- The components are magnitudes. The field is symmetric and teams switch ends every
  quarter, so headwind vs. tailwind (or wind "from" vs. "to") has no meaning for a whole
  game.
- **All wind is Open-Meteo's exterior 10 m estimate, a relative indicator only, never
  field-level wind** (`docs/sources.md`).

#### Other weather values (status 1 only)
Hour offsets: instantaneous variables use 0–4 (kickoff hour H through H+4).
Preceding-hour aggregates use 1–4, because offset 0 would describe the hour before
kickoff. A sum, max, or mean with any null or missing hour in its range is **omitted**,
never computed from part of the window.

| Signal | Formula |
|---|---|
| `temperature_f` / `apparent_temperature_f` | mean, offsets 0–4 |
| `precip_total_in` / `snowfall_total_in` | sum, offsets 1–4 |
| `precip_prob_max_pct` | max, offsets 1–4 |
| `weather_lead_hours` | the headline snapshot's `lead_hours` |
| `weather_model_regime_break` | 1 if the headline is t48 (GFS), else 0 |
| `weather_forecast_domain` | 1 = venue inside NOAA's HRRR CONUS grid, 2 = outside, meaning Open-Meteo's `best_match` model there is unverified: lower confidence, not a different number. Computed by projecting the stadium's coords onto the HRRR grid (`docs/sources.md`), not from a list of stadium IDs. |
| `venue_elevation_m` | the snapshot's `grid_elevation_m` (Open-Meteo's 90 m DEM elevation), used as altitude (Denver, Mexico City). Only for fetched games; a full-coverage version would need a `stadiums` elevation column (not built). |

#### Venue codes (game scope)
| `venue_roof_code` | Meaning |
|---|---|
| 1 | fixed roof |
| 2 | retractable, closed |
| 3 | retractable, open or not yet reported: label "if roof open" (nflverse leaves `games.roof` null before the game) |
| 4 | open-air |

Emitted only when the venue resolves; there's no row for status 5.

| `surface_code` | `games.surface` values |
|---|---|
| 1 | `grass` |
| 2 | `fieldturf`, `matrixturf`, `sportturf`, `a_turf`, `astroturf` |

Emitted for every game with a recognized value. `''`, null, or an unrecognized string gets
no row and is logged in `agent_runs.meta.unrecognized_surface`.

#### Rest, travel, timezone (team scope)
| Signal | Formula |
|---|---|
| `rest_days` | `games.home_rest` / `away_rest` |
| `rest_diff` | own rest − opponent's rest |
| `travel_miles` | great-circle miles, team's home venue → game venue (`stadiums` lat/lon) |
| `tz_shift_hours` | **primary.** Wrapped to [−12, +12): `((raw + 12) mod 24) − 12`. Positive = traveled east (SF at a 1pm ET game = +3, a 10am body-clock kickoff). |
| `tz_offset_diff_raw_hours` | unwrapped: game venue's UTC offset − home venue's, each at the kickoff instant via `zoneinfo` (DST and Arizona resolved per date) |

- **Home venue** is the `stadium_id` a team uses most for its REG home games that season.
  `games` has no neutral-site flag. In 2026 each team's own stadium beats its one
  international "home" game 8–1 (JAX 7–1). A tie is never broken by guessing: that team
  gets no travel or timezone rows, and the tie is logged in `agent_runs.meta`.
- **Home timezone** is that stadium's `stadiums.tz`, so there's no separate team → tz map
  to drift.
- At an international or neutral-site game, **both** teams travel and shift: DET's
  "home" game in Munich shows DET's trip too.
- **Magnitude is never clipped.** London and Munich read 5–9h, domestic trips ≤3h.
- **Wrapping only matters past 12h.** LA at Melbourne is raw +17 (AEST +10 vs. PDT −7),
  which is the same body-clock shift as −7 going west. `tz_shift_hours` reads −7, so it's
  directly comparable with every other game; `tz_offset_diff_raw_hours` keeps the +17.
  Read `tz_shift_hours`.
- No travel or timezone rows when the game's venue is unresolved (status 5). Rest rows are
  still written.

### Market sector (Phase 4)

Source: `pipeline/analysts/market.py`. Reads `games`, `odds_snapshot_targets`,
`odds_consensus`, `odds_snapshots`. **Descriptive only**: there is no handle, ticket, or
bet-split data, so no signal says who is betting or why a line moved. `league_pct` and
`stability` are always null. **Added:** Phase 4, 2026-09-23.

**Scope: per game, same window as Environment** (`now − 24h < kickoff ≤ now + 7d`).
Rows carry the game's own `season`/`week` from `games`, never the odds tables'
`season`/`week`, which pre-fix rows got wrong. Stale cleanup is scoped to the window's
`game_id`s (see "Stale-signal cleanup").

Two scopes, both with `game_id` set and `player_id` null:
- **game** (`team` null): everything except the two team signals below.
- **team** (`team` set, one row per side): `implied_team_total`, `win_prob_novig`.

Spreads are signed **from the home team's view**, the same convention as
`odds_snapshots.spread_home_point`: −3 = home favored by 3. Every capture is checked
against `games.home_team`/`away_team` first.
- If the odds tables have the teams the other way round (possible at a neutral site,
  since `odds.py` matches either order), spreads are negated and moneyline sides are
  swapped.
- If neither order matches, the capture is skipped and listed in
  `agent_runs.meta.orientation_mismatch`.

`inputs_version`:
- status 1: `odds_open@<as_of>,odds_current@<as_of>`
- status 2–3: `odds_current@<as_of>`
- status 4–5: `schedules@<nflverse timestamp>`

#### Captures, open, and current
- **A capture** is one poll's view of one game: an `odds_consensus` row plus that poll's
  `odds_snapshots` rows. Only captures with `as_of < kickoff` count.
- **Own-week vs. lookahead captures.** The Odds API returns every upcoming game, so a poll
  fired for week N's targets also stores week N+1's lines. `odds_snapshots.target_id`
  names the target of the week that *fired* the poll, not the game's week. A capture is
  **own-week** when its `as_of` equals a `captured_at` in `odds_snapshot_targets` for the
  game's `(season, week)`. `odds.py` writes `ctx.now` to both columns, so this is an
  exact match. Any other capture is **lookahead**.
- **Open** = the earliest own-week capture. Lookahead captures never become the open:
  - their timing depends on the previous week's schedule;
  - their book sets are thinner (live: 6 books on 09-18 vs. 9 on 09-22);
  - all 5 pre-fix rows with a null `game_id` are lookahead rows.
- **Current** = the latest capture of any kind, lookahead included.
  `market_current_lead_hours` shows how old it is.

#### `market_status` (game scope, emitted for every game in the window)
| Value | Meaning | Rows emitted |
|---|---|---|
| 1 | movement available: an own-week open plus a later capture | everything |
| 2 | one own-week capture: current *is* the open | current-state only. No open, move, velocity, crossing, or book-set rows: a single observation is not a move of 0. |
| 3 | lines exist, but none are own-week (lookahead only, or every own-week poll so far missed this game) | current-state only |
| 4 | awaiting: nothing captured, kickoff ahead | status + `market_own_week_captures` only |
| 5 | missed: nothing captured, kickoff passed | status + `market_own_week_captures` only |

The same **stale-status caveat** as `weather_status` applies. Analysts only run on ticks
where some collector wrote rows, so a stored 4 for a game that has already kicked off
means "computed before kickoff", not "still awaiting".

#### `market_open_basis` (status 1 only)
| Value | Meaning |
|---|---|
| 1 | the week's `tue_opener`, captured **on time**: before the Wednesday 09:00 ET after its window opened. This is today's window rule, applied to every row regardless of the stored `deadline` (pre-fix rows carry the old, much wider one). |
| 2 | the week's `tue_opener`, captured **late** (only possible under the pre-fix window). Week 2 2026's fired Fri 09-18 23:14Z. It's labeled opener but isn't an opening line. |
| 3 | no opener line for this game: the open comes from a later own-week target. Either the opener was missed, or its poll didn't resolve this game. |

#### Current-state signals (status 1–3)
| Signal | Formula | `sample_n` |
|---|---|---|
| `spread_home_current` / `total_current` | `odds_consensus` median at current | book count |
| `market_current_lead_hours` | kickoff − current `as_of`, hours | |
| `spread_book_range` / `total_book_range` | `spread_point_range` / `total_point_range` at current (max − min across books). **No row** when null (< 2 books), never 0. | book count |
| `spread_key_straddle` | 1 if, for any key k in {±3, ±7, ±10, ±14}, the current per-book spreads fall into at least two of {below k, exactly k, beyond k} (e.g. books at 2.5 and 3), else 0. The books don't agree which side of a key the line is on. No row with < 2 books. | books with a spread |
| `implied_team_total` (team) | home = total/2 − home_spread/2, away = total/2 + home_spread/2, from the current consensus medians. Spread and total medians may come from slightly different book sets. | min(spread, total book count) |
| `win_prob_novig` (team) | For each book with **both** moneyline prices: raw p = 100/(o+100) if o > 0, else −o/(−o+100). Vig removed proportionally: p_home = r_home/(r_home + r_away). Median across books; away = 1 − home, so the two sides sum to 1. Books missing a side are skipped; zero usable books → no row. | books used |
| `market_own_week_captures` | own-week pre-kickoff captures of this game (emitted for every status) | |

**`spread_key_straddle` is common, not notable** (measured 2026-09-23 with
`scripts/inspect_straddle_rate.py`, which calls the analyst's own `key_straddle()` on
every stored capture):

| Poll (UTC) | Captures | Straddle | Rate |
|---|---|---|---|
| 2026-09-18 23:14 | 24 | 7 | 29% |
| 2026-09-21 22:20 | 17 | 6 | 35% |
| 2026-09-22 16:03 | 16 | 8 | 50% |
| **All 2026** | 57 | 21 | 37% |

- The median book range is 0.5 at every poll. Books usually split by a half point, so
  any line sitting near 3, 7, 10, or 14 straddles.
- It fires on roughly a third to a half of games, not most. But it's too frequent to
  read as an alert.
- The matchup card shows it as context, with a note saying it's common.
- This is only three polls and one week of own-week captures. Re-run the script after a
  few more weeks and update this table.

Proportional de-vigging ignores the favorite-longshot bias. Shin/power methods and a
hold (overround) signal are not built.

#### Movement signals (status 1 only)
| Signal | Formula | `sample_n` |
|---|---|---|
| `spread_home_open` / `total_open` | consensus median at open | book count |
| `market_open_lead_hours` | kickoff − open `as_of`, hours | |
| `market_open_basis` | table above | |
| `spread_home_move` / `total_move` | current − open | min(open, current) book count |
| `spread_move_per_day` / `total_move_per_day` | move ÷ days between open and current | same |
| `spread_key_crossings` | number of keys k in {±3, ±7, ±10, ±14} with `(open − k)(current − k) < 0`: the consensus passed **strictly through** k. Landing on a key or leaving from one is not a crossing. Quarter-point medians count (2.25 → 3.25 crosses 3), and a favorite flip −3.5 → 3.5 crosses two. | |
| `spread_book_set_changed` / `total_book_set_changed` | 1 if the set of bookmakers carrying that market (non-null point in `odds_snapshots`) differs between open and current, else 0 | |
| `spread_book_count_open` / `_current`, `total_book_count_open` / `_current` | the consensus book count at each end | |

**Read a move together with its `*_book_set_changed`.** A consensus median can move
because books joined or left, not because any book moved: three books joining at a
different number can produce a 1.5-point "move" by themselves. At 1, the move may be
composition, not market movement. A matched-books-only move is not built.

**Velocity is a coarse average** over at most ~6 captures a week. It is not an
instantaneous rate, and it says nothing about why the line moved. A last-leg rate isn't
built. Key numbers for totals aren't built either.

### Stale-signal cleanup

`Analyst.run()` itself (`pipeline/core/base.py`) deletes every signal name an analyst
can write (`sector = <analyst's own> AND season/week = this run's AND signal =
ANY(<analyst's own signal_names>)`) before calling `write_signals` — not something each
analyst implements itself. Every `Analyst` subclass declares `sector: str` and
`signal_names: frozenset[str]` as class attributes; the base class's
`_delete_stale_signals` uses them to scope the delete so it can never reach another
analyst's rows (different `sector`) or an unrelated signal name (not in that analyst's
own `signal_names`) — see `pipeline/core/base.py`'s docstrings and
`tests/test_analyst_registry.py`, which asserts every registered analyst's `sector` is
distinct and their `signal_names` sets are pairwise disjoint, checked against the
dispatcher's actual registry so a newly-added analyst (market, environment, usage,
scheme, ...) is covered automatically. Both `EfficiencyAnalyst` (`signal_names` derived
from `_METRIC_CONFIG`, not hardcoded) and `AvailabilityImpactAnalyst` get this for free;
a future analyst gets it too just by declaring the two class attributes — no
`write_signals` implementation needs to know this happens. This run's output is
authoritative for its scope; a signal a prior run wrote that this run's (possibly
narrower) logic no longer produces does not survive. This is what caught and removed
the 151 single-snapshot `practice_trend_risk` rows from before the `sample_n >= 2` gate
existed — `upsert_rows` alone only ever inserts/updates the rows it's given.

**Exception: `EnvironmentAnalyst` scopes cleanup by game, not by week.** Its window spans
dispatcher weeks (see "Environment sector"). A `ctx.season`/`ctx.week` delete would miss
next week's games and wipe finished games' frozen rows. So it overrides
`_delete_stale_signals` to delete `sector = 'environment' AND signal = ANY(<its
signal_names>) AND game_id = ANY(<this run's window game_ids>)`. That is still limited to
its own sector and signal names, so the registry test's guarantee holds. **`MarketAnalyst`
does the same**, for the same reason (same per-game window).

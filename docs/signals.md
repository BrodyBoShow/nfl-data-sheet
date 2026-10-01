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

Contract (implemented by migrations `0031_player_usage_week.sql` and
`0032_player_eff_week.sql`, written 2026-09-26, applied 2026-09-29; the metrics are in the
registry under "Player tables (Phase 7)" below):
- **Key:** (`player_id`, `season`, `week`). Also `season_type`, `team`, `game_id` (the
  game this row's per-game values come from), `position_group` (`players.position_group`),
  `as_of`, `inputs_version` (plain text, the same convention as `signals`), and
  `content_hash` for `filter_changed`.
- **As-of rows:** a row is written for week W only for players who played in week W. To
  read "as of week W", take each player's latest row with `week <= W` in that season.
  A player on bye or injured keeps their last row. No row is ever written for a player
  with no inputs (missing stays missing).
  - `player_usage_week`: every player with a snap in week W.
  - `player_eff_week`: every player with a nonzero sample in at least one family in week
    W.
- **Columns per metric:** `<metric>_std` (season-to-date, prior-blended), `<metric>_game`
  (this game), `<metric>_l4` (last 4 games played), all `real`. Plus `<metric>_pct`, a
  `smallint` 0–100 league percentile of the `_std` value. A null means not sourced or no
  sample, never zero-filled.
  - Usage metrics also carry `<metric>_wow` (`real`): this game's `_game` minus the
    player's previous game's `_game`. Usage values are observed shares, not prior-blended.
- **Per family, not per metric:** a sample count and a `stability` (0–1, same meaning as
  in `signals`) for each family: usage, receiving (targets), rushing (carries), passing
  (dropbacks), defense (defensive snaps). The family's sample counts are themselves
  `_std`/`_game`/`_l4` columns: `rec_targets_*`, `rush_carries_*`, `pass_dropbacks_*`,
  `def_snaps_*`, and `usage_games_std`/`usage_games_l4` (`_game` would always be 1).
- **League percentile population:** by default, players in the same position group with
  a row as of that week whose family sample meets the metric's minimum. Each registry
  entry states its own population and minimum.
- **Retention (L4): deferred to P7 step 7, not built.** The proposal is that after a
  season completes, only each player's latest-week row for that season is kept. Nothing
  currently collapses or deletes these tables: `pipeline/orchestration/retention.py` lists
  both as not deleted. L4 would destroy point-in-time weekly history that can't be
  recomputed, so it's decided at step 7 with measured table sizes (`docs/phases/P7.md`,
  "Retention policies").
- **Participation-derived columns** end in `_hist`: multi-season historical tendencies,
  2016–2025, post-season release only. `inputs_version` names the season span, and the
  UI shows the span next to the value. Never presented as current-season behavior.
  - A `_hist` column is one value, with no `_std`/`_game`/`_l4`/`_pct`. It's constant
    within a season and not blended. `hist_span` holds its seasons (e.g. `2023-2025`),
    and a per-family `<family>_hist_n` holds the labeled plays behind it.
- **Honesty:** `_game`/`_l4` values for rotational players rest on a handful of plays
  (`docs/phases/P7.md` sample-size table). Anything that displays them shows `stability`
  beside them.
- **Access:** anon reads an allow-list of columns through `web.player_usage` /
  `web.player_eff` (migration `0033`, P7 step 9; the lists are in `docs/phases/P6.md`
  §2). Nothing else in these tables is readable, including `content_hash` and
  `inputs_version`. Every PFR- or NGS-derived column carries attribution wherever it's
  shown (`docs/sources.md`, nflverse bulk → License).

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
- **Try plays are out (decided 2026-09-30, P2 open item 1).** In the collector since
  2026-09-30; stored `team_week` changes when it's re-staged with the queued re-fit.
  - **Two-point tries** leave every play count and sum. The expression is the same
    `two_point_attempt != 1` as `_player_play_scope`, so `team_week` and
    `player_game_pbp` share one scrimmage scope.
    - All 130 of 2025's carried a non-null `epa`, and all were counted: 94 in `plays`, 36
      in `garbage_time_plays_excluded`.
  - **PAT and two-point tries** leave the drive aggregation too
    (`two_point_attempt`/`extra_point_attempt`, nulls kept as non-tries).
    - The try sits inside the 20 (PATs mostly at the 15, two-point tries mostly at the
      2), so it set `closest_yardline`. That made nearly every TD drive a red-zone trip
      and a red-zone TD.
    - A try alone under a `fixed_drive` made a phantom drive.
    - 2025 REG, before → after: `red_zone_trips` 1,962 → 1,636, `red_zone_tds` 1,193 →
      921, league rate 0.608 → 0.563, `drives` 5,271 → 5,206, `points` 9,873 → 9,825.
  - Null flags are marker rows (`GAME`, `END QUARTER`, `no_play`: 1,511 in 2025). None
    reaches the scrimmage scope, and the drive aggregation keeps them as before.
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

**Who counts as currently flagged** (`_resolve_current_state`): a player is flagged if
any source currently lists a non-healthy designation, and an ESPN clearance never
overrides an active Sleeper designation.
- Since 2026-09-25, ESPN `Injured Reserve`/`Out`/`Doubtful` are never cleared by absence.
  ESPN's feed is a 25-per-team recency window (`docs/sources.md`).
- When such a state has been missing from ESPN's polls past the miss threshold, it's
  **stale**:
  - It yields to any current Sleeper row for the player, flagged or cleared.
  - It yields to a snap in a game after ESPN last listed him.
  - Otherwise the player stays flagged with ESPN's designation.
- Staleness reads `injury_presence`, which is current state, so it's exact for the live
  week and approximate when an earlier week is recomputed. See `docs/phases/P3.md`,
  "Correctness item".

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

### Player tables (Phase 7)

The metric registry for `player_usage_week` (migration `0031`) and `player_eff_week`
(`0032`), drafted 2026-09-26 with the migrations (P7 step 3). Applied 2026-09-29. Step 6's
decisions (minimums, gating, k structure, `k_usage`) are recorded here as of 2026-09-29.
No analyst code yet. The column lists are the registry: every metric below has its
`_std`/`_game`/`_l4`/`_pct` columns (plus `_wow` for usage), and the migrations have no
other metrics. The inputs are the staged tables `player_game_pbp` (`0028`),
`participation_player_season` (`0029`), `player_week` (widened by `0030`), `snaps`,
`ngs`, and `pfr_advstats` (widened by `0027`).

**Player rates and team rates cover different plays.** Every player rate here
**includes garbage time**. Every team Efficiency signal **excludes** it
(`_garbage_time_expr`).
- So a receiver's `epa_per_target` and his team's `epa_per_play_off` are computed over
  different play sets. They aren't directly comparable, and the difference can't be
  seen from the numbers.
- The choice is deliberate (see "Play scope" below), but it must never be invisible.
- **Anything that shows a player rate next to, or compared with, a team rate states that
  the player rate includes garbage time and the team rate doesn't.** That covers the web
  player view (P7 step 9), matchup cards, and any narration. This is a P7 done-when
  item.

Compact per-family tables stand in for one template block per metric, as in the
Efficiency sector: the windows, blend, percentile, and null rules are shared, and only
the numerator, denominator, and source differ.

#### Rules shared by every player metric
- **Play scope:** all scrimmage plays, garbage time **included** (`0028`'s header).
  Team-level Efficiency excludes garbage time. The player tables can't, because
  `player_week`, `ngs`, and `pfr_advstats` are all-play and can't be filtered, so every
  player rate uses one scope. Two-point tries, kneels, spikes, and no-plays are out.
- **Windows:** every window is `Σ numerator / Σ denominator` over its games, never a mean
  of per-game rates. NGS averages are weighted by NGS's own count in that row
  (`attempts`/`rush_attempts`/`targets`, staged by `0027`).
  - `_game`: this row's game.
  - `_l4`: the player's last 4 games played, this one included. A game played is a
    `snaps` row. It's also a `player_game_pbp` row with no snaps row, which happened in
    8 player-games in 2025–2026 (a crosswalk gap), so the game still counts.
  - `_std`: every game of the season through this row's week. POST weeks continue the
    count.
- **Nulls:** a window is null when its denominator is 0 there. That covers no targets,
  no FTN-charted targets (FTN charts within 48h), no NGS row (NGS is thresholded: "below
  threshold", not zero), and no PFR row. The family sample being nonzero doesn't make a
  sub-denominator metric non-null.
- **Source preference:** where nflverse (pbp, `player_week`, FTN) and PFR/NGS both carry
  a quantity, nflverse is used. PFR and NGS are used only for what they alone have:
  - PFR: contact yards, broken tackles, pressures, blitzes, combined/missed tackles,
    nearest-defender allowed stats.
  - NGS: separation, cushion, RYOE, time to line of scrimmage, time to throw,
    aggressiveness, air yards to sticks.

  That keeps the provenance-basis attribution surface (`docs/sources.md`, nflverse bulk
  → License) as small as the metric set allows. The **Src** column below names each
  metric's sources, and every PFR or NGS source carries its credit wherever the metric is
  shown.
  - `snaps` is PFR-sourced, so **every usage snap share and every defense per-snap rate
    is PFR-derived** through its denominator.
  - **Where the credit goes (decided 2026-09-30, user, P7 step 9): at the column head,
    not per value.**
    - Every PFR (Sports Reference LLC), NGS or FTN column names its source in its header.
      `/sources` carries the full statements.
    - "A game played is a `snaps` row" (the `_l4` window, `usage_games_*`, and every
      per-game-played `_pct` minimum) isn't read literally as making every value
      PFR-derived. The page states once, plainly, that game counts and windows come
      from PFR snap counts, and credits SRL there.
- **Mixed-source rates** (numerator and denominator from different providers) are marked
  † below. Their attribution can disagree: a PFR pressure isn't guaranteed to sit on a
  pbp dropback. Read them as approximate.
- **Prior blend (`player_eff_week` `_std` only).** The same three-way form as the
  Efficiency sector, with no QB/OL discount:
  - `w_cur = n/(n+k)`, `w_prior = (1−w_cur)·r`, `w_league = 1−w_cur−w_prior`.
  - `n` is the metric's own current-season denominator.
  - **The prior is the player's own season−1 ratio, shrunk with the same `k` toward
    season−1's league value for his group:** `(Σnum + k·league₋₁) / (Σden + k)`. This
    matches Efficiency, whose prior is a `k_metric`-shrunk plain solve (decided
    2026-09-29, user; corrects the earlier "raw" wording).
    - With a raw prior, no constant `r` is right: a 5-target prior and a 150-target prior
      would get the same weight.
    - The prior is recomputed from the staged tables, which L2 retention keeps for
      `[season−1, season]`. It's never the previous season's blended final row.
    - It counts wherever the player played, so a traded player keeps his prior.
    - With no season−1 sample, `w_prior = 0` and the weight goes to the league.
  - The league value is the season-to-date `Σ/Σ` over the player's `position_group`.
  - `_game`/`_l4` are raw, never blended.
  - **`k` is per metric *and* per position group (decided 2026-09-29, user).** k0 varies
    by up to ~500× across groups for the same metric, so one k per metric would be wrong
    for some groups.
    - **Where the data pins it,** `k` = the pooled k0 from
      `scripts/estimate_player_reliability.py`. k0 is the sample at which a player's own
      rate is half signal (one-way random effects, 2001–2025 or the source's span).
    - "Pins it" means the 90% bootstrap interval of k0 is finite and within 2×, and
      r_corr's lower bound is above 0. That holds for 96 of 206 metric × group pairs
      (P7 step 6).
    - **Elsewhere (110), `k` is pinned by judgment**, like Efficiency's `k_metric`, and
      each entry's "k basis" says which rule:
      - **Split of a parent metric** (gap cells, field location, play action, vs blitz,
        stacked box, catchable/contested/drop): the parent's `k`, in the split's own
        units.
        - This departs from Efficiency, which sizes splits below the parent roughly in
          proportion to their share (plays 200 → pass/rush 120 → down 50).
        - Here the measured point k0s of splits sit near the parent's: RB `epa_per_carry`
          cells 65–408 vs 215; WR `epa_per_target` left/middle/right 179/150/155 vs 177.
        - Proportional sizing gave QB gap cells k ≈ 1, i.e. no shrinkage.
      - **Own denominator, finite point k0:** the point k0. Its interval is too wide to
        call it derived.
      - **No detectable between-player signal:** the family headline's `k` for the
        group, with `r = 0`. That's Efficiency's `down4` pattern: an ordinary k and no
        prior.
  - **`r` = `r_slope`, clipped to [0, 1] (decided 2026-09-29, user).** It's estimated
    offline by the same script from adjacent-season pairs of the same player in the same
    group. The DB holds only `[season−1, season]`.
    - `r_slope` is the weighted slope of next season's raw rate on this season's shrunk
      value. With a shrunk prior, it's exactly the coefficient the blend multiplies.
    - **`r_corr` is recorded beside it as the attenuated comparison only. It's never an
      input.** It's Efficiency's method, the pooled correlation of shrunk values in both
      seasons, and both seasons' noise pulls it down.
      - Example, WR `epa_per_target`: r_corr 0.22 (0.19–0.26), r_slope 0.79
        (0.69–0.93).
      - Teams show the same gap. Efficiency's r is attenuated too (`docs/phases/P2.md`,
        open item 2).
    - **No shrinkage toward a family mean.** Efficiency shrinks each r halfway toward its
      side mean because 7 pairs of 32 teams is thin. Here pairs number in the hundreds
      to thousands, and the bootstrap interval is recorded instead.
    - **When k isn't pinned,** `r` is still `r_slope` when r_corr's lower bound is above
      0 and `r_slope`'s 90% upper bound is ≤ 1.5. Otherwise `r = 0` (Efficiency's `down4`
      pattern: no reliable year-over-year signal).
  - **Position groups outside the estimate** borrow the family's primary group's `k` and
    `r`: receiving → WR, rushing → RB, passing → QB, defense → DB. That covers QB
    receiving, WR/TE rushing, non-QB passing, OL/SPEC, and offensive players with
    defensive snaps. It's judgment.
  - The k and r table is under "k and r by metric and position group" below. The
    analyst's constants (`pipeline/analysts/player_efficiency.py`, `_K_R`) are
    generated from the same rows (`estimate_player_reliability.py recommend`), and a
    test fails if the two differ.
- **Family `stability`:** the share of the family headline metric's `_std` that isn't
  league average. Headlines: receiving `epa_per_target`, rushing `epa_per_carry`,
  passing `epa_per_dropback`, defense `tackles_per_snap`.
  - Formula: `w_cur + w_prior · n₋₁/(n₋₁ + k)`, where `n₋₁` is the prior season's
    denominator.
  - The second factor exists because the prior is itself shrunk (above). Without it,
    `w_cur + w_prior` would count the prior's own league-average part as information.
    - Example: an RB with 40 prior-season targets and k 189 has a prior that's 83%
      league.
    - Efficiency's team priors rest on ~1,000 plays, so the factor is near 1 there. For
      players it isn't.
  - This keeps `stability`'s meaning, which the P6 dimming floor relies on (added
    2026-09-29 with the shrunk prior).
    - Whether the floor's *number* (0.2262, derived from team stability) is right for
      player values is unmeasured. It's an open question in `docs/phases/P6.md` §4.
    - **Display (decided 2026-09-30, user, P7 step 9): player values are never dimmed.**
      The family stability is shown as a number beside the values, and no threshold
      gates rendering. A player-level derivation (P6 §4) is what would justify dimming
      later.
- **`usage_stability = games/(games + k_usage)`, with `k_usage` = 2 games (set
  2026-09-29).**
  - k0 in games is measured per season and per share (unweighted one-way ANOVA). Median
    over seasons:
    - target share: WR 0.99, TE 1.13, RB 2.13 (range 1.81–2.79)
    - carry share, RB: 0.62
    - offense snap share: WR/TE/RB/OL 0.41–0.60
    - defense snap share: DL/LB/DB 0.40–0.55
    - That's 25 seasons for the pbp shares and 13 for snaps.
  - 2 is the largest median, rounded (RB target share). So `usage_stability` never
    overstates how settled any share is. For snap shares it's conservative.
- **`_pct`:** the rank of `_std`, `round(100 × (below + 0.5 × equal) / population)`.
  - Population: the same `position_group` (`players.position_group`: QB/RB/WR/TE/OL/DL/
    LB/DB/SPEC), among players whose row is the latest as of that week, and who meet
    **that position group's** minimum for the family. Minimums are per position group,
    not per family (decided 2026-09-29, user), because the population already is.
  - "Per game played" means the family's season-to-date sample ≥ minimum × games played
    to date. A game played is a `snaps` row.
  - **Criterion (P7 step 6, 2026-09-29):** the low point in the position group's
    per-game-played volume distribution, where incidental use gives way to a regular
    role. It's a role gate, not a noise gate.
    - A noise gate can't be set per game. For most headline rates, k0 (the sample at
      which a player's own rate is half signal) is 90–380 units, so no single game's
      volume gets a rate out of noise. `stability` carries the noise.
    - Where the distribution has no low point, the number is a judgment call, labeled
      as a guess.

    | Family | Group | Minimum per game played | Status |
    |---|---|---|---|
    | passing | QB | ≥ 15 dropbacks | **Derived** |
    | passing | other groups | ≥ 15 dropbacks (the QB number) | Guess |
    | rushing | QB | ≥ 2 designed-run carries | **Judgment, not derived** |
    | rushing | RB | ≥ 6 carries | Guess |
    | rushing | other groups | ≥ 6 carries (the RB guess) | Guess |
    | receiving | WR, TE, RB, others | ≥ 3 targets | Guess, and no data will derive it |
    | defense | DB, DL, LB, others | ≥ 20 defensive snaps | Guess |
    | usage | all | a non-null `_std` | — |

    - **Passing, QB: 15, derived.**
      - In 2025, dropbacks per game played has a low point at 15–19: 2 of 77 QBs, vs 9
        at 10–14 and 8 at 20–24.
      - 2013–2025 (951 QB player-seasons) shows the same flat low at 12–20, about 0.9%
        of player-seasons per dropback, vs 1.2% at 20–25 and about 4% at 30–40. 15 sits
        inside it.
      - QBs included at 2025 week 18 with a minimum of 10 / 15 / 20: 63 / 54 / 52 of 99
        with a dropback. By week: 32/33 in week 1, 37/54 in week 4, 43/69 in week 8.
    - **Rushing, QB: 2 designed-run carries per game played. A judgment call, not
      derived (decided 2026-09-29, user).**
      - **The low-point criterion used for QB passing doesn't apply here.** No low point
        exists: designed-run carries per game played decline steadily across 768 QB
        player-seasons (2013–2025). 31% are under 0.5, 28% at 0.5–1, 19% at 1–1.5, 7% at
        1.5–2, 5% at 2–2.5, and ≤ 2% per half-carry above that.
      - The sharpest step is 19% at 1–1.5 falling to 7% at 1.5–2. 2 sits just past that
        shoulder and keeps about the top 15% (116 of 768 player-seasons average ≥ 2).
        That's slightly more than the ~14% first estimated.
      - The RB number (6) would exclude every QB. None averaged 5 or more per game
        played in 2025, yet QB designed-run rates are among the most reliable rushing
        rates (EPA/carry k0 23, 19–34).
    - **Rushing, RB: 6, a guess.** The data gives a range, not a point.
      - 2025 per game: a dip at 4–7 carries (4.6–5.8% of player-games each, vs 11.7% at
        1 and 13.6% at 12–14).
      - 2013–2025 per game played (1,964 RB player-seasons): no low point. Density falls
        steadily from 0–1 through 15–18.
    - **Receiving, every group: 3, a guess, and a judgment call.** Targets per game (2025)
      and per game played (2013–2025: WR 2,751, TE 1,546, RB 1,916 player-seasons)
      decline steadily in every group. There's no empirical low point, so more data
      won't change this.
    - **Defense, every group: 20, a guess (DB downgraded 2026-09-29, user).**
      - DB: 2025 alone showed a low point at 15–24 snaps per game (3.7–4.0% of
        player-games per 5-snap bin, vs 9% at 1–4 and 10% at 60+). 2013–2025 per game
        played (4,867 DB player-seasons) doesn't reproduce it: flat from 10 to 50 snaps
        (5.8–6.8% per 5-snap bin), then a peak at 55–65. A one-season artifact doesn't
        get a derived label.
      - DL is single-peaked (mode 15–25 per game played). LB is flat from 0 to 65.
      - Moot until the defense gate below clears.
  - **Defense rate percentiles are gated by source (decided 2026-09-30, user; P7 step 7
    measured the missing-row question on 2025).**
    - **`player_week`-derived (`tfl`, `sacks`, `qb_hits`, `forced_fumbles`,
      `pass_defended` per snap): ungated.** A missing row reads as zero.
      - 1,172 of 1,172 defender-games with no row had no pbp credit on any play,
        special teams included (Wilson 95% lower bound 99.67%).
      - Where a row exists, pbp matches it exactly on four of the five numerators, and on
        forced fumbles in 99.83% of rows.
    - **`pfr_advstats`-derived (every other defense metric): gated, permanently as far
      as this source goes.** Neither reading is correct.
      - Read as zero, it drops 12.4% of 2025's pbp defensive tackles.
      - Present-only inflates every per-snap rate, because the denominator counts only
        the games with a row.
    - Usage and offense-family rates are not gated.
  - **Direction-neutral:** a high `stuff_rate_pct` is a high stuff rate. The display
    decides which way is good.
- **Which weeks a run writes, and stale rows (designed at step 6, 2026-09-29).**
  - A run for `(season, W)` recomputes every week ≤ W of that season that has inputs,
    not only the latest.
    - Late inputs land that way: FTN charts within 48h, and nflverse revises stats.
    - `filter_changed` writes only the rows whose `content_hash` moved.
    - Each row still uses only games through its own week, so there's no leakage.
  - `Analyst.run()`'s signals cleanup doesn't reach these tables, and these two analysts
    declare no `signal_names`. Instead, each analyst's write deletes its own table's rows
    for that season with week ≤ W whose `(player_id, week)` this run didn't produce.
    Then it upserts.
    - That's the "delete only within your own current scope and rewrite it in the same
      run" rule (CLAUDE.md), and it's one transaction.
    - Rows for weeks after W are never touched.

#### Usage family (`player_usage_week`)
- **Family:** usage. Sample: `usage_games_std`/`_l4` (games with a `snaps` row).
- **Team denominators**, per team-game:
  - Target, air-yard, carry, dropback, red-zone, end-zone, and goal-line totals are the
    sum over the team's `player_game_pbp` rows in that game. Plays with no attributed
    player (e.g. a throwaway, which has no `receiver_id`) are in no row.
  - Team snaps per side are recovered from PFR's own percentages: the median of
    `snaps ÷ pct` over the team's players with `pct ≥ 0.50` on that side, rounded. This
    has to be derived because no stored column holds it.
    - Using the max-snaps player fails: in 2025, 54 of 570 team-games had no defender at
      100%, and 552 had no ST player at 100%.
    - Null when nobody reaches 0.50.
    - **Verified 2026-09-29** against `team_week` scrimmage plays (`plays +
      garbage_time_plays_excluded`), with `usage.team_snaps` on live snaps.
      - Recovered offense snaps minus scrimmage plays, 2025: median +3, p5 +1, p95 +7,
        range −1 to +12, below zero in 2 of 570 team-games (both −1), never null.
      - 2026 weeks 1–3: median +4, range 0 to +9.
      - A small positive gap is expected: snaps include the no-plays, kneels and spikes
        that `team_week` drops.
      - Defense snaps mirror the opponent's offense exactly, and ST snaps are never null.
- `_std`/`_l4` sum the player's and the team's values over the player's own games, so a
  missed game doesn't dilute a share.
- **Prior blend:** none (observed shares). **Pct population:** the same `position_group`
  with a non-null `_std`.

| Metric | Numerator / denominator | Src |
|---|---|---|
| `off_snap_share` | `snaps.offense_snaps` / team offense snaps | PFR |
| `def_snap_share` | `snaps.defense_snaps` / team defense snaps | PFR |
| `st_snap_share` | `snaps.st_snaps` / team ST snaps | PFR |
| `target_share` | `targets` / team targets | pbp |
| `air_yards_share` | `rec_air_yards_sum` / team `rec_air_yards_sum` (negative air yards count) | pbp |
| `carry_share` | `carries` / team carries (designed runs) | pbp |
| `dropback_share` | `dropbacks` / team dropbacks (QB splits) | pbp |
| `rz_target_share` | `rz_targets` / team RZ targets (`yardline_100 ≤ 20`) | pbp |
| `ez_target_share` | `ez_targets` / team end-zone targets (`air_yards ≥ yardline_100`) | pbp |
| `rz_carry_share` | `rz_carries` / team RZ carries | pbp |
| `gl_carry_share` | `gl_carries` / team goal-line carries (`yardline_100 ≤ 5`) | pbp |
| `targets_per_off_snap` | `targets` / `snaps.offense_snaps` † (a per-snap rate, not per route: no free route data) | pbp, PFR |

`<m>_wow = _game − previous game's _game`. It's null on a player's first game of the
season, or when either value is null. **Added:** P7, 2026-09-26 (drafted).

**Display (decided 2026-10-01, user, P7 step 9): red-zone shares show season only.**
`rz_target_share` and `rz_carry_share` appear on the web as `_std` only, never `_game`.
- A single game's red-zone share rests on one or two touches and carries no stability.
  It's trivia, not a role signal.
- This isn't a width fallback. The columns are still stored, and anon can still read
  them (migration `0033`), but `web/lib/db.ts` doesn't select them.
- Snap, target, air-yards and carry share show season and last game.

#### Receiving family (`player_eff_week`)
- **Sample:** `rec_targets_*` = `player_game_pbp.targets`. **Minimum for pct:** 3 targets
  per game played, every group. It's a guess and a judgment call: there's no empirical
  low point (see the `_pct` rules above).
- **k, r:** per metric × group, recorded 2026-09-29 in "k and r by metric and position
  group" below.
  - Headline `epa_per_target`: WR k 177 (pinned), r 0.79; TE k 141 and RB k 189
    (judgment, point k0), r 0.94 / 0.72.
- **RB `epa_per_target` (decided 2026-09-29, user: kept, not special-cased).**
  - 2025 alone showed no detectable between-RB signal (τ² ≤ 0 in 80% of bootstrap
    draws).
  - The multi-season re-check (2001–2025, 2,595 RB pairs) finds a thin one: k0 189
    (131–355), r_corr 0.11 (0.06–0.15), r_slope 0.72.
  - Where RB stability lands: take a week-4 RB with 12 targets and 40 last season. With
    k 189 and r 0.72, stability is 12/201 + (189/201)·0.72·(40/229) ≈ 0.18. That's below
    the team-derived P6 floor (0.2262). Player values aren't dimmed (decided
    2026-09-30), so it shows at full weight beside its 0.18.
  - A heavy-volume RB (60 targets now, 90 prior) reaches ≈ 0.54. Re-check against live
    rows once the analyst runs.
    - **Live, 2026 week 3 (run 835):** RB `rec_stability` median 0.104. 70 of 77 RB rows
      are below 0.2262, and 18 of the 25 RBs with a headline `_pct`.
- **Added:** P7, 2026-09-26 (drafted).

| Metric | Numerator / denominator | Src |
|---|---|---|
| `epa_per_target` | `rec_epa_sum` / `targets` | pbp |
| `rec_success_rate` | `rec_success` / `targets` | pbp |
| `catch_rate` | `receptions` / `targets` | pbp |
| `yards_per_target` | `rec_yards` / `targets` | pbp |
| `rec_adot` | `rec_air_yards_sum` / `rec_air_yards_n` | pbp |
| `yac_per_reception` | `rec_yac_sum` / `receptions` | pbp |
| `yac_oe_per_reception` | `rec_yac_oe_sum` / `rec_yac_oe_n` (pbp's `xyac_mean_yardage` model, not NGS tracking) | pbp |
| `rec_first_down_rate` | `rec_first_downs` / `targets` | pbp |
| `rec_explosive_rate` | `rec_explosive` (20+ yd catches) / `targets` | pbp |
| `deep_target_rate` | `deep_targets` (air ≥ 20) / `targets` | pbp |
| `epa_per_target_left` / `_middle` / `_right` | `rec_epa_sum_<loc>` / `targets_<loc>`. A **field-location** split (`pass_location`), never slot/perimeter alignment | pbp |
| `catchable_catch_rate` | `ftn_catchable_receptions` / `ftn_catchable_targets` | FTN |
| `drop_rate` | `ftn_drops` / `ftn_catchable_targets` | FTN |
| `contested_target_rate` | `ftn_contested_targets` / `ftn_charted_targets` | FTN |
| `contested_catch_rate` | `ftn_contested_receptions` / `ftn_contested_targets` | FTN |
| `created_reception_rate` | `ftn_created_receptions` / `ftn_charted_receptions` | FTN |
| `screen_target_rate` | `ftn_screen_targets` / `ftn_charted_targets` | FTN |
| `epa_per_target_play_action` | `ftn_pa_rec_epa_sum` / `ftn_pa_targets` | FTN, pbp |
| `broken_tackles_per_reception` | `pfr_advstats.receiving_broken_tackles` (rec) / pbp `receptions` † | PFR, pbp |
| `avg_separation` | `ngs.avg_separation`, target-weighted (receiving rows) | NGS |
| `avg_cushion` | `ngs.avg_cushion`, target-weighted | NGS |

**`_hist` (participation, FTN era):**
- `epa_per_target_vs_man_hist`: `Σ rec_epa_sum_man / Σ targets_man`.
- `target_rate_vs_man_hist`: `Σ targets_man / Σ off_dropbacks_man`. It's targets per
  on-field dropback, not per route.
- Zone likewise.
- `rec_hist_n = Σ (off_dropbacks_man + off_dropbacks_zone)`.

#### Rushing family (`player_eff_week`)
- **Sample:** `rush_carries_*` = `player_game_pbp.carries`. Designed runs only;
  scrambles are the passer's dropbacks. **Minimum for pct:** RB and other groups, 6
  carries per game played (a guess; the data gives a 4–7 range in 2025 only). QB: 2
  designed-run carries per game played, a judgment call (no low point exists). See the
  `_pct` rules above.
- **Gap cells:** `run_location` left/middle/right × `run_gap` end/tackle/guard, with
  `run_gap` null on middle (fixture-verified), gives 7 cells: `le lt lg mid rg rt re`.
  Carries with a null `run_location` are in the family sample but in no cell, so the
  gap shares can sum below 1. Cells average ~2.4 carries per game (P7 sample-size
  table). `_game` cell values are trivia; read `_std` with `rush_stability`.
  - Across 2001–2025, most gap-cell EPA/success rates can't be pinned: k0's interval is
    unbounded or r_corr's reaches 0.
- **k, r:** per metric × group, recorded 2026-09-29 in "k and r by metric and position
  group" below.
  - Headline `epa_per_carry`: RB k 215 (pinned), r 0.51; QB (designed runs) k 23
    (pinned), r 0.67.
- **RB `epa_per_carry` (decided 2026-09-29, user: kept, not special-cased).**
  - 2025 alone showed essentially no between-RB signal: k0 ~4,500, with no signal in 45%
    of bootstrap draws.
  - The multi-season re-check (2001–2025, 2,556 RB pairs) finds k0 215 (186–270),
    r_corr 0.17 (0.12–0.22) and r_slope 0.51. The signal is real but thin.
  - **Where RB `rush_stability` lands, and it isn't always at the floor:**
    - A rotational back (20 carries now, 30 last season): 20/235 + (215/235)·0.51·(30/245)
      ≈ 0.14. That's below the team-derived P6 floor (0.2262).
    - A feature back at week 4 (60 now, 200 last season): 0.22 + 0.78·0.51·0.48 ≈ 0.41.
      That's above the floor.
    - The expectation that RB stability "lands at or below the floor" holds only for
      low-volume backs.
      - **Live, 2026 week 3 (run 835):** RB `rush_stability` median 0.255. 35 of 75 RB
        rows are below 0.2262, and 9 of the 48 RBs with a headline `_pct`.
    - Player values aren't dimmed either way (decided 2026-09-30). The number is shown.
- **Added:** P7, 2026-09-26 (drafted).

| Metric | Numerator / denominator | Src |
|---|---|---|
| `epa_per_carry` | `rush_epa_sum` / `carries` | pbp |
| `rush_success_rate` | `rush_success` / `carries` | pbp |
| `yards_per_carry` | `rush_yards` / `carries` | pbp |
| `stuff_rate` | `rush_stuffs` (≤ 0 yd) / `carries` | pbp |
| `rush_explosive_rate` | `rush_explosive` (10+ yd) / `carries` | pbp |
| `rush_first_down_rate` | `rush_first_downs` / `carries` | pbp |
| `gap_share_<cell>` (7) | `carries_<cell>` / `carries` | pbp |
| `epa_per_carry_<cell>` (7) | `rush_epa_sum_<cell>` / `carries_<cell>` | pbp |
| `rush_success_rate_<cell>` (7) | `rush_success_<cell>` / `carries_<cell>` | pbp |
| `stacked_box_rate` | `ftn_stacked_box_carries` (box ≥ 8) / `ftn_charted_carries` | FTN |
| `epa_per_carry_stacked_box` | `ftn_stacked_box_epa_sum` / `ftn_stacked_box_carries` | FTN, pbp |
| `yards_before_contact_per_carry` | `Σ rushing_yards_before_contact / Σ carries`, both `pfr_advstats` rush | PFR |
| `yards_after_contact_per_carry` | `Σ rushing_yards_after_contact / Σ carries`, both PFR | PFR |
| `broken_tackles_per_carry` | `Σ rushing_broken_tackles / Σ carries`, both PFR | PFR |
| `ryoe_per_carry` | `Σ ngs.rush_yards_over_expected / Σ ngs.rush_attempts` | NGS |
| `avg_time_to_los` | `ngs.avg_time_to_los`, `rush_attempts`-weighted | NGS |

No rushing `_hist`: participation has no rusher-level coverage field that fits.

#### Passing family (`player_eff_week`)
- **Sample:** `pass_dropbacks_*` = `player_game_pbp.dropbacks`, including sacks and
  scrambles. **Minimum for pct:** QB, 15 dropbacks per game played, **derived**
  (2026-09-29): the low point at 15–19. See the `_pct` rules above. Other groups use the
  same 15, which in practice excludes them.
- **k, r:** QB, recorded 2026-09-29 in "k and r by metric and position group" below.
  - Headline `epa_per_dropback`: k 199 (pinned; 90% 177–231), r 0.81 (r_corr 0.42).
  - 16 of 23 passing metrics are pinned. The seven that aren't are all FTN rates, with
    **3 season pairs, revisit at 5+** (2027 completes the 4th, 2028 the 5th):
    `blitzed_rate`, `catchable_rate`, `throwaway_rate`, `int_worthy_rate`,
    `qb_fault_sack_share`, `epa_per_dropback_play_action`, and
    `epa_per_dropback_vs_blitz`. Each carries a judgment k.
- **Added:** P7, 2026-09-26 (drafted).

| Metric | Numerator / denominator | Src |
|---|---|---|
| `epa_per_dropback` | `dropback_epa_sum` / `dropbacks` | pbp |
| `dropback_success_rate` | `dropback_success` / `dropbacks` | pbp |
| `cpoe` | `cpoe_sum` / `cpoe_n` (pbp's model; NGS's CPOE not used) | pbp |
| `pass_adot` | `pass_air_yards_sum` / `pass_air_yards_n` | pbp |
| `sack_rate` | `sacks` / `dropbacks` | pbp |
| `scramble_rate` | `scrambles` / `dropbacks` | pbp |
| `int_rate` | `interceptions` / `pass_attempts` | pbp |
| `deep_attempt_rate` | `deep_attempts` / `pass_attempts` | pbp |
| `play_action_rate` | `ftn_pa_dropbacks` / `ftn_charted_dropbacks` | FTN |
| `epa_per_dropback_play_action` | `ftn_pa_epa_sum` / `ftn_pa_dropbacks` | FTN, pbp |
| `blitzed_rate` | `ftn_blitzed_dropbacks` (`n_blitzers > 0`) / `ftn_charted_dropbacks` | FTN |
| `epa_per_dropback_vs_blitz` | `ftn_blitzed_epa_sum` / `ftn_blitzed_dropbacks` | FTN, pbp |
| `out_of_pocket_rate` | `ftn_out_of_pocket_dropbacks` / `ftn_charted_dropbacks` | FTN |
| `screen_rate` | `ftn_screen_attempts` / `ftn_charted_attempts` | FTN |
| `throwaway_rate` | `ftn_throwaways` / `ftn_charted_attempts` | FTN |
| `catchable_rate` | `ftn_catchable_attempts` / (`ftn_charted_attempts − ftn_throwaways`) | FTN |
| `int_worthy_rate` | `ftn_int_worthy` / `ftn_charted_attempts` | FTN |
| `qb_fault_sack_share` | `ftn_qb_fault_sacks` / `ftn_charted_sacks` | FTN |
| `pressure_rate` | `pfr_advstats.times_pressured` (pass) / pbp `dropbacks` † | PFR, pbp |
| `pressure_to_sack_rate` | `times_sacked / times_pressured`, both PFR pass | PFR |
| `avg_time_to_throw` | `ngs.avg_time_to_throw`, `attempts`-weighted | NGS |
| `aggressiveness` | `ngs.aggressiveness`, `attempts`-weighted | NGS |
| `avg_air_yards_to_sticks` | `ngs.avg_air_yards_to_sticks`, `attempts`-weighted | NGS |

**`_hist`:**
- `epa_per_dropback_vs_man_hist`: `Σ pass_epa_sum_man / Σ pass_dropbacks_man`.
- Zone likewise.
- `pass_hist_n = Σ (pass_dropbacks_man + pass_dropbacks_zone)`.

#### Defense family (`player_eff_week`)
- **Sample:** `def_snaps_*` = `snaps.defense_snaps`. Every per-snap rate is per
  *defensive snap*: not per pass rush, not per coverage snap, since neither exists in
  a free in-season source. **Minimum for pct:** 20 defensive snaps per game played, a
  guess in every group (DB's 2025 low point isn't reproduced 2013–2025). See the `_pct`
  rules above.
- **Percentiles gated by source (decided 2026-09-30, user; P7 step 7).**
  - **`player_week` numerators: a missing row is 0,** and these five metrics rank. Their
    per-snap denominator is every game with defense snaps.
  - **`pfr_advstats` numerators: `_pct` stays null.** Their denominator counts only games
    with a PFR def row, which is a known upward bias on every per-snap rate. (`snaps`,
    the denominator, is PFR-sourced too, but it's complete: the gap is `pfr_advstats`
    only.)
    - It's a source gap, not a collector bug. The raw nflverse 2025 def file has 7,926
      rows, exactly what's staged.
    - 2,889 of the 3,153 missing 2025 rows belong to players with PFR def rows in other
      games. Only 3 team-games (all week 13) are missing entirely.
    - **The zero rate falls with snaps.** Of missing rows, 75–84% had zero pbp credits at
      1–9 defensive snaps, and 0–7% at 40+.
  - The values themselves are still written in both cases.
  - **The family headline, `tackles_per_snap`, is PFR-derived.** So `def_stability`
    (headline-based) still rests on a gated metric. A pbp defender role is filed as a
    decision (`docs/phases/P7.md`, open item 9).
- The PFR allowed stats are **PFR's nearest-defender charting on targeted plays, not
  coverage assignments** (`docs/phases/P7.md`, "Coverage: who covered whom"). They have
  no untargeted snaps and no receiver identity.
- **k, r:** per metric × group, in "k and r by metric and position group" below.
  - The PFR-derived rows are estimated on present rows only (recorded 2026-09-29).
  - The five `player_week` rows were re-estimated 2026-09-30 under the zero reading.
    - k moved, for example DL `tfl` 1023 → 827 and `sacks` 688 → 595.
    - Every group kept its k basis (pinned or judgment).
  - For `tackles_per_snap`, missing read as zero vs present rows only gives DL k0 322 vs
    378, LB 88 vs 93, DB 322 vs 265. Moot while PFR stays gated.
  - `forced_fumbles_per_snap` and `int_rate_on_targets` can't be pinned in any group.
- **Added:** P7, 2026-09-26 (drafted).

| Metric | Numerator / denominator | Src |
|---|---|---|
| `tackles_per_snap` | `pfr_advstats.def_tackles_combined` / `def_snaps` | PFR |
| `missed_tackle_rate` | `def_missed_tackles` / (`def_tackles_combined + def_missed_tackles`), both PFR | PFR |
| `tfl_per_snap` | `player_week.def_tackles_for_loss` / `def_snaps` † | nflverse, PFR |
| `sacks_per_snap` | `player_week.def_sacks` (halves count 0.5) / `def_snaps` † | nflverse, PFR |
| `qb_hits_per_snap` | `player_week.def_qb_hits` / `def_snaps` † | nflverse, PFR |
| `pressures_per_snap` | `pfr_advstats.def_pressures` / `def_snaps` | PFR |
| `blitzes_per_snap` | `def_times_blitzed` / `def_snaps` | PFR |
| `forced_fumbles_per_snap` | `player_week.def_fumbles_forced` / `def_snaps` † | nflverse, PFR |
| `pass_defended_per_snap` | `player_week.def_pass_defended` / `def_snaps` †. Per snap, not per PFR target, since the two providers attribute plays independently | nflverse, PFR |
| `targets_per_snap` | `def_targets` / `def_snaps` (targeted as PFR's nearest defender) | PFR |
| `completion_pct_allowed` | `def_completions_allowed` / `def_targets` | PFR |
| `yards_per_target_allowed` | `def_yards_allowed` / `def_targets` | PFR |
| `yac_allowed_per_completion` | `def_yards_after_catch` / `def_completions_allowed` | PFR |
| `adot_allowed` | `def_adot`, `def_targets`-weighted | PFR |
| `td_rate_allowed` | `def_receiving_td_allowed` / `def_targets` | PFR |
| `int_rate_on_targets` | `def_ints` / `def_targets` | PFR |

No defense `_hist`. The natural one, PFR targets per on-field coverage dropback, needs
PFR rows for the `_hist` seasons, and L2 retention keeps `pfr_advstats` only for
`[season−1, season]`.

#### k and r by metric and position group (recorded 2026-09-29)
- **Generated.** `scripts/estimate_player_reliability.py fetch` → `estimate` →
  `recommend` (P7 step 6) produced these rows, and the analyst's `_K_R` constants come
  from the same run. Rules are under "Prior blend" above.
- **k basis:** `pinned` = pooled k0, 90% interval finite and within 2×. `J:` = judgment,
  with the rule named.
- **r basis:** `r_slope` = the estimate, clipped to [0, 1]. `0:` = Efficiency's `down4`
  pattern, with the reason given.
- **r_corr is the attenuated comparison only. It's never an input.**
- **Seasons (pairs):** the estimation span and the count of adjacent-season player
  pairs.
- **Defense:** PFR-derived rows are present-only. The five `player_week` rows use the zero
  reading, re-estimated 2026-09-30 (P7 step 7). `recommend` picks the reading per metric
  (`ZERO_READING`).
- **Groups not listed** borrow the family's primary group (receiving WR, rushing RB,
  passing QB, defense DB).

**Receiving**

| Metric | Group | k | k basis | r | r basis | r_corr (attenuated) | Seasons (pairs) |
|---|---|---|---|---|---|---|---|
| `epa_per_target` | WR | 177 | pinned | 0.79 | r_slope | 0.22 | 2001-2025 (3432) |
| `epa_per_target` | TE | 141 | J: point k0 (interval too wide) | 0.94 | r_slope (k judgment) | 0.24 | 2001-2025 (1953) |
| `epa_per_target` | RB | 189 | J: point k0 (interval too wide) | 0.72 | r_slope (k judgment) | 0.11 | 2001-2025 (2595) |
| `rec_success_rate` | WR | 100 | pinned | 0.79 | r_slope | 0.30 | 2001-2025 (3432) |
| `rec_success_rate` | TE | 130 | pinned | 0.90 | r_slope | 0.24 | 2001-2025 (1953) |
| `rec_success_rate` | RB | 126 | pinned | 0.58 | r_slope | 0.12 | 2001-2025 (2595) |
| `catch_rate` | WR | 64 | pinned | 0.81 | r_slope | 0.38 | 2001-2025 (3432) |
| `catch_rate` | TE | 128 | pinned | 0.76 | r_slope | 0.20 | 2001-2025 (1953) |
| `catch_rate` | RB | 99 | pinned | 0.50 | r_slope | 0.12 | 2001-2025 (2595) |
| `yards_per_target` | WR | 154 | pinned | 0.77 | r_slope | 0.24 | 2001-2025 (3432) |
| `yards_per_target` | TE | 118 | pinned | 1.00 | r_slope | 0.32 | 2001-2025 (1953) |
| `yards_per_target` | RB | 150 | J: point k0 (interval too wide) | 0.71 | r_slope (k judgment) | 0.13 | 2001-2025 (2595) |
| `rec_adot` | WR | 19 | pinned | 0.87 | r_slope | 0.58 | 2006-2025 (2793) |
| `rec_adot` | TE | 22 | pinned | 0.81 | r_slope | 0.45 | 2006-2025 (1609) |
| `rec_adot` | RB | 18 | pinned | 0.79 | r_slope | 0.42 | 2006-2025 (2039) |
| `yac_per_reception` | WR | 53 | pinned | 0.91 | r_slope | 0.38 | 2001-2025 (3320) |
| `yac_per_reception` | TE | 48 | pinned | 0.91 | r_slope | 0.34 | 2001-2025 (1888) |
| `yac_per_reception` | RB | 80 | J: point k0 (interval too wide) | 0.77 | r_slope (k judgment) | 0.17 | 2001-2025 (2499) |
| `yac_oe_per_reception` | WR | 111 | pinned | 0.99 | r_slope | 0.28 | 2006-2025 (2681) |
| `yac_oe_per_reception` | TE | 71 | J: point k0 (interval too wide) | 0.96 | r_slope (k judgment) | 0.29 | 2006-2025 (1535) |
| `yac_oe_per_reception` | RB | 274 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment) | 0.09 | 2006-2025 (1952) |
| `rec_first_down_rate` | WR | 113 | pinned | 0.72 | r_slope | 0.26 | 2001-2025 (3432) |
| `rec_first_down_rate` | TE | 99 | pinned | 0.87 | r_slope | 0.27 | 2001-2025 (1953) |
| `rec_first_down_rate` | RB | 130 | pinned | 0.93 | r_slope | 0.19 | 2001-2025 (2595) |
| `rec_explosive_rate` | WR | 185 | pinned | 0.69 | r_slope | 0.19 | 2001-2025 (3432) |
| `rec_explosive_rate` | TE | 142 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment) | 0.26 | 2001-2025 (1953) |
| `rec_explosive_rate` | RB | 402 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.09 | 2001-2025 (2595) |
| `deep_target_rate` | WR | 35 | pinned | 0.85 | r_slope | 0.48 | 2001-2025 (3432) |
| `deep_target_rate` | TE | 58 | pinned | 0.67 | r_slope | 0.27 | 2001-2025 (1953) |
| `deep_target_rate` | RB | 48 | J: point k0 (interval too wide) | 0.71 | r_slope (k judgment) | 0.26 | 2001-2025 (2595) |
| `epa_per_target_left` | WR | 177 | J: split of `epa_per_target` (0.31 of its units), parent k | 0.57 | r_slope (k judgment) | 0.08 | 2001-2025 (2603) |
| `epa_per_target_left` | TE | 141 | J: split of `epa_per_target` (0.28 of its units), parent k | 0.98 | r_slope (k judgment) | 0.12 | 2004-2025 (1384) |
| `epa_per_target_left` | RB | 189 | J: split of `epa_per_target` (0.31 of its units), parent k | 0.00 | 0: no season pairs | n/a | 2002-2025 (0) |
| `epa_per_target_middle` | WR | 177 | J: split of `epa_per_target` (0.18 of its units), parent k | 0.50 | r_slope (k judgment) | 0.05 | 2002-2025 (2438) |
| `epa_per_target_middle` | TE | 141 | J: split of `epa_per_target` (0.26 of its units), parent k | 0.00 | 0: r_slope unstable | 0.08 | 2003-2025 (1346) |
| `epa_per_target_middle` | RB | 189 | J: split of `epa_per_target` (0.21 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.02 | 2005-2025 (1456) |
| `epa_per_target_right` | WR | 177 | J: split of `epa_per_target` (0.32 of its units), parent k | 1.00 | r_slope (k judgment) | 0.18 | 2001-2025 (2621) |
| `epa_per_target_right` | TE | 141 | J: split of `epa_per_target` (0.34 of its units), parent k | 0.00 | 0: r_slope unstable | 0.12 | 2001-2025 (1490) |
| `epa_per_target_right` | RB | 189 | J: split of `epa_per_target` (0.37 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.01 | 2004-2025 (1853) |
| `catchable_catch_rate` | WR | 64 | J: split of `catch_rate` (0.71 of its units), parent k | 0.84 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.27 | 2022-2025 (467) |
| `catchable_catch_rate` | TE | 128 | J: split of `catch_rate` (0.79 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | 0.07 | 2022-2025 (272) |
| `catchable_catch_rate` | RB | 99 | J: split of `catch_rate` (0.85 of its units), parent k | 0.40 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.10 | 2022-2025 (287) |
| `drop_rate` | WR | 64 | J: split of `catch_rate` (0.71 of its units), parent k | 0.78 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.16 | 2022-2025 (467) |
| `drop_rate` | TE | 128 | J: split of `catch_rate` (0.79 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | 0.05 | 2022-2025 (272) |
| `drop_rate` | RB | 99 | J: split of `catch_rate` (0.85 of its units), parent k | 0.44 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.11 | 2022-2025 (287) |
| `contested_target_rate` | WR | 82 | pinned | 0.73 | r_slope; 3 season pairs, revisit at 5+ | 0.32 | 2022-2025 (484) |
| `contested_target_rate` | TE | 120 | J: point k0 (interval too wide) | 0.83 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.29 | 2022-2025 (275) |
| `contested_target_rate` | RB | 468 | J: point k0 (interval too wide) | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | 0.14 | 2022-2025 (296) |
| `contested_catch_rate` | WR | 64 | J: split of `catch_rate` (0.17 of its units), parent k | 0.00 | 0: r_slope unstable; 3 season pairs, revisit at 5+ | 0.15 | 2022-2025 (394) |
| `contested_catch_rate` | TE | 128 | J: split of `catch_rate` (0.14 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | -0.11 | 2022-2025 (192) |
| `contested_catch_rate` | RB | 99 | J: split of `catch_rate` (0.05 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | 0.04 | 2022-2025 (126) |
| `created_reception_rate` | WR | 74 | J: point k0 (interval too wide) | 0.78 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.27 | 2022-2025 (459) |
| `created_reception_rate` | TE | 13871 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable; 3 season pairs, revisit at 5+ | 0.30 | 2022-2025 (269) |
| `created_reception_rate` | RB | 13114 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable; 3 season pairs, revisit at 5+ | 0.17 | 2022-2025 (278) |
| `screen_target_rate` | WR | 19 | pinned | 0.76 | r_slope; 3 season pairs, revisit at 5+ | 0.49 | 2022-2025 (484) |
| `screen_target_rate` | TE | 39 | J: point k0 (interval too wide) | 0.93 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.49 | 2022-2025 (275) |
| `screen_target_rate` | RB | 31 | pinned | 0.54 | r_slope; 3 season pairs, revisit at 5+ | 0.25 | 2022-2025 (296) |
| `epa_per_target_play_action` | WR | 177 | J: split of `epa_per_target` (0.19 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | 0.05 | 2022-2025 (413) |
| `epa_per_target_play_action` | TE | 141 | J: split of `epa_per_target` (0.27 of its units), parent k | 0.00 | 0: no season pairs; 3 season pairs, revisit at 5+ | n/a | 2022-2025 (0) |
| `epa_per_target_play_action` | RB | 189 | J: split of `epa_per_target` (0.21 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | 0.07 | 2022-2025 (232) |
| `broken_tackles_per_reception` | WR | 169 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.29 | 2018-2025 (1079) |
| `broken_tackles_per_reception` | TE | 351 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.31 | 2018-2025 (617) |
| `broken_tackles_per_reception` | RB | 169 | J: no detectable signal, WR point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2018-2025 (0) |
| `avg_separation` | WR | 35 | pinned | 0.87 | r_slope | 0.52 | 2016-2025 (927) |
| `avg_separation` | TE | 67 | J: point k0 (interval too wide) | 0.93 | r_slope (k judgment) | 0.34 | 2016-2025 (364) |
| `avg_separation` | RB | 35 | J: no detectable signal, WR point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2016-2023 (0) |
| `avg_cushion` | WR | 50 | pinned | 0.70 | r_slope | 0.39 | 2016-2025 (927) |
| `avg_cushion` | TE | 164 | J: point k0 (interval too wide) | 0.76 | r_slope (k judgment) | 0.18 | 2016-2025 (364) |
| `avg_cushion` | RB | 50 | J: no detectable signal, WR point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2016-2023 (0) |

**Rushing**

| Metric | Group | k | k basis | r | r basis | r_corr (attenuated) | Seasons (pairs) |
|---|---|---|---|---|---|---|---|
| `epa_per_carry` | RB | 215 | pinned | 0.51 | r_slope | 0.17 | 2001-2025 (2556) |
| `epa_per_carry` | QB | 23 | pinned | 0.67 | r_slope | 0.19 | 2001-2025 (994) |
| `rush_success_rate` | RB | 196 | pinned | 0.57 | r_slope | 0.20 | 2001-2025 (2556) |
| `rush_success_rate` | QB | 22 | pinned | 0.74 | r_slope | 0.23 | 2001-2025 (994) |
| `yards_per_carry` | RB | 241 | pinned | 0.63 | r_slope | 0.21 | 2001-2025 (2556) |
| `yards_per_carry` | QB | 13 | pinned | 0.95 | r_slope | 0.42 | 2001-2025 (994) |
| `stuff_rate` | RB | 251 | pinned | 0.57 | r_slope | 0.18 | 2001-2025 (2556) |
| `stuff_rate` | QB | 10 | pinned | 0.78 | r_slope | 0.32 | 2001-2025 (994) |
| `rush_explosive_rate` | RB | 258 | pinned | 0.70 | r_slope | 0.22 | 2001-2025 (2556) |
| `rush_explosive_rate` | QB | 24 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment) | 0.39 | 2001-2025 (994) |
| `rush_first_down_rate` | RB | 116 | pinned | 0.61 | r_slope | 0.25 | 2001-2025 (2556) |
| `rush_first_down_rate` | QB | 19 | pinned | 0.83 | r_slope | 0.28 | 2001-2025 (994) |
| `gap_share_le` | RB | 56 | pinned | 0.74 | r_slope | 0.44 | 2001-2025 (2556) |
| `gap_share_le` | QB | 16 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment) | 0.48 | 2001-2025 (994) |
| `epa_per_carry_le` | RB | 215 | J: split of `epa_per_carry` (0.11 of its units), parent k | 0.57 | r_slope (k judgment) | 0.08 | 2001-2025 (1877) |
| `epa_per_carry_le` | QB | 23 | J: split of `epa_per_carry` (0.14 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | -0.02 | 2001-2025 (272) |
| `rush_success_rate_le` | RB | 196 | J: split of `rush_success_rate` (0.11 of its units), parent k | 0.00 | 0: r_slope unstable | 0.05 | 2001-2025 (1877) |
| `rush_success_rate_le` | QB | 22 | J: split of `rush_success_rate` (0.14 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.07 | 2001-2025 (272) |
| `gap_share_lt` | RB | 111 | pinned | 0.60 | r_slope | 0.29 | 2001-2025 (2556) |
| `gap_share_lt` | QB | 43 | J: point k0 (interval too wide) | 0.87 | r_slope (k judgment) | 0.25 | 2001-2025 (994) |
| `epa_per_carry_lt` | RB | 215 | J: split of `epa_per_carry` (0.13 of its units), parent k | 0.93 | r_slope (k judgment) | 0.08 | 2001-2025 (1917) |
| `epa_per_carry_lt` | QB | 23 | J: split of `epa_per_carry` (0.04 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.01 | 2001-2025 (99) |
| `rush_success_rate_lt` | RB | 196 | J: split of `rush_success_rate` (0.13 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.04 | 2001-2025 (1917) |
| `rush_success_rate_lt` | QB | 22 | J: split of `rush_success_rate` (0.04 of its units), parent k | 0.00 | 0: no season pairs | n/a | 2001-2025 (0) |
| `gap_share_lg` | RB | 99 | pinned | 0.73 | r_slope | 0.36 | 2001-2025 (2556) |
| `gap_share_lg` | QB | 57 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.36 | 2001-2025 (994) |
| `epa_per_carry_lg` | RB | 215 | J: split of `epa_per_carry` (0.12 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.01 | 2001-2025 (1941) |
| `epa_per_carry_lg` | QB | 23 | J: split of `epa_per_carry` (0.04 of its units), parent k | 0.00 | 0: r_slope unstable | 0.26 | 2001-2025 (101) |
| `rush_success_rate_lg` | RB | 196 | J: split of `rush_success_rate` (0.12 of its units), parent k | 0.54 | r_slope (k judgment) | 0.07 | 2001-2025 (1941) |
| `rush_success_rate_lg` | QB | 22 | J: split of `rush_success_rate` (0.04 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.17 | 2001-2025 (101) |
| `gap_share_mid` | RB | 41 | pinned | 0.80 | r_slope | 0.49 | 2001-2025 (2556) |
| `gap_share_mid` | QB | 9.2 | pinned | 0.96 | r_slope | 0.44 | 2001-2025 (994) |
| `epa_per_carry_mid` | RB | 215 | J: split of `epa_per_carry` (0.28 of its units), parent k | 0.73 | r_slope (k judgment) | 0.15 | 2001-2025 (2323) |
| `epa_per_carry_mid` | QB | 23 | J: split of `epa_per_carry` (0.35 of its units), parent k | 0.54 | r_slope (k judgment) | 0.12 | 2001-2025 (702) |
| `rush_success_rate_mid` | RB | 132 | pinned | 0.60 | r_slope | 0.13 | 2001-2025 (2323) |
| `rush_success_rate_mid` | QB | 11 | pinned | 0.75 | r_slope | 0.23 | 2001-2025 (702) |
| `gap_share_rg` | RB | 76 | pinned | 0.80 | r_slope | 0.43 | 2001-2025 (2556) |
| `gap_share_rg` | QB | 43 | J: point k0 (interval too wide) | 0.69 | r_slope (k judgment) | 0.18 | 2001-2025 (994) |
| `epa_per_carry_rg` | RB | 215 | J: split of `epa_per_carry` (0.13 of its units), parent k | 0.29 | r_slope (k judgment) | 0.05 | 2001-2025 (1959) |
| `epa_per_carry_rg` | QB | 23 | J: split of `epa_per_carry` (0.05 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | -0.16 | 2001-2025 (131) |
| `rush_success_rate_rg` | RB | 196 | J: split of `rush_success_rate` (0.13 of its units), parent k | 0.00 | 0: r_slope unstable | 0.11 | 2001-2025 (1959) |
| `rush_success_rate_rg` | QB | 22 | J: split of `rush_success_rate` (0.05 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | -0.04 | 2001-2025 (131) |
| `gap_share_rt` | RB | 110 | pinned | 0.63 | r_slope | 0.31 | 2001-2025 (2556) |
| `gap_share_rt` | QB | 150 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.32 | 2001-2025 (994) |
| `epa_per_carry_rt` | RB | 215 | J: split of `epa_per_carry` (0.13 of its units), parent k | 0.00 | 0: r_slope unstable | 0.05 | 2001-2025 (1936) |
| `epa_per_carry_rt` | QB | 23 | J: split of `epa_per_carry` (0.04 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.16 | 2001-2025 (96) |
| `rush_success_rate_rt` | RB | 196 | J: split of `rush_success_rate` (0.13 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.04 | 2001-2025 (1936) |
| `rush_success_rate_rt` | QB | 22 | J: split of `rush_success_rate` (0.04 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.03 | 2001-2025 (96) |
| `gap_share_re` | RB | 61 | pinned | 0.74 | r_slope | 0.44 | 2001-2025 (2556) |
| `gap_share_re` | QB | 14 | pinned | 0.97 | r_slope | 0.39 | 2001-2025 (994) |
| `epa_per_carry_re` | RB | 215 | J: split of `epa_per_carry` (0.10 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.01 | 2001-2025 (1826) |
| `epa_per_carry_re` | QB | 23 | J: split of `epa_per_carry` (0.14 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.02 | 2001-2025 (292) |
| `rush_success_rate_re` | RB | 196 | J: split of `rush_success_rate` (0.10 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.01 | 2001-2025 (1826) |
| `rush_success_rate_re` | QB | 22 | J: split of `rush_success_rate` (0.14 of its units), parent k | 0.59 | r_slope (k judgment) | 0.09 | 2001-2025 (292) |
| `stacked_box_rate` | RB | 113 | pinned | 0.41 | r_slope; 3 season pairs, revisit at 5+ | 0.22 | 2022-2025 (308) |
| `stacked_box_rate` | QB | 16 | J: point k0 (interval too wide) | 0.62 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.27 | 2022-2025 (146) |
| `epa_per_carry_stacked_box` | RB | 215 | J: split of `epa_per_carry` (0.13 of its units), parent k | 0.00 | 0: no season pairs; 3 season pairs, revisit at 5+ | n/a | 2022-2025 (0) |
| `epa_per_carry_stacked_box` | QB | 23 | J: split of `epa_per_carry` (0.22 of its units), parent k | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | -0.07 | 2022-2025 (77) |
| `yards_before_contact_per_carry` | RB | 229 | pinned | 0.67 | r_slope | 0.24 | 2018-2025 (758) |
| `yards_before_contact_per_carry` | QB | 26 | pinned | 0.92 | r_slope | 0.42 | 2018-2025 (401) |
| `yards_after_contact_per_carry` | RB | 265 | J: point k0 (interval too wide) | 0.79 | r_slope (k judgment) | 0.24 | 2018-2025 (758) |
| `yards_after_contact_per_carry` | QB | 30 | J: point k0 (interval too wide) | 0.80 | r_slope (k judgment) | 0.45 | 2018-2025 (401) |
| `broken_tackles_per_carry` | RB | 613 | J: point k0 (interval too wide) | 0.87 | r_slope (k judgment) | 0.18 | 2018-2025 (758) |
| `broken_tackles_per_carry` | QB | 175 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.32 | 2018-2025 (401) |
| `ryoe_per_carry` | RB | 661 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.25 | 2018-2025 (398) |
| `avg_time_to_los` | RB | 45 | pinned | 0.62 | r_slope | 0.41 | 2016-2025 (503) |

- NGS rushing weekly rows, 2016–2025 regular season, joined to `players.position_group`:
  5,309 RB, 6 WR, and **no QB**. Measured 2026-09-29; why NGS omits QBs isn't recorded.
- So QBs get no `ryoe_per_carry`/`avg_time_to_los` (null, never estimated). The
  borrowed-group rule only matters if a QB ever appears.

**Passing**

| Metric | Group | k | k basis | r | r basis | r_corr (attenuated) | Seasons (pairs) |
|---|---|---|---|---|---|---|---|
| `epa_per_dropback` | QB | 199 | pinned | 0.81 | r_slope | 0.42 | 2001-2025 (1332) |
| `dropback_success_rate` | QB | 178 | pinned | 0.81 | r_slope | 0.45 | 2001-2025 (1332) |
| `cpoe` | QB | 233 | pinned | 0.91 | r_slope | 0.45 | 2006-2025 (1042) |
| `pass_adot` | QB | 213 | pinned | 0.66 | r_slope | 0.35 | 2006-2025 (1046) |
| `sack_rate` | QB | 189 | pinned | 0.72 | r_slope | 0.40 | 2001-2025 (1332) |
| `scramble_rate` | QB | 67 | pinned | 0.94 | r_slope | 0.68 | 2001-2025 (1332) |
| `int_rate` | QB | 757 | pinned | 0.54 | r_slope | 0.15 | 2001-2025 (1324) |
| `deep_attempt_rate` | QB | 360 | pinned | 0.63 | r_slope | 0.27 | 2001-2025 (1324) |
| `play_action_rate` | QB | 155 | pinned | 0.35 | r_slope; 3 season pairs, revisit at 5+ | 0.22 | 2022-2025 (181) |
| `epa_per_dropback_play_action` | QB | 199 | J: split of `epa_per_dropback` (0.22 of its units), parent k | 1.00 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.37 | 2022-2025 (167) |
| `blitzed_rate` | QB | 1347 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable; 3 season pairs, revisit at 5+ | 0.32 | 2022-2025 (181) |
| `epa_per_dropback_vs_blitz` | QB | 199 | J: split of `epa_per_dropback` (0.27 of its units), parent k | 0.56 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.16 | 2022-2025 (170) |
| `out_of_pocket_rate` | QB | 73 | pinned | 0.95 | r_slope; 3 season pairs, revisit at 5+ | 0.68 | 2022-2025 (181) |
| `screen_rate` | QB | 221 | pinned | 0.38 | r_slope; 3 season pairs, revisit at 5+ | 0.24 | 2022-2025 (179) |
| `throwaway_rate` | QB | 283 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.45 | 2022-2025 (179) |
| `catchable_rate` | QB | 475 | J: point k0 (interval too wide) | 0.88 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.29 | 2022-2025 (177) |
| `int_worthy_rate` | QB | 531 | J: point k0 (interval too wide) | 0.64 | r_slope (k judgment); 3 season pairs, revisit at 5+ | 0.21 | 2022-2025 (179) |
| `qb_fault_sack_share` | QB | 215 | J: point k0 (interval too wide) | 0.00 | 0: no YoY signal (r_corr reaches 0); 3 season pairs, revisit at 5+ | 0.03 | 2022-2025 (154) |
| `pressure_rate` | QB | 246 | pinned | 0.73 | r_slope | 0.40 | 2018-2025 (399) |
| `pressure_to_sack_rate` | QB | 90 | pinned | 0.69 | r_slope | 0.30 | 2018-2025 (360) |
| `avg_time_to_throw` | QB | 81 | pinned | 0.72 | r_slope | 0.56 | 2016-2025 (388) |
| `aggressiveness` | QB | 387 | pinned | 0.77 | r_slope | 0.37 | 2016-2025 (388) |
| `avg_air_yards_to_sticks` | QB | 258 | pinned | 0.64 | r_slope | 0.33 | 2016-2025 (388) |

**Defense.** The PFR-derived rows are estimated on games with a PFR def row (present
only, `_pct` gated). The five `player_week` rows (`tfl`, `sacks`, `qb_hits`,
`forced_fumbles`, `pass_defended`) were re-estimated 2026-09-30 under the zero reading,
over every game with defense snaps (P7 step 7). Their k basis is unchanged in every group.

| Metric | Group | k | k basis | r | r basis | r_corr (attenuated) | Seasons (pairs) |
|---|---|---|---|---|---|---|---|
| `tackles_per_snap` | DL | 378 | pinned | 0.83 | r_slope | 0.35 | 2018-2025 (1482) |
| `tackles_per_snap` | LB | 93 | pinned | 0.95 | r_slope | 0.71 | 2018-2025 (1335) |
| `tackles_per_snap` | DB | 265 | pinned | 0.86 | r_slope | 0.52 | 2018-2025 (1895) |
| `pressures_per_snap` | DL | 397 | pinned | 0.92 | r_slope | 0.40 | 2018-2025 (1482) |
| `pressures_per_snap` | LB | 84 | pinned | 0.96 | r_slope | 0.74 | 2018-2025 (1335) |
| `pressures_per_snap` | DB | 603 | pinned | 0.87 | r_slope | 0.42 | 2018-2025 (1895) |
| `blitzes_per_snap` | DL | 80 | pinned | 0.79 | r_slope | 0.62 | 2018-2025 (1482) |
| `blitzes_per_snap` | LB | 153 | pinned | 0.65 | r_slope | 0.48 | 2018-2025 (1335) |
| `blitzes_per_snap` | DB | 92 | pinned | 0.64 | r_slope | 0.52 | 2018-2025 (1895) |
| `targets_per_snap` | DL | 303 | pinned | 0.67 | r_slope | 0.32 | 2018-2025 (1482) |
| `targets_per_snap` | LB | 76 | pinned | 0.96 | r_slope | 0.75 | 2018-2025 (1335) |
| `targets_per_snap` | DB | 143 | pinned | 0.87 | r_slope | 0.62 | 2018-2025 (1895) |
| `tfl_per_snap` | DL | 827 | pinned | 0.92 | r_slope | 0.33 | 2013-2025 (2842) |
| `tfl_per_snap` | LB | 859 | pinned | 1.00 | r_slope | 0.39 | 2013-2025 (2467) |
| `tfl_per_snap` | DB | 1504 | J: point k0 (interval too wide) | 0.91 | r_slope (k judgment) | 0.26 | 2013-2025 (3419) |
| `sacks_per_snap` | DL | 595 | pinned | 0.96 | r_slope | 0.41 | 2013-2025 (2842) |
| `sacks_per_snap` | LB | 347 | pinned | 1.00 | r_slope | 0.56 | 2013-2025 (2467) |
| `sacks_per_snap` | DB | 10343 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.23 | 2013-2025 (3419) |
| `qb_hits_per_snap` | DL | 237 | pinned | 0.92 | r_slope | 0.57 | 2013-2025 (2842) |
| `qb_hits_per_snap` | LB | 163 | pinned | 0.99 | r_slope | 0.68 | 2013-2025 (2467) |
| `qb_hits_per_snap` | DB | 1287 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment) | 0.34 | 2013-2025 (3419) |
| `forced_fumbles_per_snap` | DL | 5262 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.20 | 2013-2025 (2842) |
| `forced_fumbles_per_snap` | LB | 5262 | J: no detectable signal, DL point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2013-2025 (0) |
| `forced_fumbles_per_snap` | DB | 5262 | J: no detectable signal, DL point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2013-2025 (0) |
| `pass_defended_per_snap` | DL | 840 | pinned | 0.77 | r_slope | 0.27 | 2013-2025 (2842) |
| `pass_defended_per_snap` | LB | 1980 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment) | 0.27 | 2013-2025 (2467) |
| `pass_defended_per_snap` | DB | 1017 | pinned | 0.94 | r_slope | 0.35 | 2013-2025 (3419) |
| `missed_tackle_rate` | DL | 51 | pinned | 0.56 | r_slope | 0.17 | 2018-2025 (1429) |
| `missed_tackle_rate` | LB | 141 | pinned | 0.72 | r_slope | 0.21 | 2018-2025 (1319) |
| `missed_tackle_rate` | DB | 158 | pinned | 0.91 | r_slope | 0.21 | 2018-2025 (1872) |
| `completion_pct_allowed` | DL | 204 | J: no detectable signal, LB point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2018-2025 (0) |
| `completion_pct_allowed` | LB | 204 | J: point k0 (interval too wide) | 0.42 | r_slope (k judgment) | 0.06 | 2018-2025 (1185) |
| `completion_pct_allowed` | DB | 112 | pinned | 0.70 | r_slope | 0.21 | 2018-2025 (1876) |
| `yards_per_target_allowed` | DL | 10 | J: point k0 (interval too wide) | 0.52 | r_slope (k judgment) | 0.11 | 2018-2025 (468) |
| `yards_per_target_allowed` | LB | 283 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.16 | 2018-2025 (1141) |
| `yards_per_target_allowed` | DB | 104 | pinned | 0.73 | r_slope | 0.19 | 2018-2025 (1821) |
| `yac_allowed_per_completion` | DL | 5.4 | J: point k0 (interval too wide) | 0.34 | r_slope (k judgment) | 0.09 | 2018-2025 (462) |
| `yac_allowed_per_completion` | LB | 275 | J: point k0 (interval too wide) | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.06 | 2018-2025 (1141) |
| `yac_allowed_per_completion` | DB | 163 | J: point k0 (interval too wide) | 1.00 | r_slope (k judgment) | 0.15 | 2018-2025 (1818) |
| `adot_allowed` | DL | 103 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.30 | 2018-2025 (585) |
| `adot_allowed` | LB | 44 | pinned | 0.67 | r_slope | 0.24 | 2018-2025 (1182) |
| `adot_allowed` | DB | 31 | pinned | 0.64 | r_slope | 0.34 | 2018-2025 (1875) |
| `td_rate_allowed` | DL | 5 | J: point k0 (interval too wide) | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.13 | 2018-2025 (468) |
| `td_rate_allowed` | LB | 208 | J: point k0 (interval too wide) | 0.00 | 0: no YoY signal (r_corr reaches 0) | 0.05 | 2018-2025 (1141) |
| `td_rate_allowed` | DB | 171 | pinned | 0.62 | r_slope | 0.11 | 2018-2025 (1821) |
| `int_rate_on_targets` | DL | 1409 | J: no detectable signal, DB point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2018-2025 (0) |
| `int_rate_on_targets` | LB | 1409 | J: no detectable signal, DB point k0 (same units) | 0.00 | 0: no detectable signal | n/a | 2018-2025 (0) |
| `int_rate_on_targets` | DB | 1409 | J: point k0 (interval too wide) | 0.00 | 0: r_slope unstable | 0.25 | 2018-2025 (1876) |

#### Considered, not built
- **FTN `read_thrown`.** The code meanings for `1` and `2` aren't recorded in
  `docs/sources.md`. Verify them against the nflreadr dictionary before any metric
  uses it.
- **PFR drops/bad throws** (`receiving_drop`, `passing_drops`, `passing_bad_throws`,
  staged by `0027`): FTN's per-play `is_drop`/`is_catchable_ball` cover the same ground.
- **NGS CPOE and NGS YAC-oe:** pbp's `cpoe`/`xyac` cover every player, where NGS covers
  only those above its threshold.
- **Passer rating allowed, WOPR, RACR:** composites of metrics already here.
- **Route-based rates** (TPRR, YPRR): no free per-receiver route data. Participation's
  `route` is the primary receiver's only.

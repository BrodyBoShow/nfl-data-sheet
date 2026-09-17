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
- `w_prior = (1 - w_cur) * prior_discount`
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
- **Defense signals always use `prior_discount = 1.0`** — there's no reliable front-
  seven/secondary continuity data staged yet, so a defense's blend shifts weight only via
  `n_cur` growing over the season, not via a personnel discount. **Documented gap**: this
  is a candidate future refinement once such data exists, not solved here.
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
the "Prior blending" section above; garbage time and the explosive-play thresholds are
filtered upstream at collection (`pipeline/collectors/nflverse_bulk.py`'s
`_GARBAGE_TIME_WP_LOW/HIGH`, `_EXPLOSIVE_PASS_YARDS`/`_EXPLOSIVE_RUSH_YARDS`), not
re-filtered here. `sample_n` = the current-season denominator only (prior-season sample
size isn't folded in). Source table for every one of these: `team_week`
(`pipeline/collectors/nflverse_bulk.py`), self-joined on `opponent_team` for the
opponent-adjustment solve. **Added:** Phase 2, 2026.

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

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

Weeks 1–5 of a season are mostly noise. The efficiency analyst blends the current
season's opponent-adjusted values with last season's opponent-adjusted values as a prior,
discounted for:
- starting-QB changes (derived from nflverse depth charts / snap counts / pbp)
- offensive line continuity (derived from nflverse snap counts / pbp)

Coordinator changes are out of scope unless a verified live source exists. Blend weight
shifts toward the current season as weeks accumulate; `stability` reflects the resulting
confidence (higher = less prior-dependent).

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

_(empty — populate as each analyst implements its signals, starting in Phase 2)_

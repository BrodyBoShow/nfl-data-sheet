// Every data source behind something the site displays, with its license (docs/phases/
// P6.md §3 /sources, step 7). Licenses and terms come from docs/sources.md, or were read
// from the source's own page on the date given.
//
// `sourcesFor` maps a displayed signal to the sources behind it. An unmapped signal
// returns null. scripts/check-sources.mjs fails on any live signal it can't map, so a new
// signal can't reach a page without a source entry.

export type SourceId =
  | "nflverse"
  | "pfr"
  | "ngs"
  | "ftn"
  | "odds_api"
  | "open_meteo"
  | "osm"
  | "wikipedia"
  | "iana_tz"
  | "espn"
  | "sleeper";

export interface Source {
  id: SourceId;
  name: string;
  url: string;
  feeds: string; // what on this site comes from it
  license: { name: string; url: string | null };
  terms: string[]; // obligations and how the site meets them
}

export const SOURCES: readonly Source[] = [
  {
    id: "nflverse",
    name: "nflverse (via nflreadpy)",
    url: "https://github.com/nflverse/nflverse-data",
    feeds:
      "The schedule on every page. Every efficiency rating (team play-by-play aggregates, plus the QB " +
      "continuity adjustment from player stats, and the depth charts both continuity adjustments fall back " +
      "on). Surface, roof status for retractable roofs, rest days, and the player positions behind the " +
      "availability counts. In the players section: player names and positions, and every player rate and " +
      "share built from play-by-play and player stats. Snap counts, Next Gen Stats and FTN data come " +
      "through nflverse too, but they're credited to their providers below.",
    license: { name: "CC BY 4.0", url: "https://creativecommons.org/licenses/by/4.0/" },
    terms: ["Attribution required. Credited here and in the site footer."],
  },
  {
    id: "pfr",
    name: "Pro Football Reference (Sports Reference LLC), via nflverse",
    url: "https://www.pro-football-reference.com/",
    feeds:
      "Snap counts. They identify each team's O-line for the O-line continuity adjustment, which reaches every " +
      "offense efficiency rating and, through the opponent adjustment, every defense rating. They also show " +
      "when a player ESPN still lists as out has played since, which clears him from the availability counts. " +
      "In the players section: snap shares, every defensive per-snap rate, and the games and last-4 windows " +
      "(a game played is a game with a snap). Advanced stats: pressures, blitzes, tackles, missed tackles, " +
      "yards before contact, and nearest-defender coverage charting. Each such column is marked PFR.",
    license: { name: "Sports Reference Terms of Use", url: "https://www.sports-reference.com/termsofuse.html" },
    terms: [
      'Terms of Use §5 (page "Last Updated: May 19, 2023", read 2026-09-25): sharing or publishing data ' +
        '"should explicitly credit SRL as the source of the data to the maximum extent possible".',
      "Credited here, in the site footer, and under each block whose values use it.",
      "Fetched only as nflverse's published release files (tag snap_counts), never from Sports Reference's " +
        "own sites.",
      "A takedown request from Sports Reference is honored immediately.",
    ],
  },
  {
    id: "ngs",
    name: "NFL Next Gen Stats, via nflverse",
    url: "https://nextgenstats.nfl.com/",
    feeds:
      "In the players section: receiver separation and quarterback time to throw. Each such column is " +
      "marked NGS.",
    license: { name: "NFL Terms and Conditions", url: "https://www.nfl.com/legal/terms" },
    terms: [
      "Credited here and at the head of every column that uses it.",
      "Fetched only as nflverse's published release files (tag nextgen_stats), never from the NFL's own " +
        "sites. The NFL's terms aren't a grant to this site; a takedown request from the NFL is honored " +
        "immediately.",
      '"Next Gen Stats, Next Generation Stats, NFL and the NFL shield design are registered trademarks of ' +
        'the National Football League." This site isn\'t affiliated with the NFL.',
    ],
  },
  {
    id: "ftn",
    name: "FTN Data, via nflverse",
    url: "https://github.com/nflverse/nflverse-data/releases/tag/pbp_participation",
    feeds:
      "In the players section: the coverage history (each player's EPA and target rate against man and zone " +
      "coverage), from FTN's participation charting of past seasons. Each such column is marked FTN.",
    license: { name: "CC BY-SA 4.0", url: "https://creativecommons.org/licenses/by-sa/4.0/" },
    terms: [
      'nflverse states: "This data is released under the CC-BY-SA 4.0 Creative Commons license and ' +
        'attribution must be made to FTN Data via nflverse (from 2023 onwards)" (load_participation, read ' +
        "2026-09-25). Every season shown here is 2023 or later.",
      "Modified: this site aggregates the play-level charting into per-player rates for each season shown.",
      "The coverage-history values are adapted material, shared under CC BY-SA 4.0 " +
        "(https://creativecommons.org/licenses/by-sa/4.0/). The block that shows them says so.",
      "Provided as is, without warranties of any kind (CC BY-SA 4.0, section 5).",
    ],
  },
  {
    id: "odds_api",
    name: "The Odds API",
    url: "https://the-odds-api.com/",
    feeds:
      "Every market line and the values computed from it: consensus spread and total, movement, book " +
      "range and count, key-number crossings, implied team totals, no-vig win probability.",
    license: { name: "The Odds API terms and conditions", url: "https://the-odds-api.com/terms-and-conditions.html" },
    terms: [
      'Read 2026-09-24 (page "Last updated: 31 August 2026"). Permitted uses include "Calculating and ' +
        'displaying values you derive from our data".',
      "The site shows consensus values and values derived from them. The per-book feed isn't published.",
    ],
  },
  {
    id: "open_meteo",
    name: "Open-Meteo",
    url: "https://open-meteo.com/",
    feeds:
      "Every weather value (temperature, precipitation, snowfall, wind), the forecast lead time and model " +
      "notes, and venue elevation.",
    license: { name: "CC BY 4.0", url: "https://creativecommons.org/licenses/by/4.0/" },
    terms: [
      'Credited as "Weather data by Open-Meteo.com" on every block that shows weather.',
      "The free tier is non-commercial only: no ads, subscriptions or paywall on this site.",
      "Wind is Open-Meteo's exterior 10 m estimate for the grid cell, labeled " +
        '"Outside wind (10 m est.)", never wind at the field.',
    ],
  },
  {
    id: "osm",
    name: "OpenStreetMap",
    url: "https://www.openstreetmap.org/copyright",
    feeds:
      "Stadium coordinates and field bearings, which place each weather request and give travel " +
      "distances and the along-field and crosswind split.",
    license: { name: "ODbL 1.0", url: "https://opendatacommons.org/licenses/odbl/" },
    terms: ["© OpenStreetMap contributors. The game view's environment block links here."],
  },
  {
    id: "wikipedia",
    name: "Wikipedia",
    url: "https://en.wikipedia.org/",
    feeds:
      "Each stadium's roof type (fixed, retractable or open), cited to its article in the pipeline's " +
      "stadium reference. Only that fact is used; no article text appears on the site.",
    license: {
      name: "Article text: CC BY-SA 4.0",
      url: "https://en.wikipedia.org/wiki/Wikipedia:Copyrights",
    },
    terms: ["Credited here."],
  },
  {
    id: "iana_tz",
    name: "IANA time zone database",
    url: "https://www.iana.org/time-zones",
    feeds: "Each stadium's time zone, behind the time-zone shift values.",
    license: { name: "Public domain", url: "https://github.com/eggert/tz/blob/main/LICENSE" },
    terms: ['The tz LICENSE file: the tz code and data "are in the public domain" (read 2026-09-25).'],
  },
  {
    id: "espn",
    name: "ESPN injuries (unofficial endpoint)",
    url: "https://www.espn.com/nfl/injuries",
    feeds: "Injury designations behind the O-line and secondary availability counts.",
    license: { name: "No published terms", url: null },
    terms: ["Used for derived per-team counts only. No injury text or player status is republished."],
  },
  {
    id: "sleeper",
    name: "Sleeper API",
    url: "https://docs.sleeper.com/",
    feeds: "Player positions and IDs used to resolve the injury designations behind the availability counts.",
    license: { name: "Sleeper API terms", url: "https://docs.sleeper.com/" },
    terms: [
      'Free for non-commercial use; "For commercial use of the Sleeper API, please reach out to us ' +
        'directly to discuss licensing." (read 2026-09-25).',
      "Used for derived per-team counts only.",
    ],
  },
];

const WEATHER = [
  "weather_status",
  "temperature_f",
  "apparent_temperature_f",
  "precip_total_in",
  "snowfall_total_in",
  "precip_prob_max_pct",
  "wind_speed_mph",
  "wind_gust_max_mph",
  "wind_direction_mode",
  "wind_along_field_mph",
  "wind_crosswind_mph",
  "weather_lead_hours",
  "weather_model_regime_break",
  "weather_forecast_domain",
  "venue_elevation_m",
];

const ENVIRONMENT: Record<string, SourceId[]> = {
  ...Object.fromEntries(WEATHER.map((s) => [s, ["open_meteo", "osm"] as SourceId[]])),
  venue_roof_code: ["wikipedia", "nflverse"],
  surface_code: ["nflverse"],
  rest_days: ["nflverse"],
  rest_diff: ["nflverse"],
  travel_miles: ["nflverse", "osm"],
  tz_shift_hours: ["nflverse", "iana_tz"],
  tz_offset_diff_raw_hours: ["nflverse", "iana_tz"],
};

/** The sources behind a displayed signal, or null if it isn't mapped. */
export function sourcesFor(sector: string, signal: string): SourceId[] | null {
  switch (sector) {
    case "efficiency":
      return ["nflverse", "pfr"];
    case "market":
      return ["odds_api"];
    case "availability":
      return ["espn", "sleeper", "nflverse", "pfr"];
    case "environment":
      return ENVIRONMENT[signal] ?? null;
    default:
      return null;
  }
}

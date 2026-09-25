// Every data source behind something the site displays, with its license (docs/phases/
// P6.md §3 /sources, step 7). Licenses and terms come from docs/sources.md, or were read
// from the source's own page on the date given.
//
// `sourcesFor` maps a displayed signal to the sources behind it. An unmapped signal
// returns null. scripts/check-sources.mjs fails on any live signal it can't map, so a new
// signal can't reach a page without a source entry.

export type SourceId = "nflverse" | "odds_api" | "open_meteo" | "osm" | "wikipedia" | "iana_tz" | "espn" | "sleeper";

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
      "The schedule on every page. Every efficiency rating (team play-by-play aggregates, plus QB and " +
      "O-line continuity from player stats and snap counts). Surface, roof status for retractable roofs, " +
      "rest days, and the snap and depth-chart inputs to the availability counts.",
    license: { name: "CC BY 4.0", url: "https://creativecommons.org/licenses/by/4.0/" },
    terms: ["Attribution required. Credited here and in the site footer."],
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
      return ["nflverse"];
    case "market":
      return ["odds_api"];
    case "availability":
      return ["espn", "sleeper", "nflverse"];
    case "environment":
      return ENVIRONMENT[signal] ?? null;
    default:
      return null;
  }
}

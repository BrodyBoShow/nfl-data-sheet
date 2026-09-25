import Link from "next/link";

import type { Card } from "@/lib/card";
import { formatFixed } from "@/lib/format";

// Environment block (docs/phases/P6.md §6, context). Weather values only at
// weather_status 1, always with the Open-Meteo credit on the block itself, and wind
// labeled as the outside 10 m estimate (licensing obligations, §0). Travel/timezone are
// stadium-derived, so there's an OSM credit link (via /sources).

const DASH = <span className="null">—</span>;

const WEATHER_STATUS: Record<number, string> = {
  1: "forecast available",
  2: "indoor: weather doesn't apply",
  3: "awaiting a forecast capture",
  4: "missed: no forecast captured",
  5: "venue unresolved",
  6: "not tracked (played before weather collection began)",
};
const ROOF: Record<number, string> = {
  1: "fixed roof",
  2: "retractable, closed",
  3: "retractable (if roof open)",
  4: "open air",
};
const SURFACE: Record<number, string> = { 1: "grass", 2: "artificial turf" };
const WIND_MODE: Record<number, string> = {
  1: "speed only (no hour reaches 8 mph sustained)",
  2: "speed only (no field bearing for this venue)",
  3: "split into along-field and crosswind",
};

export function Environment({ card, kickedOff }: { card: Card; kickedOff: boolean }) {
  const g = card.context.environment.game;
  const teams = card.context.environment.teams;
  const n = (k: string) => g[k] ?? null;
  const status = n("weather_status");
  // A stored 3 after kickoff means "computed before kickoff", not "still awaiting".
  const statusText =
    status === null
      ? null
      : status === 3 && kickedOff
        ? "unknown (last computed before kickoff)"
        : WEATHER_STATUS[status] ?? `code ${status}`;

  const weather: [string, string | null][] =
    status === 1
      ? [
          ["Temperature", n("temperature_f") === null ? null : `${Math.round(n("temperature_f")!)}°F`],
          ["Feels like", n("apparent_temperature_f") === null ? null : `${Math.round(n("apparent_temperature_f")!)}°F`],
          ["Outside wind (10 m est.)", n("wind_speed_mph") === null ? null : `${Math.round(n("wind_speed_mph")!)} mph`],
          ["Gusts, max (10 m est.)", n("wind_gust_max_mph") === null ? null : `${Math.round(n("wind_gust_max_mph")!)} mph`],
          ["Wind direction", n("wind_direction_mode") === null ? null : WIND_MODE[n("wind_direction_mode")!] ?? null],
          ...(n("wind_direction_mode") === 3
            ? ([
                ["Along-field wind (10 m est.)", n("wind_along_field_mph") === null ? null : `${Math.round(n("wind_along_field_mph")!)} mph`],
                ["Crosswind (10 m est.)", n("wind_crosswind_mph") === null ? null : `${Math.round(n("wind_crosswind_mph")!)} mph`],
              ] as [string, string | null][])
            : []),
          ["Precipitation, total", n("precip_total_in") === null ? null : `${formatFixed(n("precip_total_in")!, 2)} in`],
          ["Precipitation chance, max", n("precip_prob_max_pct") === null ? null : `${Math.round(n("precip_prob_max_pct")!)}%`],
          ["Snowfall, total", n("snowfall_total_in") === null ? null : `${formatFixed(n("snowfall_total_in")!, 2)} in`],
          ["Forecast captured", n("weather_lead_hours") === null ? null : `${formatFixed(n("weather_lead_hours")!, 1)} h before kickoff`],
          ...(n("weather_model_regime_break") === 1
            ? ([["Forecast model", "48 h forecast only (different model regime)"]] as [string, string | null][])
            : []),
          ...(n("weather_forecast_domain") === 2
            ? ([["Forecast confidence", "lower: venue outside the HRRR grid"]] as [string, string | null][])
            : []),
          ["Elevation", n("venue_elevation_m") === null ? null : `${Math.round(n("venue_elevation_m")!)} m`],
        ]
      : [];

  const order = [card.identity.away_team, card.identity.home_team];
  const t = (team: string, k: string) => teams[team]?.[k] ?? null;
  const signed = (x: number | null, d: number) => (x === null ? null : `${x > 0 ? "+" : ""}${formatFixed(x, d)}`);

  return (
    <section className="section" aria-labelledby="env-h">
      <h2 id="env-h" className="section-label t-cap">
        Environment · not used by the model
      </h2>
      <p className="t-small">
        {[
          n("venue_roof_code") === null ? null : ROOF[n("venue_roof_code")!],
          n("surface_code") === null ? null : SURFACE[n("surface_code")!],
          statusText,
        ]
          .filter(Boolean)
          .join(" · ") || DASH}
      </p>
      {weather.length ? (
        <>
          <table className="data env-table">
            <tbody>
              {weather.map(([label, value]) => (
                <tr key={label}>
                  <th scope="row">{label}</th>
                  <td className="num">{value ?? DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="legend t-small ink-2">
            Weather data by{" "}
            <a href="https://open-meteo.com/" rel="noopener">
              Open-Meteo.com
            </a>{" "}
            (CC BY 4.0). Wind is an outside 10 m estimate, not wind at the field.
          </p>
        </>
      ) : null}
      <div className="table-scroll">
        <table className="data env-teams-table">
          <thead>
            <tr>
              <th scope="col">Team</th>
              <th scope="col" className="num">Rest days</th>
              <th scope="col" className="num">Rest diff</th>
              <th scope="col" className="num">Travel mi</th>
              <th scope="col" className="num">TZ shift h</th>
            </tr>
          </thead>
          <tbody>
            {order.map((team) => (
              <tr key={team}>
                <th scope="row" className="mono">{team}</th>
                <td className="num">{t(team, "rest_days") === null ? DASH : String(t(team, "rest_days"))}</td>
                <td className="num">{signed(t(team, "rest_diff"), 0) ?? DASH}</td>
                <td className="num">{t(team, "travel_miles") === null ? DASH : String(Math.round(t(team, "travel_miles")!))}</td>
                <td className="num">{signed(t(team, "tz_shift_hours"), 0) ?? DASH}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="legend t-small ink-2">
        Travel and time zones use stadium locations © OpenStreetMap contributors (
        <Link href="/sources">sources</Link>). TZ shift: hours of time-zone change; positive
        means the team traveled east.
      </p>
    </section>
  );
}

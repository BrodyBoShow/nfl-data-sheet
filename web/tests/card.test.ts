import { describe, expect, it } from "vitest";

import { parseCard } from "../lib/card";
import lockedRow from "./fixtures/card_2026_03_ATL_GB.json";
import provisionalRow from "./fixtures/card_2026_03_ARI_SF.json";

const clone = <T>(x: T): T => structuredClone(x);

describe("parseCard: real cards (trimmed fixtures from web.cards)", () => {
  it("parses the locked TNF card and keeps both market lines", () => {
    const r = parseCard(lockedRow.card);
    if (!r.ok) throw new Error(JSON.stringify(r));
    expect(r.card.lock.locked).toBe(true);
    expect(r.card.edge.at_lock?.market_spread).toBe(-6.5);
    expect(r.card.edge.at_lock?.market_total).toBe(44.5);
    expect(r.card.edge.vs_current.market_spread).toBe(-4.5);
    expect(r.card.uncertainty?.stability_bucket).toBe("low");
    expect(r.card.uncertainty?.edge_validated).toEqual({ spread: false, total: false });
  });

  it("parses a provisional card: no lock, no at-lock edge, a locks_from time", () => {
    const r = parseCard(provisionalRow.card);
    if (!r.ok) throw new Error(JSON.stringify(r));
    expect(r.card.lock.locked).toBe(false);
    expect(r.card.edge.at_lock).toBeNull();
    expect(r.card.lock.locks_from).toBe("2026-09-27T14:05:00+00:00");
  });

  it("keeps the model's decomposition terms and the context pairings", () => {
    const r = parseCard(lockedRow.card);
    if (!r.ok) throw new Error(JSON.stringify(r));
    const home = r.card.projection?.decomposition.home;
    expect(home?.terms.map((t) => t.signal)).toEqual(["epa_per_play_off", "epa_per_play_def"]);
    expect(r.card.context.in_model).toBe(false);
    expect(r.card.context.efficiency_pairings.home_offense[0]?.subject.player_id).toBeNull();
  });

  it("accepts a not-projected card (projection and uncertainty null)", () => {
    const raw = clone(provisionalRow.card) as Record<string, unknown>;
    raw.projection_status = 2;
    raw.projection = null;
    raw.uncertainty = null;
    expect(parseCard(raw).ok).toBe(true);
  });
});

describe("parseCard: failure modes never throw", () => {
  it("reports an unknown card_version as unsupported", () => {
    const raw = { ...clone(lockedRow.card), card_version: 2 };
    expect(parseCard(raw)).toEqual({ ok: false, reason: "unsupported_version", version: 2 });
  });

  it("reports a missing card_version and non-objects as unsupported", () => {
    const { card_version: _, ...raw } = clone(lockedRow.card);
    expect(parseCard(raw)).toMatchObject({ ok: false, reason: "unsupported_version" });
    expect(parseCard(null)).toMatchObject({ ok: false, reason: "unsupported_version" });
    expect(parseCard("card")).toMatchObject({ ok: false, reason: "unsupported_version" });
  });

  it("reports a changed field type as invalid, with its path", () => {
    const raw = clone(lockedRow.card) as { projection: { spread_home: unknown } };
    raw.projection.spread_home = "-4.2";
    const r = parseCard(raw);
    expect(r.ok).toBe(false);
    if (r.ok || r.reason !== "invalid") throw new Error("expected invalid");
    expect(r.issues.some((i) => i.startsWith("projection.spread_home"))).toBe(true);
  });

  it("reports a removed block as invalid", () => {
    const raw = clone(lockedRow.card) as Record<string, unknown>;
    delete raw.uncertainty;
    expect(parseCard(raw)).toMatchObject({ ok: false, reason: "invalid" });
  });
});

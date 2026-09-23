"""One-off script: find each stadium's playing-field polygon in OpenStreetMap and derive
the field's centroid (lat/lon), long-axis compass bearing (field_bearing), and size, for
hand review before reference/stadiums.csv is written (docs/phases/P4.md, "Weather design
decisions").

For each stadium_id: Nominatim search (hand-written query hint below -- a lookup aid only,
every result is reviewed) -> the leisure=stadium feature -> Overpass: every
leisure=pitch way inside that stadium's area (falling back to within 150 m of it if the
stadium isn't mapped as an area). Each pitch gets a minimum-area bounding rectangle;
its long side gives length and bearing (0-179 deg, clockwise from true north -- a field is
symmetric end to end, so 0 and 180 are the same axis).

A pick is suggested (length 95-130 m, american_football tag preferred, then closest to
110 m) but is only a suggestion -- a wrong bearing silently swaps headwind and crosswind,
so every row is eyeballed before it goes in the CSV.

Not part of the pipeline; makes live calls to OSM services (Nominatim usage policy:
<= 1 request/second, identifying User-Agent). Writes all candidates to
data/osm_stadium_candidates.json (gitignored) and prints a review table.

Usage:
  uv run python scripts/osm_stadium_fields.py
  uv run python scripts/osm_stadium_fields.py --only GNB00,LON00
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "nfl-data-sheet/0.1 (one-off stadium field lookup; non-commercial)"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "osm_stadium_candidates.json"

# stadium_id -> Nominatim query hint. The 38 stadium_ids on the 2026 schedule plus past
# international FRA00, GER00, SAO00. Hints use the name OSM is most likely to carry
# (e.g. NRG not nflverse's stale "Reliant", Azteca not "Estadio Banorte").
STADIUMS: dict[str, str] = {
    "ATL97": "Mercedes-Benz Stadium, Atlanta",
    "BAL00": "M&T Bank Stadium, Baltimore",
    "BOS00": "Gillette Stadium, Foxborough",
    "BUF00": "Highmark Stadium, Orchard Park",
    "CAR00": "Bank of America Stadium, Charlotte",
    "CHI98": "Soldier Field, Chicago",
    "CIN00": "Paycor Stadium, Cincinnati",
    "CLE00": "Huntington Bank Field, Cleveland",
    "DAL00": "AT&T Stadium, Arlington",
    "DEN00": "Empower Field at Mile High, Denver",
    "DET00": "Ford Field, Detroit",
    "FRA00": "Deutsche Bank Park, Frankfurt",
    "GER00": "Allianz Arena, München",
    "GNB00": "Lambeau Field, Green Bay",
    "HOU00": "NRG Stadium, Houston",
    "IND00": "Lucas Oil Stadium, Indianapolis",
    "JAX00": "EverBank Stadium, Jacksonville",
    "KAN00": "Arrowhead Stadium, Kansas City",
    "LAX01": "SoFi Stadium, Inglewood",
    "LON00": "Wembley Stadium, London",
    "LON02": "Tottenham Hotspur Stadium, London",
    "MAD01": "Estadio Santiago Bernabéu, Madrid",
    "MEL00": "Melbourne Cricket Ground",
    "MEX00": "Estadio Azteca, Ciudad de México",
    "MIA00": "Hard Rock Stadium, Miami Gardens",
    "MIN01": "U.S. Bank Stadium, Minneapolis",
    "MUN01": "Allianz Arena, München",
    "NAS00": "Nissan Stadium, Nashville",
    "NOR00": "Caesars Superdome, New Orleans",
    "NYC01": "MetLife Stadium, East Rutherford",
    "PAR00": "Stade de France, Saint-Denis",
    "PHI00": "Lincoln Financial Field, Philadelphia",
    "PHO00": "State Farm Stadium, Glendale",
    "PIT00": "Acrisure Stadium, Pittsburgh",
    "RIO00": "Maracanã, Rio de Janeiro",
    "SAO00": "Neo Química Arena, São Paulo",
    "SEA00": "Lumen Field, Seattle",
    "SFO01": "Levi's Stadium, Santa Clara",
    "TAM00": "Raymond James Stadium, Tampa",
    "VEG00": "Allegiant Stadium, Las Vegas",
    "WAS00": "Northwest Stadium, Landover",
}

EARTH_M_PER_DEG_LAT = 111_320.0


def nominatim_stadium(client: httpx.Client, query: str) -> dict[str, Any] | None:
    """First leisure=stadium way/relation Nominatim returns for the query, else None."""
    r = client.get(NOMINATIM_URL, params={"q": query, "format": "jsonv2", "limit": 10})
    r.raise_for_status()
    time.sleep(1.1)  # Nominatim usage policy: <= 1 req/s
    for hit in r.json():
        if (
            hit.get("category") == "leisure"
            and hit.get("type") == "stadium"
            and hit.get("osm_type") in ("way", "relation")
        ):
            return hit
    return None


def overpass(client: httpx.Client, query: str) -> list[dict[str, Any]]:
    status = None
    for attempt in range(4):
        r = client.post(OVERPASS_URL, data={"data": query})
        status = r.status_code
        if status in (429, 502, 503, 504):
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        time.sleep(2)
        return list(r.json()["elements"])
    raise RuntimeError(f"Overpass kept failing ({status})")


def pitches_for(client: httpx.Client, osm_type: str, osm_id: int) -> tuple[str, list[dict]]:
    """Pitch ways inside the stadium's area; falls back to within 150 m of it."""
    sel = f"{osm_type}({osm_id})->.s;"
    inside = overpass(
        client,
        f'[out:json][timeout:60];{sel}.s map_to_area->.a;'
        f'way(area.a)["leisure"="pitch"];out tags geom;',
    )
    if inside:
        return "inside", inside
    near = overpass(
        client,
        f'[out:json][timeout:60];{sel}way(around.s:150)["leisure"="pitch"];out tags geom;',
    )
    return "within_150m", near


def _project(pts: list[tuple[float, float]], lat0: float, lon0: float) -> list[tuple[float, float]]:
    kx = EARTH_M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return [((lon - lon0) * kx, (lat - lat0) * EARTH_M_PER_DEG_LAT) for lat, lon in pts]


def _hull(p: list[tuple[float, float]]) -> list[tuple[float, float]]:
    p = sorted(set(p))
    if len(p) < 3:
        return p

    def cross(o, a, b):  # type: ignore[no-untyped-def]
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for q in p:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper: list[tuple[float, float]] = []
    for q in reversed(p):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return lower[:-1] + upper[:-1]


def field_geometry(geom: list[dict[str, float]]) -> dict[str, float] | None:
    """Centroid, min-area-rectangle length/width, long-axis bearing, and fill ratio
    (polygon area / rectangle area -- near 1.0 means the pitch is drawn as a clean
    rectangle, lower means an irregular outline worth a closer look)."""
    pts = [(g["lat"], g["lon"]) for g in geom]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return None
    lat0 = sum(p[0] for p in pts) / len(pts)
    lon0 = sum(p[1] for p in pts) / len(pts)
    xy = _project(pts, lat0, lon0)

    # polygon area + area centroid (shoelace)
    a2 = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1], strict=True):
        c = x1 * y2 - x2 * y1
        a2 += c
        cx += (x1 + x2) * c
        cy += (y1 + y2) * c
    area = abs(a2) / 2
    if area == 0:
        return None
    cx, cy = cx / (3 * a2), cy / (3 * a2)

    hull = _hull(xy)
    best: tuple[float, float, float, float] | None = None  # (area, theta, w_along, w_perp)
    for (x1, y1), (x2, y2) in zip(hull, hull[1:] + hull[:1], strict=True):
        theta = math.atan2(y2 - y1, x2 - x1)
        c, s = math.cos(theta), math.sin(theta)
        us = [x * c + y * s for x, y in hull]
        vs = [-x * s + y * c for x, y in hull]
        w_u, w_v = max(us) - min(us), max(vs) - min(vs)
        if best is None or w_u * w_v < best[0]:
            best = (w_u * w_v, theta, w_u, w_v)
    assert best is not None
    rect_area, theta, w_u, w_v = best
    axis = theta if w_u >= w_v else theta + math.pi / 2  # math angle: from east, CCW
    bearing = (90.0 - math.degrees(axis)) % 180.0  # compass: from north, CW, folded

    kx = EARTH_M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return {
        "lat": round(lat0 + cy / EARTH_M_PER_DEG_LAT, 6),
        "lon": round(lon0 + cx / kx, 6),
        "length_m": round(max(w_u, w_v), 1),
        "width_m": round(min(w_u, w_v), 1),
        "bearing_deg": round(bearing, 1),
        "fill": round(area / rect_area, 3),
    }


def suggest(cands: list[dict[str, Any]]) -> int | None:
    ok = [i for i, c in enumerate(cands) if 95 <= c["length_m"] <= 130]
    if not ok:
        return None

    def key(i: int) -> tuple[int, float]:
        sport = cands[i]["tags"].get("sport", "")
        return (0 if "american_football" in sport else 1, abs(cands[i]["length_m"] - 110))

    return min(ok, key=key)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated stadium_ids")
    args = ap.parse_args()
    ids = args.only.split(",") if args.only else list(STADIUMS)

    results: dict[str, Any] = {}
    stadium_cache: dict[str, Any] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=90) as client:
        for sid in ids:
            q = STADIUMS[sid]
            if q not in stadium_cache:
                st = nominatim_stadium(client, q)
                if st is None:
                    stadium_cache[q] = {"stadium": None, "method": None, "candidates": []}
                else:
                    method, ways = pitches_for(client, st["osm_type"], int(st["osm_id"]))
                    cands = []
                    for w in ways:
                        g = field_geometry(w.get("geometry", []))
                        if g:
                            cands.append({"osm_way_id": w["id"], "tags": w.get("tags", {}), **g})
                    stadium_cache[q] = {
                        "stadium": {
                            "osm_type": st["osm_type"],
                            "osm_id": int(st["osm_id"]),
                            "name": st.get("name"),
                            "display_name": st.get("display_name"),
                        },
                        "method": method,
                        "candidates": cands,
                    }
            entry = dict(stadium_cache[q])
            entry["query"] = q
            entry["suggested"] = suggest(entry["candidates"])
            results[sid] = entry
            print(f"{sid}: {len(entry['candidates'])} pitch candidate(s)", file=sys.stderr)

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    hdr = f"{'id':6} {'osm stadium':34} {'way_id':>11} {'lat':>10} {'lon':>11} {'brg':>6} {'len':>6} {'wid':>5} {'fill':>5} {'sport':18} {'how':11} note"
    print(hdr)
    print("-" * len(hdr))
    for sid, e in results.items():
        name = (e["stadium"] or {}).get("name") or "-- NOT FOUND --"
        cands, pick = e["candidates"], e["suggested"]
        plausible = [c for c in cands if 95 <= c["length_m"] <= 130]
        note = []
        if pick is None:
            note.append("NO PICK")
        if len(plausible) > 1:
            note.append(f"{len(plausible)} plausible")
        rows = [cands[pick]] if pick is not None else []
        if not rows:
            print(f"{sid:6} {name[:34]:34} {'':>11} {'':>10} {'':>11} {'':>6} {'':>6} {'':>5} {'':>5} {'':18} {str(e['method']):11} {'; '.join(note)} ({len(cands)} cands)")
            continue
        c = rows[0]
        print(
            f"{sid:6} {name[:34]:34} {c['osm_way_id']:>11} {c['lat']:>10.5f} {c['lon']:>11.5f} "
            f"{c['bearing_deg']:>6.1f} {c['length_m']:>6.1f} {c['width_m']:>5.1f} {c['fill']:>5.2f} "
            f"{c['tags'].get('sport', '')[:18]:18} {e['method']:11} {'; '.join(note)}"
        )
    print(f"\nall candidates: {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()

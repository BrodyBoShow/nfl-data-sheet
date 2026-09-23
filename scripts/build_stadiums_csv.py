"""One-off script: write reference/stadiums.csv from the OSM lookup output plus the
hand-review decisions below (docs/phases/P4.md, "Weather design decisions").

Numbers are never typed by hand: field centroid/bearing/length come from
data/osm_stadium_candidates.json (scripts/osm_stadium_fields.py), the outline cross-check
axis from data/osm_stadium_outline_axis.json, and stadium-feature centers are fetched live
from Overpass (`out center`). This file only records *decisions* -- which OSM field to
use, whether its bearing is kept, roof type and its citation, and known nflverse names.

Coordinates: the chosen field polygon's centroid when the lookup found it inside the
stadium's area; otherwise the stadium feature's center -- no field, a field outside the
building (roll-out grass trays at PHO00/VEG00), or a field found by the 150 m fallback
because the stadium's area lookup returned nothing (relations at DEN00/RIO00, where the
field centroid and stadium center coincide anyway).

bearing_basis:
  osm_field_agrees             field polygon bearing within a few degrees of the
                               stadium outline's long axis
  osm_field_outline_disagrees  field polygon reviewed and kept; outline check unreliable
                               (near-square building or outline skewed by annexes)
  null_no_field                no usable NFL field polygon in OSM -- bearing left null,
                               never substituted with the building's axis
  null_soccer_pitch_only       international venue mapped only as a soccer pitch; the
                               temporary NFL field isn't necessarily on the same axis
  null_fixed_roof              fixed roof -- weather is never fetched, bearing unused

Usage:
  uv run python scripts/build_stadiums_csv.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from osm_stadium_fields import USER_AGENT, overpass  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "data" / "osm_stadium_candidates.json"
OUTLINES = ROOT / "data" / "osm_stadium_outline_axis.json"
OUT = ROOT / "reference" / "stadiums.csv"
WIKI = "https://en.wikipedia.org/wiki/"

# Reviewed 2026-09-23. field_way: the OSM leisure=pitch way chosen for coords (and bearing,
# when basis keeps it); None = no usable field. names: every stadium name nflverse has
# used for this stadium_id, 2018-2026, confirmed as the same place -- minus known
# mislabels (JAX00's "Tottenham Hotspur Stadium", game 2026_05_PHI_JAX).
# wiki: resolved English Wikipedia title (verified to exist via the MediaWiki API) cited
# for roof_type.
REVIEW: dict[str, dict[str, Any]] = {
    "ATL97": dict(
        name="Mercedes-Benz Stadium",
        names=["Mercedes-Benz Stadium"],
        roof="retractable",
        wiki="Mercedes-Benz Stadium",
        field_way=None,
        basis="null_no_field",
        note="OSM maps the building only, no field polygon.",
    ),
    "BAL00": dict(
        name="M&T Bank Stadium",
        names=["M&T Bank Stadium"],
        roof="open",
        wiki="M&T Bank Stadium",
        field_way=172620322,
        basis="osm_field_outline_disagrees",
        note="Outline axis 90 deg off, but the building is near-square (aspect "
        "1.06) so its axis is meaningless; field polygon is a clean "
        "113.5x52.5 m NFL field and was kept.",
    ),
    "BOS00": dict(
        name="Gillette Stadium",
        names=["Gillette Stadium"],
        roof="open",
        wiki="Gillette Stadium",
        field_way=129176835,
        basis="osm_field_agrees",
        note="",
    ),
    "BUF00": dict(
        name="Highmark Stadium",
        names=["Highmark Stadium", "New Era Field"],
        roof="open",
        wiki="Highmark Stadium",
        field_way=None,
        basis="null_no_field",
        note="KNOWN GAP, fill by hand first: OSM 'Highmark Stadium' is the new "
        "building (outline axis ~169 deg); the only nearby field (way "
        "648483761, 71.5 deg) lies outside it and is the old stadium's -- "
        "82 deg off, would swap headwind/crosswind. Open-air and the most "
        "wind-sensitive venue in the league, so this is where the split "
        "matters most. Also: nflverse kept BUF00 across the move next door, "
        "and 'Highmark Stadium' named both buildings, so neither the ID nor "
        "the name distinguishes old from new.",
    ),
    "CAR00": dict(
        name="Bank of America Stadium",
        names=["Bank of America Stadium"],
        roof="open",
        wiki="Bank of America Stadium",
        field_way=187011790,
        basis="osm_field_agrees",
        note="",
    ),
    "CHI98": dict(
        name="Soldier Field",
        names=["Soldier Field"],
        roof="open",
        wiki="Soldier Field",
        field_way=91535224,
        basis="osm_field_agrees",
        note="",
    ),
    "CIN00": dict(
        name="Paycor Stadium",
        names=["Paul Brown Stadium", "Paycor Stadium"],
        roof="open",
        wiki="Paycor Stadium",
        field_way=32946979,
        basis="osm_field_outline_disagrees",
        note="Outline axis ~20 deg off; building is near-square (aspect 1.12). "
        "Field polygon includes sideline turf (127.5x84.8 m, fill 0.88) but "
        "its long axis is unambiguous; kept.",
    ),
    "CLE00": dict(
        name="Huntington Bank Field",
        names=["FirstEnergy Stadium", "Huntington Bank Field"],
        roof="open",
        wiki="Huntington Bank Field",
        field_way=172658198,
        basis="osm_field_agrees",
        note="",
    ),
    "DAL00": dict(
        name="AT&T Stadium",
        names=["AT&T Stadium"],
        roof="retractable",
        wiki="AT&T Stadium",
        field_way=126705643,
        basis="osm_field_agrees",
        note="Polygon is the whole stadium floor (141.8x98.9 m), not the field "
        "lines; its axis matches the building's.",
    ),
    "DEN00": dict(
        name="Empower Field at Mile High",
        names=["Empower Field at Mile High", "Sports Authority Field at Mile High"],
        roof="open",
        wiki="Empower Field at Mile High",
        field_way=72605220,
        basis="osm_field_agrees",
        note="",
    ),
    "DET00": dict(
        name="Ford Field",
        names=["Ford Field"],
        roof="fixed",
        wiki="Ford Field",
        field_way=None,
        basis="null_fixed_roof",
        note="",
    ),
    "FRA00": dict(
        name="Deutsche Bank Park",
        names=["Deutsche Bank Park"],
        roof="retractable",
        wiki="Waldstadion (Frankfurt)",
        field_way=24833322,
        basis="null_soccer_pitch_only",
        note="Soccer pitch agrees with outline (127 deg) but the NFL field laid "
        "over it isn't mapped.",
    ),
    "GER00": dict(
        name="Allianz Arena",
        names=["Allianz Arena"],
        roof="open",
        wiki="Allianz Arena",
        field_way=123121774,
        basis="null_soccer_pitch_only",
        note="Same venue as MUN01 (same OSM way). Soccer pitch agrees with "
        "outline (165 deg) but the NFL field laid over it isn't mapped.",
    ),
    "GNB00": dict(
        name="Lambeau Field",
        names=["Lambeau Field"],
        roof="open",
        wiki="Lambeau Field",
        field_way=145797338,
        basis="osm_field_outline_disagrees",
        note="Outline axis 118 deg is skewed by the Atrium and other annexes; "
        "field polygon is a clean 113.3x52.4 m NFL field running N-S; kept.",
    ),
    "HOU00": dict(
        name="NRG Stadium",
        names=["NRG Stadium", "Reliant Stadium"],
        roof="retractable",
        wiki="Reliant Stadium",
        field_way=None,
        basis="null_no_field",
        note="OSM maps the building only; the two football fields ~250 m NW "
        "are practice fields, not the stadium field. nflverse's 2026 rows "
        "use the retired name 'Reliant Stadium'.",
    ),
    "IND00": dict(
        name="Lucas Oil Stadium",
        names=["Lucas Oil Stadium"],
        roof="retractable",
        wiki="Lucas Oil Stadium",
        field_way=None,
        basis="null_no_field",
        note="OSM maps the building only (two outlines, 26 vs 2 deg -- neither usable).",
    ),
    "JAX00": dict(
        name="EverBank Stadium",
        names=["EverBank Stadium", "TIAA Bank Stadium"],
        roof="open",
        wiki="EverBank Stadium",
        field_way=172665883,
        basis="osm_field_outline_disagrees",
        note="Outline axis 90 deg off, but building is near-square (aspect "
        "1.05). Field polygon includes sideline turf (124.8x83.5 m) but its "
        "long axis is unambiguous; kept. 'Tottenham Hotspur Stadium' "
        "(2026_05_PHI_JAX) deliberately NOT a known name -- a mislabeled "
        "London game the name guard must catch.",
    ),
    "KAN00": dict(
        name="GEHA Field at Arrowhead Stadium",
        names=["Arrowhead Stadium", "GEHA Field at Arrowhead Stadium"],
        roof="open",
        wiki="Arrowhead Stadium",
        field_way=65960009,
        basis="osm_field_agrees",
        note="",
    ),
    "LAX01": dict(
        name="SoFi Stadium",
        names=["SoFi Stadium"],
        roof="fixed",
        wiki="SoFi Stadium",
        field_way=None,
        basis="null_fixed_roof",
        note="Fixed translucent roof but open sides (Wikipedia: 'an open-air "
        "facility'), so air temperature is outdoor ambient; skipped under "
        "the fixed-roof rule since no rain or direct wind reaches the field.",
    ),
    "LON00": dict(
        name="Wembley Stadium",
        names=["Wembley Stadium"],
        roof="open",
        wiki="Wembley Stadium",
        field_way=116539074,
        basis="null_soccer_pitch_only",
        note="Soccer pitch agrees with outline (~90 deg) but the NFL field laid "
        "over it isn't mapped.",
    ),
    "LON02": dict(
        name="Tottenham Hotspur Stadium",
        names=["Tottenham Hotspur Stadium", "Tottenham Stadium"],
        roof="open",
        wiki="Tottenham Hotspur Stadium",
        field_way=636941979,
        basis="null_soccer_pitch_only",
        note="Soccer pitch agrees with outline (~177 deg); the dedicated NFL "
        "surface under the retractable pitch isn't mapped separately.",
    ),
    "MAD01": dict(
        name="Santiago Bernabéu",
        names=["Bernabeu"],
        roof="retractable",
        wiki="Bernabéu (stadium)",
        field_way=1446479375,
        basis="null_soccer_pitch_only",
        note="Soccer pitch disagreed with outline by 90 deg; NFL field not "
        "mapped. Coords from the pitch (inside the stadium).",
    ),
    "MEL00": dict(
        name="Melbourne Cricket Ground",
        names=["Melbourne Cricket Ground"],
        roof="open",
        wiki="Melbourne Cricket Ground",
        field_way=1043590388,
        basis="null_no_field",
        note="Cricket oval; no NFL field mapped. nflverse wrongly labels roof "
        "'dome'. Coords from the cricket pitch (inside the ground).",
    ),
    "MEX00": dict(
        name="Estadio Banorte",
        names=["Azteca Stadium", "Estadio Banorte"],
        roof="open",
        wiki="Estadio Azteca",
        field_way=118934909,
        basis="null_soccer_pitch_only",
        note="Soccer pitch agrees with outline (~6 deg) but the NFL field laid "
        "over it isn't mapped.",
    ),
    "MIA00": dict(
        name="Hard Rock Stadium",
        names=["Hard Rock Stadium"],
        roof="open",
        wiki="Hard Rock Stadium",
        field_way=171419978,
        basis="osm_field_agrees",
        note="OSM tags it sport=soccer but it's 112.5x52.4 m, an NFL field.",
    ),
    "MIN01": dict(
        name="U.S. Bank Stadium",
        names=["U.S. Bank Stadium"],
        roof="fixed",
        wiki="U.S. Bank Stadium",
        field_way=None,
        basis="null_fixed_roof",
        stadium_feature=("way", 743461508),
        note="Nominatim returned only a light-rail stop; stadium feature found "
        "via Overpass (leisure=stadium within 400 m of it).",
    ),
    "MUN01": dict(
        name="Allianz Arena",
        names=["FC Bayern Munich Stadium"],
        roof="open",
        wiki="Allianz Arena",
        field_way=123121774,
        basis="null_soccer_pitch_only",
        note="Same venue as GER00 (same OSM way). nflverse wrongly labels roof "
        "'dome' -- open over the pitch.",
    ),
    "NAS00": dict(
        name="Nissan Stadium",
        names=["Nissan Stadium"],
        roof="open",
        wiki="Nissan Stadium",
        field_way=172668023,
        basis="osm_field_agrees",
        note="",
    ),
    "NOR00": dict(
        name="Caesars Superdome",
        names=["Caesars Superdome", "Mercedes-Benz Superdome"],
        roof="fixed",
        wiki="Caesars Superdome",
        field_way=None,
        basis="null_fixed_roof",
        note="",
    ),
    "NYC01": dict(
        name="MetLife Stadium",
        names=["MetLife Stadium"],
        roof="open",
        wiki="MetLife Stadium",
        field_way=180559973,
        basis="osm_field_agrees",
        note="OSM tags it sport=soccer but it's 110.1x48.4 m, an NFL field.",
    ),
    "PAR00": dict(
        name="Stade de France",
        names=["Stade de France"],
        roof="open",
        wiki="Stade de France",
        field_way=23608423,
        basis="null_soccer_pitch_only",
        note="Soccer pitch agrees with outline (~169 deg) but the NFL field "
        "laid over it isn't mapped. nflverse wrongly labels roof 'dome'.",
    ),
    "PHI00": dict(
        name="Lincoln Financial Field",
        names=["Lincoln Financial Field"],
        roof="open",
        wiki="Lincoln Financial Field",
        field_way=708050530,
        basis="osm_field_agrees",
        note="Hand-picked: polygon is 130.1 m (incl. sideline turf), just over "
        "the script's 130 m auto-pick cutoff; only candidate.",
    ),
    "PHO00": dict(
        name="State Farm Stadium",
        names=["State Farm Stadium"],
        roof="retractable",
        wiki="State Farm Stadium",
        field_way=130353352,
        basis="osm_field_agrees",
        note="Polygon is the roll-out grass tray in its outdoor position; its "
        "axis matches the building's. Coords from the stadium center "
        "instead, since the tray sits outside.",
    ),
    "PIT00": dict(
        name="Acrisure Stadium",
        names=["Acrisure Stadium", "Heinz Field"],
        roof="open",
        wiki="Acrisure Stadium",
        field_way=172659571,
        basis="osm_field_outline_disagrees",
        note="Outline axis ~13 deg off (open-ended horseshoe bowl skews it); "
        "field polygon includes sideline turf (123.3x86.2 m) but its long "
        "axis is unambiguous; kept.",
    ),
    "RIO00": dict(
        name="Maracanã",
        names=["Maracana Stadium"],
        roof="open",
        wiki="Maracanã Stadium",
        field_way=1361281074,
        basis="null_soccer_pitch_only",
        note="Soccer pitch (way 1361281074) centroid coincides with the stadium "
        "center -- it is the Maracana pitch; found via the 150 m fallback "
        "only because the stadium is an OSM relation whose area lookup "
        "returned nothing. Disagreed with the (elliptical) outline by "
        "58 deg; NFL field not mapped.",
    ),
    "SAO00": dict(
        name="Neo Química Arena",
        names=["Arena Corinthians"],
        roof="open",
        wiki="Arena Corinthians",
        field_way=218667235,
        basis="null_soccer_pitch_only",
        note="Soccer pitch disagreed with outline by 90 deg; NFL field not "
        "mapped. Coords from the pitch (inside the stadium).",
    ),
    "SEA00": dict(
        name="Lumen Field",
        names=["CenturyLink Field", "Lumen Field"],
        roof="open",
        wiki="Lumen Field",
        field_way=163840038,
        basis="osm_field_agrees",
        note="Two overlapping polygons (NFL and soccer), same 0 deg axis; the "
        "american_football one used.",
    ),
    "SFO01": dict(
        name="Levi's Stadium",
        names=["Levi's Stadium"],
        roof="open",
        wiki="Levi's Stadium",
        field_way=357300430,
        basis="osm_field_agrees",
        note="",
    ),
    "TAM00": dict(
        name="Raymond James Stadium",
        names=["Raymond James Stadium"],
        roof="open",
        wiki="Raymond James Stadium",
        field_way=172698720,
        basis="osm_field_agrees",
        note="",
    ),
    "VEG00": dict(
        name="Allegiant Stadium",
        names=["Allegiant Stadium"],
        roof="fixed",
        wiki="Allegiant Stadium",
        field_way=None,
        basis="null_fixed_roof",
        note="",
    ),
    "WAS00": dict(
        name="Northwest Stadium",
        names=["FedExField", "Northwest Stadium"],
        roof="open",
        wiki="Northwest Stadium",
        field_way=612644655,
        basis="osm_field_agrees",
        note="",
    ),
}

# IANA time zone per venue, for the Environment analyst's timezone-crossing signal.
# Added 2026-09-23: entered by city, then cross-checked against Open-Meteo's
# `timezone=auto` answer for each row's stored lat/lon -- all 41 matched. A zone, not a
# fixed UTC offset, so DST (and Arizona's lack of it) resolves per kickoff date.
TZ: dict[str, str] = {
    "ATL97": "America/New_York",
    "BAL00": "America/New_York",
    "BOS00": "America/New_York",
    "BUF00": "America/New_York",
    "CAR00": "America/New_York",
    "CHI98": "America/Chicago",
    "CIN00": "America/New_York",
    "CLE00": "America/New_York",
    "DAL00": "America/Chicago",
    "DEN00": "America/Denver",
    "DET00": "America/Detroit",
    "FRA00": "Europe/Berlin",
    "GER00": "Europe/Berlin",
    "GNB00": "America/Chicago",
    "HOU00": "America/Chicago",
    "IND00": "America/Indiana/Indianapolis",
    "JAX00": "America/New_York",
    "KAN00": "America/Chicago",
    "LAX01": "America/Los_Angeles",
    "LON00": "Europe/London",
    "LON02": "Europe/London",
    "MAD01": "Europe/Madrid",
    "MEL00": "Australia/Melbourne",
    "MEX00": "America/Mexico_City",
    "MIA00": "America/New_York",
    "MIN01": "America/Chicago",
    "MUN01": "Europe/Berlin",
    "NAS00": "America/Chicago",
    "NOR00": "America/Chicago",
    "NYC01": "America/New_York",
    "PAR00": "Europe/Paris",
    "PHI00": "America/New_York",
    "PHO00": "America/Phoenix",
    "PIT00": "America/New_York",
    "RIO00": "America/Sao_Paulo",
    "SAO00": "America/Sao_Paulo",
    "SEA00": "America/Los_Angeles",
    "SFO01": "America/Los_Angeles",
    "TAM00": "America/New_York",
    "VEG00": "America/Los_Angeles",
    "WAS00": "America/New_York",
}

KEEPS_BEARING = {"osm_field_agrees", "osm_field_outline_disagrees"}
COLUMNS = [
    "stadium_id",
    "name",
    "known_names",
    "lat",
    "lon",
    "coord_basis",
    "coord_osm",
    "roof_type",
    "roof_source",
    "field_bearing",
    "field_osm_way_id",
    "field_length_m",
    "bearing_basis",
    "outline_axis_deg",
    "source_note",
    "tz",
]


def stadium_centers(
    client: httpx.Client, feats: set[tuple[str, int]]
) -> dict[tuple[str, int], tuple[float, float]]:
    ways = ",".join(str(i) for t, i in feats if t == "way")
    rels = ",".join(str(i) for t, i in feats if t == "relation")
    q = "[out:json][timeout:90];("
    q += f"way(id:{ways});" if ways else ""
    q += f"relation(id:{rels});" if rels else ""
    q += ");out ids center;"
    return {
        (e["type"], e["id"]): (e["center"]["lat"], e["center"]["lon"]) for e in overpass(client, q)
    }


def main() -> None:
    cands = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    outlines = json.loads(OUTLINES.read_text(encoding="utf-8"))
    assert set(REVIEW) == set(cands), set(REVIEW) ^ set(cands)
    assert set(TZ) == set(REVIEW), set(TZ) ^ set(REVIEW)

    def feature(sid: str) -> tuple[str, int]:
        if "stadium_feature" in REVIEW[sid]:
            return REVIEW[sid]["stadium_feature"]  # type: ignore[no-any-return]
        st = cands[sid]["stadium"]
        return (st["osm_type"], int(st["osm_id"]))

    def field(sid: str) -> dict[str, Any] | None:
        way = REVIEW[sid]["field_way"]
        if way is None:
            return None
        match = [c for c in cands[sid]["candidates"] if c["osm_way_id"] == way]
        assert len(match) == 1, (sid, way)
        return match[0]  # type: ignore[no-any-return]

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120) as client:
        centers = stadium_centers(client, {feature(s) for s in REVIEW})

    rows = []
    for sid, r in sorted(REVIEW.items()):
        f = field(sid)
        field_inside = f is not None and cands[sid]["method"] == "inside"
        if field_inside:
            assert f is not None
            lat, lon = f["lat"], f["lon"]
            coord_basis, coord_osm = "osm_field_centroid", f"way/{f['osm_way_id']}"
        else:
            t, i = feature(sid)
            lat, lon = centers[(t, i)]
            coord_basis, coord_osm = "osm_stadium_center", f"{t}/{i}"
        keep = r["basis"] in KEEPS_BEARING
        assert keep == (f is not None and r["basis"] in KEEPS_BEARING)
        ax = outlines.get(sid)
        rows.append(
            {
                "stadium_id": sid,
                "name": r["name"],
                "known_names": "|".join(r["names"]),
                "lat": f"{lat:.5f}",
                "lon": f"{lon:.5f}",
                "coord_basis": coord_basis,
                "coord_osm": coord_osm,
                "roof_type": r["roof"],
                "roof_source": WIKI + r["wiki"].replace(" ", "_"),
                "field_bearing": f"{f['bearing_deg']:.1f}" if keep and f else "",
                "field_osm_way_id": str(f["osm_way_id"]) if keep and f else "",
                "field_length_m": f"{f['length_m']:.1f}" if keep and f else "",
                "bearing_basis": r["basis"],
                "outline_axis_deg": f"{ax[0]:.1f}" if ax else "",
                "source_note": r["note"],
                "tz": TZ[sid],
            }
        )

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()

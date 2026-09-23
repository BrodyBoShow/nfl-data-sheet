import csv
import io

import pytest

from pipeline.collectors.stadiums import CSV_PATH, StadiumsCollector


def _csv_text() -> str:
    return CSV_PATH.read_text(encoding="utf-8")


def _rewrite(mutate) -> str:
    """The real CSV with one row mutated, for negative tests."""
    rows = list(csv.DictReader(io.StringIO(_csv_text())))
    mutate(rows)
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def test_real_csv_validates():
    validated = StadiumsCollector().validate(_csv_text())
    rows = {r.stadium_id: r for r in validated["rows"]}
    assert len(rows) == 41
    assert rows["GNB00"].field_bearing == pytest.approx(179.8)
    assert rows["GNB00"].roof_type == "open"
    assert rows["HOU00"].known_names == ["NRG Stadium", "Reliant Stadium"]


def test_real_csv_excludes_the_known_mislabel_from_jax_names():
    rows = {r.stadium_id: r for r in StadiumsCollector().validate(_csv_text())["rows"]}
    assert "Tottenham Hotspur Stadium" not in rows["JAX00"].known_names
    assert "Tottenham Hotspur Stadium" in rows["LON02"].known_names


def test_real_csv_nulls_every_bearing_it_did_not_review():
    rows = StadiumsCollector().validate(_csv_text())["rows"]
    for r in rows:
        if r.bearing_basis.startswith("null_"):
            assert r.field_bearing is None, r.stadium_id
    assert {r.stadium_id for r in rows if r.bearing_basis == "null_soccer_pitch_only"} >= {
        "LON00", "LON02", "MAD01", "MUN01", "PAR00", "MEX00", "RIO00", "SAO00", "FRA00", "GER00",
    }  # fmt: skip


def test_bearing_without_reviewed_basis_is_rejected():
    def mutate(rows):
        row = next(r for r in rows if r["stadium_id"] == "BUF00")
        row["field_bearing"] = "71.5"
        row["field_osm_way_id"] = "648483761"

    with pytest.raises(ValueError, match="BUF00"):
        StadiumsCollector().validate(_rewrite(mutate))


def test_reviewed_basis_without_bearing_is_rejected():
    def mutate(rows):
        next(r for r in rows if r["stadium_id"] == "GNB00")["field_bearing"] = ""

    with pytest.raises(ValueError, match="GNB00"):
        StadiumsCollector().validate(_rewrite(mutate))


def test_bearing_of_180_is_rejected_since_the_axis_folds_to_0():
    def mutate(rows):
        next(r for r in rows if r["stadium_id"] == "GNB00")["field_bearing"] = "180"

    with pytest.raises(ValueError, match="GNB00"):
        StadiumsCollector().validate(_rewrite(mutate))


def test_unknown_roof_type_is_rejected():
    def mutate(rows):
        next(r for r in rows if r["stadium_id"] == "GNB00")["roof_type"] = "dome"

    with pytest.raises(ValueError):
        StadiumsCollector().validate(_rewrite(mutate))


def test_duplicate_stadium_id_is_rejected():
    def mutate(rows):
        rows.append(dict(rows[0]))

    with pytest.raises(ValueError, match="duplicate"):
        StadiumsCollector().validate(_rewrite(mutate))


def test_changed_columns_are_rejected():
    text = _csv_text().replace("field_bearing", "bearing", 1)
    with pytest.raises(ValueError, match="columns"):
        StadiumsCollector().validate(text)

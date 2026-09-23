"""
Job: Load the hand-reviewed stadium reference CSV into the stadiums table.
Reads: reference/stadiums.csv (checked in; built by scripts/build_stadiums_csv.py)
Writes: stadiums
Tier: T3
Phase: P4
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

from pipeline.core.base import Collector, RunContext, WorkResult
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.freshness import get_last_value, set_last_value
from pipeline.core.hashing import hash_row

CSV_PATH = Path(__file__).resolve().parents[2] / "reference" / "stadiums.csv"
_FRESHNESS_KEY = "stadiums_csv"

_BEARING_KEPT = {"osm_field_agrees", "osm_field_outline_disagrees"}

_COLS = [
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
]


def _blank_to_none(v: Any) -> Any:
    return None if v == "" else v


class StadiumRow(BaseModel):
    """One CSV row. Mirrors 0018_stadiums.sql's CHECKs so a bad edit to the CSV fails
    here, loudly, instead of as a partial upsert."""

    stadium_id: str
    name: str
    known_names: list[str]
    lat: float
    lon: float
    coord_basis: Literal["osm_field_centroid", "osm_stadium_center"]
    coord_osm: str
    roof_type: Literal["fixed", "retractable", "open"]
    roof_source: str
    field_bearing: float | None
    field_osm_way_id: int | None
    field_length_m: float | None
    bearing_basis: Literal[
        "osm_field_agrees",
        "osm_field_outline_disagrees",
        "null_no_field",
        "null_soccer_pitch_only",
        "null_fixed_roof",
    ]
    outline_axis_deg: float | None
    source_note: str

    @field_validator(
        "field_bearing", "field_osm_way_id", "field_length_m", "outline_axis_deg", mode="before"
    )
    @classmethod
    def _blank(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @field_validator("known_names", mode="before")
    @classmethod
    def _split_names(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [n for n in v.split("|") if n]
        return v

    @model_validator(mode="after")
    def _check(self) -> StadiumRow:
        if not self.known_names:
            raise ValueError(f"{self.stadium_id}: known_names is empty")
        if not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise ValueError(f"{self.stadium_id}: lat/lon out of range")
        if self.field_bearing is not None and not (0 <= self.field_bearing < 180):
            raise ValueError(f"{self.stadium_id}: field_bearing must be in [0, 180)")
        has_bearing = self.field_bearing is not None and self.field_osm_way_id is not None
        if (self.bearing_basis in _BEARING_KEPT) != has_bearing:
            raise ValueError(
                f"{self.stadium_id}: bearing_basis {self.bearing_basis!r} doesn't match "
                f"field_bearing/field_osm_way_id presence"
            )
        return self


def _file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StadiumsCollector(Collector):
    name = "stadiums"

    def __init__(self, csv_path: Path = CSV_PATH) -> None:
        self._csv_path = csv_path

    def should_run(self, ctx: RunContext) -> bool:
        # The CSV's own content hash is its change marker -- it only changes when someone
        # commits a reviewed edit, so every other tick is a cheap local no-op.
        text = self._csv_path.read_text(encoding="utf-8")
        return get_last_value(ctx.conn, _FRESHNESS_KEY) != _file_hash(text)

    def fetch(self, ctx: RunContext) -> str:
        return self._csv_path.read_text(encoding="utf-8")

    def validate(self, raw: str) -> dict[str, Any]:
        reader = csv.DictReader(io.StringIO(raw))
        if reader.fieldnames != _COLS:
            raise ValueError(f"stadiums.csv columns {reader.fieldnames} != expected {_COLS}")
        rows = [StadiumRow.model_validate(r) for r in reader]
        ids = [r.stadium_id for r in rows]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate stadium_id in stadiums.csv: {dupes}")
        return {"rows": rows, "file_hash": _file_hash(raw)}

    def store(self, ctx: RunContext, validated: dict[str, Any]) -> WorkResult:
        hash_fields = [c for c in _COLS if c != "stadium_id"]
        rows = []
        for r in validated["rows"]:
            row = r.model_dump()
            row["content_hash"] = hash_row({k: row[k] for k in hash_fields})
            row["updated_at"] = ctx.now
            rows.append(row)

        changed = filter_changed(ctx.conn, "stadiums", "stadium_id", rows)
        written = upsert_rows(
            ctx.conn,
            "stadiums",
            changed,
            conflict_cols=["stadium_id"],
            update_cols=hash_fields + ["content_hash", "updated_at"],
        )
        # No deletes: a stadium dropped from the CSV may still be referenced by
        # weather_snapshots (FK). Removing a venue is a deliberate manual step.
        set_last_value(ctx.conn, _FRESHNESS_KEY, validated["file_hash"])
        return WorkResult(written, meta={"csv_rows": len(rows)})

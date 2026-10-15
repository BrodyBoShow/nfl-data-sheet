"""
Job: Mirror the allow-listed clubs' official RSS feeds into club_rss_items, headline and
     link only, each row attributed to the feed it came from.
Reads: https://www.<club domain>/rss/news for each club in CLUB_FEEDS (docs/sources.md,
       Intel; allow-list per docs/phases/P8.md D16)
Writes: club_rss_items
Tier: T1
Phase: P8
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from pipeline.core.base import Collector, RunContext, WorkResult
from pipeline.core.db import delete_rows, upsert_changed
from pipeline.core.hashing import hash_row

# D16 (user, 2026-10-15): only clubs whose site terms carry the Packers §6 Modular
# Content section with the same permission and the same conditions; differing wording
# cosmetic or more permissive. 7 verbatim + 11 reworded. Excluded: Cardinals and Lions
# (an added "expressly authorized ... in each case" condition), 10 with no such section,
# 2 with no website terms page. Domains are each club's own site, from nfl.com/teams
# (docs/sources.md, "Team RSS → every club's terms page").
#
# Takedown (D4, within 24 hours): remove the club here and push. The next run deletes its
# rows (store's allow-list delete), and the /web build reads this same list.
CLUB_FEEDS: dict[str, str] = {
    # verbatim
    "GB": "packers.com",
    "DAL": "dallascowboys.com",
    "DEN": "denverbroncos.com",
    "NO": "neworleanssaints.com",
    "PIT": "steelers.com",
    "PHI": "philadelphiaeagles.com",
    "SEA": "seahawks.com",
    # reworded, same permission and conditions
    "SF": "49ers.com",
    "BAL": "baltimoreravens.com",
    "LAC": "chargers.com",
    "CHI": "chicagobears.com",
    "NYG": "giants.com",
    "HOU": "houstontexans.com",
    "CAR": "panthers.com",
    "LV": "raiders.com",
    "LA": "therams.com",
    "MIN": "vikings.com",
    "KC": "chiefs.com",
}

_USER_AGENT = "nfl-data-sheet (+https://github.com/BrodyBoShow/nfl-data-sheet)"
_TIMEOUT_S = 20.0
_PAUSE_S = 1.0  # sequential, one club at a time
_MAX_BYTES = 5_000_000  # feeds measured 2026-10-12 hold at most 100 items of ~1 KB

_HASH_FIELDS = ("title", "published_at", "feed_url")
_UPDATE_COLS = [*_HASH_FIELDS, "content_hash", "updated_at"]


def feed_url(domain: str) -> str:
    return f"https://www.{domain}/rss/news"


@dataclass
class FeedResult:
    """One club's parsed feed. `ok` False means the club is skipped this run: its stored
    rows stay, and `reason` goes to meta (D15: an empty feed is a failed fetch)."""

    team: str
    feed_url: str
    ok: bool
    reason: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def _parse_pubdate(text: str | None) -> datetime | None:
    """RFC 822 `<pubDate>` as UTC. None when missing, unparseable, or without a zone:
    a time that can't be placed is left NULL, never filled."""
    if not text or not text.strip():
        return None
    try:
        parsed = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def parse_feed(team: str, url: str, body: bytes) -> FeedResult:
    """RSS 2.0 → the rows to mirror. Never raises for one club's bad feed.

    An item is kept only with a non-blank `<title>` and an http(s) `<link>`, since a row
    without both can't be shown as headline + link (D4). The title is stored exactly as
    published (D4: never edited). Nothing else in the item is read (D5, D12)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return FeedResult(team, url, ok=False, reason=f"invalid_xml: {exc}")
    channel = root.find("channel")
    if root.tag != "rss" or root.get("version") != "2.0" or channel is None:
        return FeedResult(team, url, ok=False, reason=f"not_rss_2.0: root <{root.tag}>")

    raw_items = channel.findall("item")
    counts = {"feed_items": len(raw_items), "skipped_no_title": 0, "skipped_bad_link": 0,
              "published_at_null": 0}
    if not raw_items:
        return FeedResult(team, url, ok=False, reason="empty_feed", counts=counts)

    items = []
    for item in raw_items:
        title = item.findtext("title")
        link = (item.findtext("link") or "").strip()
        if title is None or not title.strip():
            counts["skipped_no_title"] += 1
            continue
        if not link.startswith(("https://", "http://")):
            counts["skipped_bad_link"] += 1
            continue
        published_at = _parse_pubdate(item.findtext("pubDate"))
        if published_at is None:
            counts["published_at_null"] += 1
        items.append({"team": team, "link": link, "title": title,
                      "published_at": published_at, "feed_url": url})
    if not items:
        # Items were there, but none could be shown. Mirroring that would delete the
        # club's rows over a format change, the same blip D15 guards against.
        return FeedResult(team, url, ok=False, reason="no_usable_items", counts=counts)
    return FeedResult(team, url, ok=True, items=items, counts=counts)


class ClubRssCollector(Collector):
    name = "club_rss"

    def __init__(self, feeds: dict[str, str] | None = None, pause_s: float = _PAUSE_S) -> None:
        self._feeds = dict(CLUB_FEEDS if feeds is None else feeds)
        self._pause_s = pause_s

    def should_run(self, ctx: RunContext) -> bool:
        # No change marker was verified for these feeds (only `Cache-Control: max-age=5`,
        # docs/sources.md). The hash diff in store makes an unchanged poll write 0 rows.
        return True

    def fetch(self, ctx: RunContext) -> dict[str, dict[str, Any]]:
        """One GET per club, sequential. A club's failure is recorded, never raised."""
        out: dict[str, dict[str, Any]] = {}
        with httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT_S,
                          follow_redirects=False) as client:
            for i, (team, domain) in enumerate(sorted(self._feeds.items())):
                if i and self._pause_s:
                    time.sleep(self._pause_s)
                url = feed_url(domain)
                try:
                    resp = client.get(url)
                except httpx.HTTPError as exc:
                    out[team] = {"url": url, "status": None, "body": None,
                                 "error": f"{type(exc).__name__}: {exc}"}
                    continue
                body = resp.content if len(resp.content) <= _MAX_BYTES else None
                error = None
                if resp.status_code != 200:
                    error = f"http_{resp.status_code}"
                elif body is None:
                    error = f"too_large: {len(resp.content)} bytes"
                out[team] = {"url": url, "status": resp.status_code, "body": body,
                             "error": error}
        return out

    def validate(self, raw: dict[str, dict[str, Any]]) -> list[FeedResult]:
        results = []
        for team, r in sorted(raw.items()):
            if r["error"] is not None:
                results.append(FeedResult(team, r["url"], ok=False, reason=r["error"]))
            else:
                results.append(parse_feed(team, r["url"], r["body"]))
        if results and not any(r.ok for r in results):
            # Every club failed: likely ours (network, a shared CMS change), not theirs.
            # Fail the run so it's logged as failed, and keep every stored row.
            raise RuntimeError(
                "club_rss: every feed failed: "
                + "; ".join(f"{r.team} {r.reason}" for r in results)
            )
        return results

    def store(self, ctx: RunContext, validated: list[FeedResult]) -> WorkResult:
        rows = []
        for r in validated:
            for item in r.items:
                row = dict(item)
                row["content_hash"] = hash_row({k: row[k] for k in _HASH_FIELDS})
                row["updated_at"] = ctx.now
                rows.append(row)
        upserted = upsert_changed(ctx.conn, "club_rss_items", rows, ["team", "link"],
                                  _UPDATE_COLS)

        # D8 mirror: an ok club's rows become exactly its feed's items. A failed club is
        # skipped (D15), so its rows stay.
        mirror_deleted = 0
        for r in validated:
            if r.ok:
                mirror_deleted += delete_rows(
                    ctx.conn, "club_rss_items", "team = %s AND link <> ALL(%s)",
                    (r.team, [i["link"] for i in r.items]),
                )
        # Takedown path (D4): a club no longer on the allow-list loses its rows.
        takedown_deleted = delete_rows(
            ctx.conn, "club_rss_items", "team <> ALL(%s)", (sorted(self._feeds),)
        )

        clubs = {
            r.team: {"ok": r.ok, "reason": r.reason, "kept_items": len(r.items), **r.counts}
            for r in validated
        }
        meta = {
            "clubs": clubs,
            "clubs_ok": sum(r.ok for r in validated),
            "clubs_failed": sorted(r.team for r in validated if not r.ok),
            "upserts": {"club_rss_items": upserted.meta()},
            "mirror_deleted": mirror_deleted,
            "takedown_deleted": takedown_deleted,
        }
        return WorkResult(upserted.rows_changed + mirror_deleted + takedown_deleted, meta)

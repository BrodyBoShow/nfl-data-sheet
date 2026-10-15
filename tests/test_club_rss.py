import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import pipeline.collectors.club_rss as club_rss
from pipeline.collectors.club_rss import (
    CLUB_FEEDS,
    ClubRssCollector,
    FeedResult,
    feed_url,
    parse_feed,
)
from pipeline.core.base import RunContext
from pipeline.core.db import ChangedUpsert

FIXTURE = Path(__file__).parent / "fixtures" / "team_rss_packers_news_sample.xml"
GB_URL = "https://www.packers.com/rss/news"


def _fixture() -> bytes:
    return FIXTURE.read_bytes()


def _feed(items_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel>'
        f"<title>News</title>{items_xml}</channel></rss>"
    ).encode()


def _item(title="T", link="https://www.packers.com/news/a", pub="Mon, 12 Oct 2026 21:40:27 GMT"):
    pub_xml = "" if pub is None else f"<pubDate>{pub}</pubDate>"
    return f"<item><title><![CDATA[{title}]]></title><link>{link}</link>{pub_xml}</item>"


# ---- the allow-list (D16) ------------------------------------------------------------


def test_allow_list_is_d16s_18_clubs():
    verbatim = {"GB", "DAL", "DEN", "NO", "PIT", "PHI", "SEA"}
    reworded = {"SF", "BAL", "LAC", "CHI", "NYG", "HOU", "CAR", "LV", "LA", "MIN", "KC"}
    assert set(CLUB_FEEDS) == verbatim | reworded
    assert len(CLUB_FEEDS) == 18
    # The added-condition pair and the no-section clubs are out under every reading.
    assert not {"ARI", "DET", "CIN", "TB", "CLE", "IND", "WAS", "MIA", "NYJ", "NE", "TEN",
                "BUF", "ATL", "JAX"} & set(CLUB_FEEDS)


def test_feed_urls_are_the_verified_path():
    assert feed_url(CLUB_FEEDS["GB"]) == GB_URL
    for domain in CLUB_FEEDS.values():
        assert re.fullmatch(r"https://www\.[a-z0-9]+\.com/rss/news", feed_url(domain))


def test_no_forbidden_word_in_any_identifier():
    # D11: table, column and view names, not just labels.
    names = ["club_rss_items", "team", "link", "title", "published_at", "feed_url",
             "content_hash", "updated_at", "club_rss"]
    for word in ("trending", "buzz", "breakout", "hot", "news", "signal", "momentum"):
        assert not [n for n in names if word in n], word


# ---- parsing the real fixture --------------------------------------------------------


def test_real_fixture_parses_every_item_as_published():
    r = parse_feed("GB", GB_URL, _fixture())
    assert r.ok and r.reason is None
    assert r.counts["feed_items"] == 6 and len(r.items) == 6
    first = r.items[0]
    # Byte for byte as published (D4), from the fixture's CDATA.
    assert first["title"] == (
        "Packers, WIAA announce September high school football officiating 'Crew of the Month'"
    )
    assert first["link"] == (
        "https://www.packers.com/news/packers-wiaa-announce-september-high-school-football-"
        "officiating-crew-of-the-month-2026"
    )
    assert first["published_at"] == datetime(2026, 10, 12, 21, 40, 27, tzinfo=UTC)
    # The row's attribution comes from the feed it was read from (D5), never the item.
    assert {i["team"] for i in r.items} == {"GB"}
    assert {i["feed_url"] for i in r.items} == {GB_URL}
    # Only headline, link and time are kept: no description, image or keywords (D4, D12).
    assert set(first) == {"team", "link", "title", "published_at", "feed_url"}


# ---- a failed club keeps its rows (D15) ----------------------------------------------


def test_empty_feed_is_a_failed_fetch():
    r = parse_feed("GB", GB_URL, _feed(""))
    assert not r.ok and r.reason == "empty_feed" and r.items == []


def test_invalid_xml_is_a_failed_fetch():
    r = parse_feed("GB", GB_URL, b"<rss><channel><item>")
    assert not r.ok and (r.reason or "").startswith("invalid_xml")


def test_non_rss_document_is_a_failed_fetch():
    r = parse_feed("GB", GB_URL, b"<html><body>maintenance</body></html>")
    assert not r.ok and (r.reason or "").startswith("not_rss_2.0")


def test_items_that_cannot_be_shown_are_skipped_and_counted():
    r = parse_feed(
        "GB",
        GB_URL,
        _feed(
            _item(title="Kept")
            + _item(title="   ", link="https://www.packers.com/news/b")
            + _item(title="Bad link", link="javascript:alert(1)")
        ),
    )
    assert r.ok and [i["title"] for i in r.items] == ["Kept"]
    assert r.counts["skipped_no_title"] == 1 and r.counts["skipped_bad_link"] == 1


def test_feed_with_no_usable_item_is_a_failed_fetch():
    r = parse_feed("GB", GB_URL, _feed(_item(link="ftp://x/y")))
    assert not r.ok and r.reason == "no_usable_items"


@pytest.mark.parametrize("pub", [None, "", "not a date", "Mon, 12 Oct 2026 21:40:27"])
def test_unplaceable_pubdate_is_null_never_filled(pub):
    r = parse_feed("GB", GB_URL, _feed(_item(pub=pub)))
    assert r.ok and r.items[0]["published_at"] is None
    assert r.counts["published_at_null"] == 1


def test_offset_pubdate_is_stored_in_utc():
    r = parse_feed("GB", GB_URL, _feed(_item(pub="Mon, 12 Oct 2026 17:40:27 -0400")))
    assert r.items[0]["published_at"] == datetime(2026, 10, 12, 21, 40, 27, tzinfo=UTC)


def test_validate_fails_the_run_only_when_every_club_failed():
    collector = ClubRssCollector(feeds={"GB": "packers.com", "SEA": "seahawks.com"})
    ok = {"url": GB_URL, "status": 200, "body": _fixture(), "error": None}
    bad = {"url": "https://www.seahawks.com/rss/news", "status": 503, "body": None,
           "error": "http_503"}
    results = collector.validate({"GB": ok, "SEA": bad})
    assert [(r.team, r.ok, r.reason) for r in results] == [
        ("GB", True, None), ("SEA", False, "http_503")]
    with pytest.raises(RuntimeError, match="every feed failed"):
        collector.validate({"SEA": bad})


# ---- store: mirror, takedown, counts -------------------------------------------------


def _Ctx() -> RunContext:
    now = datetime(2026, 10, 15, 23, 0, tzinfo=UTC)
    return RunContext(2026, 6, "REG", now, settings=None, conn=None)  # type: ignore[arg-type]


@pytest.fixture
def captured(monkeypatch):
    calls: dict[str, list] = {"upsert": [], "delete": []}

    def fake_upsert(conn, table, rows, pk_cols, update_cols, **kw):
        calls["upsert"].append((table, list(rows), pk_cols, update_cols))
        return ChangedUpsert(inserted=len(rows), updated=0, duplicates_dropped=0)

    def fake_delete(conn, table, where, params):
        calls["delete"].append((table, where, tuple(params)))
        return 2

    monkeypatch.setattr(club_rss, "upsert_changed", fake_upsert)
    monkeypatch.setattr(club_rss, "delete_rows", fake_delete)
    return calls


def test_store_mirrors_ok_clubs_keeps_failed_ones_and_applies_the_allow_list(captured):
    gb = parse_feed("GB", GB_URL, _fixture())
    sea = FeedResult("SEA", "https://www.seahawks.com/rss/news", ok=False, reason="http_503")
    collector = ClubRssCollector(feeds={"GB": "packers.com", "SEA": "seahawks.com"})
    result = collector.store(_Ctx(), [gb, sea])

    (table, rows, pk, update_cols), = captured["upsert"]
    assert table == "club_rss_items" and pk == ["team", "link"] and len(rows) == 6
    assert "content_hash" in update_cols and "team" not in update_cols

    mirror = [d for d in captured["delete"] if d[1].startswith("team = %s")]
    assert [(d[2][0], len(d[2][1])) for d in mirror] == [("GB", 6)]  # SEA failed: no delete
    takedown = [d for d in captured["delete"] if d[1] == "team <> ALL(%s)"]
    assert takedown == [("club_rss_items", "team <> ALL(%s)", (["GB", "SEA"],))]

    assert result.rows_written == 6 + 2 + 2
    assert result.meta["clubs_failed"] == ["SEA"] and result.meta["clubs_ok"] == 1
    assert result.meta["clubs"]["SEA"]["reason"] == "http_503"


def test_content_hash_moves_with_the_title_and_not_otherwise(captured):
    collector = ClubRssCollector(feeds={"GB": "packers.com"})
    a = parse_feed("GB", GB_URL, _feed(_item(title="One")))
    b = parse_feed("GB", GB_URL, _feed(_item(title="One")))
    c = parse_feed("GB", GB_URL, _feed(_item(title="One, edited by the club")))
    for r in (a, b, c):
        collector.store(_Ctx(), [r])
    hashes = [call[1][0]["content_hash"] for call in captured["upsert"]]
    assert hashes[0] == hashes[1] != hashes[2]

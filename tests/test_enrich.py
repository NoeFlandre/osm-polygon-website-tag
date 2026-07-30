"""Transactional, resumable polygon-shard text enrichment."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.enrich import enrich_polygon_shard
from osm_polygon_website_tag.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
)
from osm_polygon_website_tag.text_extract import TextExtraction
from osm_polygon_website_tag.web_fetch import FetchResult


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _legacy_row(
    *,
    polygon_id: str = "source:way/1",
    website: str | None = "https://example.org",
    contact: str | None = "https://contact.example.org",
) -> dict[str, object]:
    return {
        "polygon_id": polygon_id,
        "region": "source",
        "source_pbf": "source.osm.pbf",
        "osm_type": "way",
        "osm_id": int(polygon_id.rsplit("/", 1)[1]),
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "name": None,
        "website": website,
        "contact_website": contact,
        "has_website": website is not None,
        "has_contact_website": contact is not None,
        "has_any_website": True,
        "website_class": "absolute_url" if website else None,
        "contact_website_class": "absolute_url" if contact else None,
        "website_hostname": "example.org" if website else None,
        "contact_website_hostname": "contact.example.org" if contact else None,
        "preferred_website": website or contact,
        "preferred_website_source": "website" if website else "contact:website",
        "wikidata": None,
        "wikidata_qid": None,
        "wikidata_class": None,
        "tags": json.dumps({"website": website, "contact:website": contact}),
        "tag_keys": '["contact:website","website"]',
        "tag_count": 2,
        "osm_primary_tag": "building",
        "geometry": '{"type":"Polygon","coordinates":[]}',
        "centroid": '{"type":"Point","coordinates":[0,0]}',
        "centroid_kind": "lambert_azimuthal_equal_area",
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0,0,0,0]",
        "area_m2": 1.0,
        "area_km2": 0.000001,
        "area_bucket": "<10m2",
        "schema_version": "v1.1",
    }


def _write_legacy(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA_V1_1), path)


def _extract(html: bytes, *, url: str) -> TextExtraction:
    text = html.decode()
    return TextExtraction("success", text, len(text.split()), None, "2.1.0")


def test_legacy_shard_migrates_both_tags_without_pbf_access(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(shard, [_legacy_row()])
    fetched: list[str] = []

    def fetch(url: str) -> FetchResult:
        fetched.append(url)
        return FetchResult("ok", url, final_url=url, body=f"text from {url}".encode())

    result = enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetch,
        extractor=_extract,
    )

    row = pq.read_table(shard).to_pylist()[0]
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    assert row["schema_version"] == "v1.2"
    assert row["website_text"] == "text from https://example.org"
    assert row["contact_website_text"] == "text from https://contact.example.org"
    assert row["website_word_count"] == 3
    assert row["contact_website_word_count"] == 3
    assert fetched == ["https://example.org", "https://contact.example.org"]
    assert result.changed
    assert result.max_batch_rows == 1


def test_duplicate_url_across_both_tags_fetches_once(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(
        shard,
        [_legacy_row(website="https://example.org", contact="https://example.org")],
    )
    calls = 0

    def fetch(url: str) -> FetchResult:
        nonlocal calls
        calls += 1
        return FetchResult("ok", url, final_url=url, body=b"same full text")

    enrich_polygon_shard(
        shard,
        cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
        invocation_id="one",
        fetcher=fetch,
        extractor=_extract,
    )

    assert calls == 1


def test_failed_url_retries_on_next_invocation(tmp_path: Path) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(shard, [_legacy_row(contact=None)])
    cache = tmp_path / "run" / "cache" / "text.sqlite3"

    enrich_polygon_shard(
        shard,
        cache_path=cache,
        invocation_id="one",
        fetcher=lambda url: FetchResult("fetch_error", url, message="TimeoutError"),
        extractor=_extract,
    )
    assert pq.read_table(shard)["website_text_status"][0].as_py() == "fetch_error"

    enrich_polygon_shard(
        shard,
        cache_path=cache,
        invocation_id="two",
        fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"recovered text"),
        extractor=_extract,
    )

    row = pq.read_table(shard).to_pylist()[0]
    assert row["website_text_status"] == "success"
    assert row["website_text"] == "recovered text"


def test_promotion_failure_preserves_prior_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "run" / "polygons" / "source.parquet"
    _write_legacy(shard, [_legacy_row()])
    original = shard.read_bytes()

    def fail(_pairs):
        raise OSError("injected promotion failure")

    monkeypatch.setattr("osm_polygon_website_tag.enrich.atomic_promote_bundle", fail)

    with pytest.raises(OSError, match="injected"):
        enrich_polygon_shard(
            shard,
            cache_path=tmp_path / "run" / "cache" / "text.sqlite3",
            invocation_id="one",
            fetcher=lambda url: FetchResult("ok", url, final_url=url, body=b"text"),
            extractor=_extract,
        )

    assert shard.read_bytes() == original

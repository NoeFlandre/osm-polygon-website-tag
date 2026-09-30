"""End-to-end performance probes for the fetch and shard-processing paths."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pytest_benchmark.fixture import BenchmarkFixture
from tests.fixtures.polygon_shards import (
    legacy_polygon_row,
    polygon_row,
    write_legacy_polygon_shard,
)

from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.pipeline.detect_languages import detect_language_shard
from osm_polygon_website_tag.pipeline.enrich import enrich_polygon_shard
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.web.text_cache import CachedText, TextCache
from osm_polygon_website_tag.web.web_fetch import FetchResult

_ARTICLE = (
    "<html><head><title>Benchmark</title></head><body><nav>"
    + "".join(f'<a href="/nav/{index}">Navigation {index}</a>' for index in range(200))
    + "</nav><article>"
    + "".join(
        "<p>Benchmark pages contain useful public information about places, services, "
        "opening times, local history, and ways to contact the organization.</p>"
        for _ in range(60)
    )
    + "</article></body></html>"
).encode()
_IDENTITY = ModelIdentity("benchmark", "model.bin", "revision", "b" * 64)


def _fetch_after_latency(url: str) -> FetchResult:
    """Simulate a short network delay without opening a socket."""
    time.sleep(0.02)
    return FetchResult(
        "ok",
        url,
        final_url=url,
        body=_ARTICLE,
        charset="utf-8",
        media_type="text/html",
    )


def test_enrichment_throughput(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """Time bounded fetch, real text extraction, and atomic shard promotion."""
    sequence = 0
    last_shard: Path | None = None

    def run_once() -> None:
        nonlocal sequence, last_shard
        sequence += 1
        directory = tmp_path / f"run-{sequence}"
        directory.mkdir()
        last_shard = directory / "polygons.parquet"
        rows = [
            legacy_polygon_row(
                polygon_id=f"source:way/{index}",
                website=f"https://example.org/page/{index}",
                contact=None,
            )
            for index in range(256)
        ]
        write_legacy_polygon_shard(last_shard, rows)
        enrich_polygon_shard(
            last_shard,
            cache_path=directory / "cache.sqlite3",
            invocation_id=f"benchmark-{sequence}",
            fetcher=_fetch_after_latency,
            batch_rows=256,
            fetch_workers=32,
        )

    benchmark.pedantic(run_once, rounds=2, iterations=1, warmup_rounds=0)

    assert last_shard is not None
    assert pq.read_table(last_shard).num_rows == 256


class _BenchmarkDetector:
    @property
    def identity(self) -> ModelIdentity:
        return _IDENTITY

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        return [LanguagePrediction("eng_Latn", 0.99) for _ in texts]


def test_language_detection_batch_overhead(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """Time Arrow batching and checkpoint promotion with a stub detector."""
    sequence = 0
    last_shard: Path | None = None

    def run_once() -> None:
        nonlocal sequence, last_shard
        sequence += 1
        last_shard = tmp_path / f"language-{sequence}.parquet"
        rows = [
            polygon_row(
                "v1.5",
                polygon_id=f"source:way/{index}",
                website=f"https://example.org/{index}",
                contact_website=None,
                website_text="A useful English article with enough words for detection.",
                website_word_count=9,
                website_text_status="success",
                contact_website_text=None,
                contact_website_word_count=None,
                contact_website_text_status="absent",
                website_language=None,
                website_language_probability=None,
            )
            for index in range(256)
        ]
        pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), last_shard)
        detect_language_shard(last_shard, detector=_BenchmarkDetector(), batch_rows=256)

    benchmark.pedantic(run_once, rounds=2, iterations=1, warmup_rounds=0)

    assert last_shard is not None
    assert pq.read_table(last_shard).column("website_language")[0].as_py() == "eng_Latn"


def test_cli_import_startup(benchmark: BenchmarkFixture) -> None:
    """Time a fresh CLI import and guard the lazy Trafilatura boundary."""

    def import_cli() -> str:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, osm_polygon_website_tag.application.cli; "
                "print('trafilatura' in sys.modules)",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()

    result = benchmark.pedantic(import_cli, rounds=3, iterations=1, warmup_rounds=0)

    assert result == "False"


def test_text_cache_batch_lookup(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """Time bounded lookup of ten thousand successful URL results."""
    cache = TextCache(tmp_path / "lookup.sqlite3")
    urls = [f"https://example.org/{index}" for index in range(10_000)]
    try:
        for url in urls:
            cache.record(
                CachedText(
                    url=url,
                    status="success",
                    text="one cached sentence",
                    word_count=3,
                    final_url=url,
                    message=None,
                    attempt_count=1,
                    last_attempt_at="",
                    trafilatura_version="2.1.0",
                    invocation_id="benchmark",
                ),
                invocation_id="benchmark",
            )
        cache.flush()

        result = benchmark(cache.get_reusable_many, urls, invocation_id="benchmark")

        assert len(result) == len(urls)
    finally:
        cache.close()

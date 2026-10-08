"""Hermetic environment fixtures for workflow-level tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.application.guard_support import install_producer_guard

from osm_polygon_website_tag.application import source_processing
from osm_polygon_website_tag.contracts.text_schema import count_words
from osm_polygon_website_tag.pipeline.enrich import EnrichmentResult, enrich_polygon_shard
from osm_polygon_website_tag.publishing.incremental import load_upload_checkpoint
from osm_polygon_website_tag.web.text_extract import TextExtraction
from osm_polygon_website_tag.web.web_fetch import FetchResult


@pytest.fixture
def offline_remote_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep workflow tests local; remote reconciliation has dedicated unit tests."""

    def reconcile_upload_checkpoint(
        run_dir: Path | str,
        **_kwargs: object,
    ) -> object:
        return load_upload_checkpoint(run_dir)

    monkeypatch.setattr(
        "osm_polygon_website_tag.application.workflow.reconcile_upload_checkpoint",
        reconcile_upload_checkpoint,
    )


@pytest.fixture
def static_text_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace workflow network enrichment with deterministic local responses."""

    def fetcher(url: str) -> FetchResult:
        return FetchResult("ok", url, final_url=url, body=b"website text")

    def extractor(_html: bytes, *, url: str) -> TextExtraction:
        text = f"text from {url}"
        return TextExtraction("success", text, count_words(text), None, "2.1.0")

    def enrich(shard_path: Path | str, **kwargs: Any) -> EnrichmentResult:
        return enrich_polygon_shard(
            shard_path,
            **kwargs,
            fetcher=fetcher,
            extractor=extractor,
        )

    monkeypatch.setattr(source_processing, "enrich_polygon_shard", enrich, raising=False)


@pytest.fixture(autouse=True)
def producers_send_counted_progress(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail a test when a source-processing producer sends counted text as a plain string.

    Reporter tests cannot see a reverted producer, because a plain "[n/m] text" string
    still drives the bar. The guard records what the producers send to
    ``report_progress`` and fails the test on any counted-looking plain string.
    """
    plain_counted = install_producer_guard(monkeypatch, source_processing)
    yield
    assert plain_counted == [], f"producers sent plain counted progress: {plain_counted}"

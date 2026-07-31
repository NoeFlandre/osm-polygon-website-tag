"""Tests for local map/card migration without source processing."""

from __future__ import annotations

from pathlib import Path

from tests.reporting.test_finalize import _setup

from osm_polygon_website_tag.reporting.repair import refresh_card_run
from osm_polygon_website_tag.reporting.verify import verify_results


def test_refresh_card_is_idempotent_for_modern_complete_run(tmp_path: Path) -> None:
    run_dir, _state = _setup(tmp_path)
    from osm_polygon_website_tag.reporting.finalize import finalize_run

    assert finalize_run(run_dir).ok
    first = refresh_card_run(run_dir)
    second = refresh_card_run(run_dir)

    assert first.ok is True
    assert second.ok is True
    assert verify_results(run_dir).ok is True
    assert (
        '"card_contract_version": 1'
        in (run_dir / "manifests" / "completion_receipt.json").read_text()
    )

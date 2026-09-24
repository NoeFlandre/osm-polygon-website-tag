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
        '"card_contract_version": 2'
        in (run_dir / "manifests" / "completion_receipt.json").read_text()
    )


def test_preflight_reports_every_error_for_a_damaged_complete_run(tmp_path: Path) -> None:
    from osm_polygon_website_tag.reporting.finalize import finalize_run
    from osm_polygon_website_tag.reporting.repair import _preflight_legacy_refresh

    run_dir, _state = _setup(tmp_path)
    assert finalize_run(run_dir).ok
    assert _preflight_legacy_refresh(run_dir).errors == []
    (run_dir / "dataset.yaml").unlink()
    (run_dir / "manifests" / "completion_receipt.json").write_text("[]", encoding="utf-8")

    report = _preflight_legacy_refresh(run_dir)

    assert report.ok is False
    assert report.errors == [
        "missing refresh prerequisite: dataset.yaml",
        "completion receipt is not a JSON object",
    ]


def test_unreadable_refresh_shards_reports_only_corrupt_public_parquet(tmp_path: Path) -> None:
    from osm_polygon_website_tag.reporting.repair import _unreadable_refresh_shards

    (tmp_path / "polygons").mkdir()
    (tmp_path / "polygons" / "bad.parquet").write_bytes(b"not parquet")
    (tmp_path / "polygons" / "notes.txt").write_bytes(b"ignored")

    errors = _unreadable_refresh_shards(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith(
        f"unreadable public shard {tmp_path / 'polygons' / 'bad.parquet'}: "
    )


def test_invalid_refresh_receipt_checks_only_the_completion_receipt(tmp_path: Path) -> None:
    from osm_polygon_website_tag.reporting.repair import _invalid_refresh_receipt

    manifests = tmp_path / "manifests"
    manifests.mkdir()
    assert _invalid_refresh_receipt(tmp_path) == []
    (manifests / "other.json").write_text("[]", encoding="utf-8")
    assert _invalid_refresh_receipt(tmp_path) == []

    receipt = manifests / "completion_receipt.json"
    receipt.write_text('{"k": "café"}', encoding="utf-8")
    assert _invalid_refresh_receipt(tmp_path) == []
    receipt.write_text("[1]", encoding="utf-8")
    assert _invalid_refresh_receipt(tmp_path) == ["completion receipt is not a JSON object"]
    receipt.write_bytes(b"{")
    [error] = _invalid_refresh_receipt(tmp_path)
    assert error.startswith("invalid completion receipt: ")
    receipt.write_bytes(b'"\xff"')
    [error] = _invalid_refresh_receipt(tmp_path)
    assert error.startswith("invalid completion receipt: ")

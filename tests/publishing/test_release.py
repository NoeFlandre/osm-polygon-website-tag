"""Contract tests for the card/report release path."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.fixtures.call_recording import recording_stub
from tests.publishing.receipt_helpers import recording_receipt_reads
from tests.reporting.test_finalize import _setup

import osm_polygon_website_tag.publishing.release as release_module
import osm_polygon_website_tag.publishing.remote_identity as remote_identity
from osm_polygon_website_tag.publishing.release import (
    CARD_RELEASE_FILES,
    ReleasedFile,
    build_card_release_plan,
    release_card_and_stats,
)
from osm_polygon_website_tag.reporting.artifact_inventory import (
    data_manifest_sha256,
    hash_file,
)
from osm_polygon_website_tag.reporting.finalize import (
    _write_completion_receipt,
    replace_receipt_atomic,
)
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET


class _RecordingUploader:
    """Capture the exact upload call a release would make."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, run_dir: Path, **kwargs: Any) -> None:
        self.calls.append({"run_dir": run_dir, **kwargs})


def test_dry_run_plans_exactly_the_card_and_report(run_dir: Path) -> None:
    uploader = _RecordingUploader()

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET, uploader=uploader)

    assert report.repo_id == DEFAULT_HF_DATASET
    assert report.published is False
    assert report.revision is None
    assert [item.relative_path for item in report.files] == list(CARD_RELEASE_FILES)
    assert report.verified_shards
    assert uploader.calls == []
    assert report.files[0].text_population_entries == (
        (
            "polygons/monaco-latest.parquet",
            (run_dir / "polygons" / "monaco-latest.parquet").stat().st_size,
            hash_file(run_dir / "polygons" / "monaco-latest.parquet"),
        ),
    )


def test_apply_uploads_only_the_card_and_report_then_verifies(run_dir: Path) -> None:
    uploader = _RecordingUploader()
    seen: list[tuple[str, tuple[ReleasedFile, ...]]] = []

    def verifier(repo_id: str, files: tuple[ReleasedFile, ...]) -> str:
        seen.append((repo_id, files))
        return "cafebabe"

    report = release_card_and_stats(
        run_dir,
        confirm_repo=DEFAULT_HF_DATASET,
        apply=True,
        uploader=uploader,
        verifier=verifier,
    )

    assert report.published is True
    assert report.revision == "cafebabe"
    assert len(uploader.calls) == 1
    call = uploader.calls[0]
    assert call["repo_id"] == DEFAULT_HF_DATASET
    assert [item.relative_path for item in call["files"]] == list(CARD_RELEASE_FILES)
    assert seen == [(DEFAULT_HF_DATASET, report.files)]


def test_second_release_is_a_byte_stable_no_op(run_dir: Path) -> None:
    first = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)
    stats = run_dir / "stats.json"
    before = (stats.read_bytes(), stats.stat().st_mtime_ns)

    second = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert second.files == first.files
    assert second.recomputed is False
    assert (stats.read_bytes(), stats.stat().st_mtime_ns) == before


def test_wrong_repository_confirmation_is_refused(run_dir: Path) -> None:
    with pytest.raises(ValueError, match="repository confirmation"):
        release_card_and_stats(run_dir, confirm_repo="someone-else/osm-polygon-website-tag")


def test_overridden_repository_is_refused_even_when_confirmation_matches(run_dir: Path) -> None:
    with pytest.raises(ValueError, match="canonical"):
        release_card_and_stats(
            run_dir,
            confirm_repo="someone-else/osm-polygon-website-tag",
            repo_id="someone-else/osm-polygon-website-tag",
        )


def test_release_rejects_a_non_dataset_repository_kind(run_dir: Path) -> None:
    with pytest.raises(ValueError, match="canonical dataset"):
        release_card_and_stats(
            run_dir,
            confirm_repo=DEFAULT_HF_DATASET,
            repo_kind="model",
        )


def test_release_requires_complete_status_before_dry_run(tmp_path: Path) -> None:
    run_dir, _state = _setup(tmp_path)

    with pytest.raises(ValueError, match="COMPLETE"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_rejects_external_text_population_shards(
    run_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path.parent / "external-text" / "source.parquet"
    monkeypatch.setattr(release_module, "text_population_parquets", lambda _root: [external])

    with pytest.raises(ValueError, match="external text population shards bound"):
        release_module._require_complete_release(run_dir)


def test_release_rejects_uninventoried_text_population_shards(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    regional = run_dir / "regional" / "polygons" / "source.parquet"
    regional.parent.mkdir(parents=True)
    regional.write_bytes(b"regional shard")
    monkeypatch.setattr(release_module, "text_population_parquets", lambda _root: [regional])

    with pytest.raises(ValueError, match="release artifact inventory"):
        release_module._require_complete_release(run_dir)


def test_release_requires_completion_receipt(run_dir: Path) -> None:
    (run_dir / "manifests" / "completion_receipt.json").unlink()

    with pytest.raises(ValueError, match="completion receipt"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


@pytest.mark.parametrize(
    "receipt_text",
    ["{invalid", "[]", "{}", '{"data_manifest_sha256": ""}'],
)
def test_release_rejects_invalid_completion_receipt(run_dir: Path, receipt_text: str) -> None:
    (run_dir / "manifests" / "completion_receipt.json").write_text(
        receipt_text,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="completion receipt"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_rebuilds_missing_metadata_before_verification(run_dir: Path) -> None:
    (run_dir / "README.md").unlink()
    (run_dir / "stats.json").unlink()

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert report.recomputed is True
    assert (run_dir / "README.md").is_file()
    assert (run_dir / "stats.json").is_file()


def test_release_rebuilds_missing_readme_from_trusted_dataset_yaml(run_dir: Path) -> None:
    custom = (
        "license: mit",
        "path: custom/*.parquet",
    )
    for relative in ("README.md", "dataset.yaml"):
        path = run_dir / relative
        text = path.read_text(encoding="utf-8")
        text = text.replace("license: odbl", custom[0]).replace(
            "path: polygons/*.parquet", custom[1]
        )
        path.write_text(text, encoding="utf-8")
    replace_receipt_atomic(run_dir)
    (run_dir / "README.md").unlink()

    release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert custom[0] in (run_dir / "README.md").read_text(encoding="utf-8")
    assert custom[1] in (run_dir / "README.md").read_text(encoding="utf-8")
    assert custom[0] in (run_dir / "dataset.yaml").read_text(encoding="utf-8")
    assert custom[1] in (run_dir / "dataset.yaml").read_text(encoding="utf-8")


def test_release_refuses_unrecoverable_missing_readme_metadata(run_dir: Path) -> None:
    readme = run_dir / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("license: odbl", "license: custom"),
        encoding="utf-8",
    )
    replace_receipt_atomic(run_dir)
    readme.unlink()

    with pytest.raises(ValueError, match="missing README custom metadata"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_does_not_mutate_bundle_when_missing_readme_body_cannot_be_recovered(
    run_dir: Path,
) -> None:
    readme = run_dir / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        .replace("Machine-readable metadata", "Tampered preserved body")
        .replace("website_total_words: 2", "website_total_words: 999"),
        encoding="utf-8",
    )
    replace_receipt_atomic(run_dir)
    readme.unlink()
    before = {
        relative: (run_dir / relative).read_bytes()
        for relative in ("dataset.yaml", "stats.json", "assets/geographic_polygon_density.png")
    }

    with pytest.raises(ValueError, match="README body"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert not readme.exists()
    assert {relative: (run_dir / relative).read_bytes() for relative in before} == before


def test_release_rebuilds_missing_dataset_yaml_before_verification(run_dir: Path) -> None:
    (run_dir / "dataset.yaml").unlink()

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert report.recomputed is True
    assert (run_dir / "dataset.yaml").is_file()
    assert "unique_text_identity_count: 1" in (run_dir / "dataset.yaml").read_text()


def test_release_rebuilds_missing_dataset_yaml_from_readme_custom_metadata(
    run_dir: Path,
) -> None:
    custom = ("license: mit", "path: custom/*.parquet")
    readme = run_dir / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        .replace("license: odbl", custom[0])
        .replace("path: polygons/*.parquet", custom[1]),
        encoding="utf-8",
    )
    dataset_yaml = run_dir / "dataset.yaml"
    dataset_yaml.write_text(
        dataset_yaml.read_text(encoding="utf-8")
        .replace("license: odbl", custom[0])
        .replace("path: polygons/*.parquet", custom[1]),
        encoding="utf-8",
    )
    replace_receipt_atomic(run_dir)
    dataset_yaml.unlink()

    release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    rebuilt = (run_dir / "dataset.yaml").read_text(encoding="utf-8")
    assert custom[0] in rebuilt
    assert custom[1] in rebuilt


def test_release_rebuilds_missing_map_before_verification(run_dir: Path) -> None:
    (run_dir / "assets" / "geographic_polygon_density.png").unlink()

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert report.recomputed is True
    assert (run_dir / "assets" / "geographic_polygon_density.png").is_file()


def test_release_rebuilds_stale_metadata_before_verification(run_dir: Path) -> None:
    readme = run_dir / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "website_total_words: 2", "website_total_words: 999"
        ),
        encoding="utf-8",
    )
    (run_dir / "stats.json").write_text("{}\n", encoding="utf-8")

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert report.recomputed is True
    assert "website_total_words: 2" in readme.read_text(encoding="utf-8")
    assert '"schema_version"' in (run_dir / "stats.json").read_text(encoding="utf-8")


def test_release_rejects_existing_readme_without_front_matter(run_dir: Path) -> None:
    (run_dir / "README.md").write_text("body-only README", encoding="utf-8")

    with pytest.raises(ValueError, match="custom YAML identity"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_rebuilds_stale_geographic_bundle_from_the_canonical_text_summary(
    run_dir: Path,
) -> None:
    readme = run_dir / "README.md"
    current_readme = readme.read_text(encoding="utf-8")
    current_readme = current_readme.replace(
        "## Website text\n", "## Website text\n\nSTALE website values.\n", 1
    )
    geographic_start = current_readme.index("## Geographic distribution")
    # The section that follows the geographic block on the current card.
    links_start = current_readme.index("## Polygon geometry", geographic_start)
    readme.write_text(
        current_readme[:geographic_start]
        + "## Geographic distribution\n\nSTALE geographic values.\n\n"
        + current_readme[links_start:],
        encoding="utf-8",
    )
    dataset_yaml = run_dir / "dataset.yaml"
    dataset_yaml.write_text(
        dataset_yaml.read_text(encoding="utf-8").replace(
            "polygon_density_row_count: 1", "polygon_density_row_count: 999"
        ),
        encoding="utf-8",
    )
    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    map_path.write_bytes(b"stale-map")
    _write_completion_receipt(run_dir)

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    updated_readme = readme.read_text(encoding="utf-8")
    assert report.recomputed is True
    assert "STALE geographic values" not in updated_readme
    assert "STALE website values" not in updated_readme
    assert "Regional overlap duplicates are removed globally" in updated_readme
    assert "covering **1** unique polygons with extracted text" in updated_readme
    assert "polygon_density_row_count: 1" in dataset_yaml.read_text(encoding="utf-8")
    assert "unique_text_identity_count: 1" in dataset_yaml.read_text(encoding="utf-8")
    assert map_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_release_refreshes_readme_front_matter_with_dataset_yaml(run_dir: Path) -> None:
    readme = run_dir / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "website_total_words: 2", "website_total_words: 999"
        ),
        encoding="utf-8",
    )

    release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    refreshed = readme.read_text(encoding="utf-8")
    assert "website_total_words: 2" in refreshed
    assert "website_total_words: 999" not in refreshed


def test_release_rejects_custom_dataset_yaml_tampering_before_refresh(run_dir: Path) -> None:
    dataset_yaml = run_dir / "dataset.yaml"
    dataset_yaml.write_text(
        dataset_yaml.read_text(encoding="utf-8").replace("license: odbl", "license: mit"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="custom YAML identity"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_rejects_custom_yaml_tampering_for_legacy_receipt(run_dir: Path) -> None:
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.pop("dataset_yaml_custom_sha256", None)
    receipt.pop("readme_yaml_custom_sha256", None)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    dataset_yaml = run_dir / "dataset.yaml"
    dataset_yaml.write_text(
        dataset_yaml.read_text(encoding="utf-8").replace("license: odbl", "license: mit"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="custom YAML identity"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_rejects_identical_custom_yaml_tampering_for_legacy_receipt(
    run_dir: Path,
) -> None:
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.pop("dataset_yaml_custom_sha256", None)
    receipt.pop("readme_yaml_custom_sha256", None)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    for relative in ("README.md", "dataset.yaml"):
        path = run_dir / relative
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace("license: odbl", "license: mit")
            .replace("website_total_words: 2", "website_total_words: 999"),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="custom YAML identity"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_rejects_preserved_readme_body_tampering(run_dir: Path) -> None
"""Contract tests for the card/report release path."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tests.reporting.test_finalize import _setup

import osm_polygon_website_tag.publishing.release as release_module
from osm_polygon_website_tag.publishing.release import (
    CARD_RELEASE_FILES,
    ReleasedFile,
    build_card_release_plan,
    default_hub_verifier,
    release_card_and_stats,
)
from osm_polygon_website_tag.reporting.artifact_inventory import (
    data_manifest_sha256,
    hash_file,
    publishable_paths,
)
from osm_polygon_website_tag.reporting.finalize import (
    _write_completion_receipt,
    finalize_run,
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


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    root, _state = _setup(tmp_path)
    assert finalize_run(root).ok
    return root


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
        .replace("Code and README", "Tampered preserved body")
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
    links_start = current_readme.index("## Links", geographic_start)
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
    assert "regional overlap duplicates are removed globally" in updated_readme
    assert "**1** unique polygons with successfully" in updated_readme
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


def test_release_rejects_preserved_readme_body_tampering(run_dir: Path) -> None:
    readme = run_dir / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        .replace("Code and README", "Tampered preserved body")
        .replace("website_total_words: 2", "website_total_words: 999"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="README body identity"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)


def test_release_adds_geometry_without_replacing_existing_card_or_configuration(
    run_dir: Path,
) -> None:
    readme_prefix = (
        "---\n"
        "language:\n"
        "  - eng\n"
        "configs:\n"
        "  - config_name: language-v1\n"
        "    data_files:\n"
        "      - split: train\n"
        "        path: language-v1/*.parquet\n"
        "---\n"
        "# Existing dataset card\n\n"
        "## Language v1\n\n"
        "This pre-existing generated section must remain byte-for-byte.\n\n"
    )
    readme_suffix = (
        "## Geographic distribution\n\n"
        "The existing geographic section remains untouched.\n\n"
        "## Unrelated metadata\n\n"
        "Keep this section too.\n"
    )
    original_readme = readme_prefix + readme_suffix
    original_yaml = (
        "configs:\n"
        "  - config_name: language-v1\n"
        "    data_files:\n"
        "      - split: train\n"
        "        path: language-v1/*.parquet\n"
        "language: eng\n"
    )
    (run_dir / "README.md").write_bytes(original_readme.encode("utf-8"))
    (run_dir / "dataset.yaml").write_bytes(original_yaml.encode("utf-8"))
    _write_completion_receipt(run_dir)

    release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    updated_readme = (run_dir / "README.md").read_bytes().decode("utf-8")
    geometry_start = updated_readme.index("## Polygon geometry")
    geographic_start = updated_readme.index("## Geographic distribution")
    unrelated_start = updated_readme.index("## Unrelated metadata")
    assert updated_readme[:geometry_start] == readme_prefix
    assert updated_readme[geographic_start:unrelated_start].startswith(
        "## Geographic distribution\n\n![H3 polygon density]"
    )
    assert "The existing geographic section remains untouched." not in updated_readme
    assert updated_readme[unrelated_start:] == readme_suffix[readme_suffix.index("## Unrelated") :]
    assert "complete breakdown is published as [`stats.json`](stats.json)" in updated_readme
    updated_yaml = (run_dir / "dataset.yaml").read_text(encoding="utf-8")
    assert updated_yaml.startswith(original_yaml.removesuffix("language: eng\n"))
    assert "language: eng\n" not in updated_yaml
    assert "polygon_density_row_count: 1" in updated_yaml
    assert "occupied_h3_cell_count: 1" in updated_yaml


def test_release_accepts_a_pre_pr_v12_receipt_without_data_identity(run_dir: Path) -> None:
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    del receipt["data_manifest_sha256"]
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    card_paths = tuple(
        run_dir / path for path in CARD_RELEASE_FILES if path != "manifests/completion_receipt.json"
    )
    card_bytes_before = {path: path.read_bytes() for path in card_paths}

    assert verify_results(run_dir).ok

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert report.data_manifest_sha256 == data_manifest_sha256(run_dir)
    assert report.recomputed is True
    assert {path: path.read_bytes() for path in card_paths} == card_bytes_before
    refreshed_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert refreshed_receipt["data_manifest_sha256"] == data_manifest_sha256(run_dir)


def test_unverified_run_is_refused(run_dir: Path) -> None:
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt_before = receipt_path.read_bytes()
    (run_dir / "polygons" / "monaco-latest.parquet").unlink()

    with pytest.raises(ValueError, match="refusing to release"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)
    assert receipt_path.read_bytes() == receipt_before


def test_tampered_receipt_bound_failure_is_rejected_before_replacement(run_dir: Path) -> None:
    failures = run_dir / "failures.jsonl"
    failures.write_text('{"url": "https://example.com"}\n', encoding="utf-8")
    _write_completion_receipt(run_dir)
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt_before = receipt_path.read_bytes()
    failures.write_text('{"url": "https://tampered.example"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="completion receipt"):
        release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert receipt_path.read_bytes() == receipt_before


def test_missing_report_is_refused(run_dir: Path) -> None:
    (run_dir / "stats.json").unlink()

    with pytest.raises(ValueError, match="missing card release artifact"):
        build_card_release_plan(run_dir)


def test_empty_remote_revision_is_refused(run_dir: Path) -> None:
    with pytest.raises(ValueError, match="empty revision"):
        release_card_and_stats(
            run_dir,
            confirm_repo=DEFAULT_HF_DATASET,
            apply=True,
            uploader=_RecordingUploader(),
            verifier=lambda repo_id, files: "",
        )


def test_release_report_binds_the_data_manifest(run_dir: Path) -> None:
    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    expected = data_manifest_sha256(run_dir)
    assert report.data_manifest_sha256 == expected
    assert {item.data_manifest_sha256 for item in report.files} == {expected}
    payload = report.to_payload()
    assert payload["data_manifest_sha256"] == expected
    assert payload["uploaded"] is False


def test_repeated_apply_skips_upload_when_remote_is_already_current(run_dir: Path) -> None:
    uploader = _RecordingUploader()
    remote_revisions = iter((None, "remote-revision"))
    verifier_calls: list[int] = []

    def remote_checker(repo_id: str, files: tuple[ReleasedFile, ...]) -> str | None:
        del repo_id, files
        return next(remote_revisions)

    def verifier(repo_id: str, files: tuple[ReleasedFile, ...]) -> str:
        del repo_id, files
        verifier_calls.append(1)
        return "remote-revision"

    first = release_card_and_stats(
        run_dir,
        confirm_repo=DEFAULT_HF_DATASET,
        apply=True,
        uploader=uploader,
        verifier=verifier,
        remote_checker=remote_checker,
    )
    second = release_card_and_stats(
        run_dir,
        confirm_repo=DEFAULT_HF_DATASET,
        apply=True,
        uploader=uploader,
        verifier=verifier,
        remote_checker=remote_checker,
    )

    assert first.uploaded is True
    assert first.no_op is False
    assert first.to_payload()["changed_files"] == list(CARD_RELEASE_FILES)
    assert second.uploaded is False
    assert second.no_op is True
    assert second.to_payload()["changed_files"] == []
    assert second.revision == "remote-revision"
    assert len(uploader.calls) == 1
    assert len(verifier_calls) == 1


def test_apply_reports_only_remote_metadata_changes(run_dir: Path) -> None:
    uploader = _RecordingUploader()

    report = release_card_and_stats(
        run_dir,
        confirm_repo=DEFAULT_HF_DATASET,
        apply=True,
        uploader=uploader,
        verifier=lambda repo_id, files: "remote-revision",
        remote_checker=lambda repo_id, files: release_module._RemoteCheck(None, ("README.md",)),
    )

    assert report.changed_files == ("README.md",)
    assert report.to_payload()["changed_files"] == ["README.md"]


def test_default_apply_checks_for_a_remote_no_op_before_upload(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[object] = []
    monkeypatch.setattr(
        release_module,
        "_default_remote_checker",
        lambda _repo_id, _files: "remote-revision",
    )
    monkeypatch.setattr(
        release_module,
        "_upload_card_files",
        lambda *args, **kwargs: uploads.append((args, kwargs)),
    )
    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: "token")
    monkeypatch.setattr(release_module, "default_hub_verifier", lambda *_args: "remote-revision")

    report = release_card_and_stats(
        run_dir,
        confirm_repo=DEFAULT_HF_DATASET,
        apply=True,
    )

    assert report.no_op is True
    assert report.uploaded is False
    assert report.revision == "remote-revision"
    assert uploads == []


def test_publish_resolves_default_upload_and_verification_adapters(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = build_card_release_plan(run_dir)
    uploads: list[object] = []
    monkeypatch.setattr(
        release_module,
        "_require_credentialed_uploader",
        lambda: lambda *args, **kwargs: uploads.append((args, kwargs)),
    )
    monkeypatch.setattr(release_module, "default_hub_verifier", lambda *_args: "remote-revision")

    result = release_module._publish(
        run_dir,
        repo_id=DEFAULT_HF_DATASET,
        repo_kind="dataset",
        files=files,
        uploader=None,
        verifier=None,
        remote_checker=None,
    )

    assert result.uploaded is True
    assert result.revision == "remote-revision"
    assert len(uploads) == 1


def test_remote_verification_refuses_data_identity_mismatch(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_receipt = tmp_path / "completion_receipt.json"
    remote_receipt.write_text(
        json.dumps({"data_manifest_sha256": "0" * 64}),
        encoding="utf-8",
    )
    parquet_entries = [
        SimpleNamespace(
            path=path.relative_to(run_dir).as_posix(),
            size=path.stat().st_size,
            lfs=SimpleNamespace(sha256=hash_file(path)),
        )
        for path in publishable_paths(run_dir)
        if path.suffix == ".parquet"
    ]

    class _Api:
        def __init__(self, *, token: str) -> None:
            assert token

        def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_id == DEFAULT_HF_DATASET
            assert repo_type == "dataset"
            return SimpleNamespace(sha="remote-revision")

        def hf_hub_download(
            self,
            repo_id: str,
            filename: str,
            *,
            revision: str,
            repo_type: str,
        ) -> str:
            assert repo_id == DEFAULT_HF_DATASET
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            if filename == "manifests/completion_receipt.json":
                return str(remote_receipt)
            return str(run_dir / filename)

        def list_repo_tree(
            self,
            _repo_id: str,
            *,
            path_in_repo: str,
            recursive: bool,
            expand: bool,
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert recursive is True
            assert expand is False
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return [entry for entry in parquet_entries if entry.path.startswith(path_in_repo + "/")]

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.release.resolve_hf_token",
        lambda: "token",
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=_Api))

    with pytest.raises(ValueError, match="data identity"):
        default_hub_verifier(DEFAULT_HF_DATASET, build_card_release_plan(run_dir))


def test_default_remote_verifier_accepts_matching_data_and_card(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    files = build_card_release_plan(run_dir)
    remote_root = tmp_path / "remote"
    for item in files:
        destination = remote_root / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / item.relative_path).read_bytes())
    for relative in ("manifests/sources.json", "manifests/expected_sources.json"):
        destination = remote_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / relative).read_bytes())
    parquet_entries = [
        SimpleNamespace(
            path=path.relative_to(run_dir).as_posix(),
            size=path.stat().st_size,
            lfs=SimpleNamespace(sha256=hash_file(path)),
        )
        for path in publishable_paths(run_dir)
        if path.suffix == ".parquet"
    ]
    assert parquet_entries

    class _Api:
        def __init__(self, *, token: str) -> None:
            assert token

        def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_id == DEFAULT_HF_DATASET
            assert repo_type == "dataset"
            return SimpleNamespace(sha="remote-revision")

        def get_paths_info(
            self,
            repo_id: str,
            *,
            paths: list[str],
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert repo_id == DEFAULT_HF_DATASET
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return [SimpleNamespace(size=(remote_root / paths[0]).stat().st_size)]

        def hf_hub_download(
            self,
            repo_id: str,
            filename: str,
            *,
            revision: str,
            repo_type: str,
        ) -> str:
            assert repo_id == DEFAULT_HF_DATASET
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return str(remote_root / filename)

        def list_repo_tree(
            self,
            repo_id: str,
            *,
            path_in_repo: str,
            recursive: bool,
            expand: bool,
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert repo_id == DEFAULT_HF_DATASET
            assert recursive is True
            assert expand is False
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return [entry for entry in parquet_entries if entry.path.startswith(path_in_repo + "/")]

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.release.resolve_hf_token",
        lambda: "token",
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=_Api))

    assert default_hub_verifier(DEFAULT_HF_DATASET, files) == "remote-revision"


def test_remote_verifier_rejects_changed_source_manifest_with_matching_receipt(
    run_dir: Path,
    tmp_path: Path,
) -> None:
    files = build_card_release_plan(run_dir)
    remote_root = tmp_path / "remote"
    for item in files:
        destination = remote_root / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / item.relative_path).read_bytes())
    for relative in ("manifests/sources.json", "manifests/expected_sources.json"):
        destination = remote_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / relative).read_bytes())
    (remote_root / "manifests" / "sources.json").write_text("[]\n", encoding="utf-8")
    parquet_entries = [
        SimpleNamespace(
            path=path.relative_to(run_dir).as_posix(),
            size=path.stat().st_size,
            lfs=SimpleNamespace(sha256=hash_file(path)),
        )
        for path in publishable_paths(run_dir)
        if path.suffix == ".parquet"
    ]

    class _Api:
        def hf_hub_download(
            self,
            _repo_id: str,
            filename: str,
            *,
            revision: str,
            repo_type: str,
        ) -> str:
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return str(remote_root / filename)

        def list_repo_tree(
            self,
            _repo_id: str,
            *,
            path_in_repo: str,
            recursive: bool,
            expand: bool,
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert recursive is True
            assert expand is False
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return [entry for entry in parquet_entries if entry.path.startswith(path_in_repo + "/")]

    with pytest.raises(ValueError, match="data manifest"):
        release_module._verify_remote_data_identity(
            _Api(), DEFAULT_HF_DATASET, "remote-revision", files
        )


def test_remote_verifier_detects_changed_parquet_with_an_unchanged_receipt(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    files = build_card_release_plan(run_dir)
    remote_root = tmp_path / "remote"
    for item in files:
        destination = remote_root / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / item.relative_path).read_bytes())
    for relative in ("manifests/sources.json", "manifests/expected_sources.json"):
        destination = remote_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / relative).read_bytes())
    parquet_entries = []
    changed = False
    for path in publishable_paths(run_dir):
        if path.suffix != ".parquet":
            continue
        relative = path.relative_to(run_dir).as_posix()
        digest = "0" * 64 if not changed else hash_file(path)
        changed = True
        parquet_entries.append(
            SimpleNamespace(
                path=relative,
                size=path.stat().st_size,
                lfs=SimpleNamespace(sha256=digest),
            )
        )
    assert parquet_entries

    class _Api:
        def __init__(self, *, token: str) -> None:
            assert token

        def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_id == DEFAULT_HF_DATASET
            assert repo_type == "dataset"
            return SimpleNamespace(sha="remote-revision")

        def get_paths_info(
            self,
            repo_id: str,
            *,
            paths: list[str],
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert repo_id == DEFAULT_HF_DATASET
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return [SimpleNamespace(size=(remote_root / paths[0]).stat().st_size)]

        def hf_hub_download(
            self,
            repo_id: str,
            filename: str,
            *,
            revision: str,
            repo_type: str,
        ) -> str:
            assert repo_id == DEFAULT_HF_DATASET
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return str(remote_root / filename)

        def list_repo_tree(
            self,
            repo_id: str,
            *,
            path_in_repo: str,
            recursive: bool,
            expand: bool,
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert repo_id == DEFAULT_HF_DATASET
            assert recursive is True
            assert expand is False
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return [entry for entry in parquet_entries if entry.path.startswith(path_in_repo + "/")]

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.release.resolve_hf_token",
        lambda: "token",
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=_Api))

    with pytest.raises(ValueError, match="data manifest"):
        default_hub_verifier(DEFAULT_HF_DATASET, files)


def test_upload_card_files_passes_only_the_release_patterns(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def upload_folder(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(upload_folder=upload_folder),
    )
    files = build_card_release_plan(run_dir)

    release_module._upload_card_files(
        run_dir,
        repo_id=DEFAULT_HF_DATASET,
        repo_kind="dataset",
        files=files,
    )

    assert captured["folder_path"] == str(run_dir)
    assert captured["allow_patterns"] == list(CARD_RELEASE_FILES)


def test_remote_release_helpers_fail_closed_on_invalid_remote_state(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    item = build_card_release_plan(run_dir)[0]
    missing_api = SimpleNamespace(get_paths_info=lambda *_args, **_kwargs: [])
    with pytest.raises(ValueError, match="remote file missing"):
        release_module._verify_remote_size(
            missing_api,
            DEFAULT_HF_DATASET,
            "remote-revision",
            item,
        )

    wrong_size_api = SimpleNamespace(
        get_paths_info=lambda *_args, **_kwargs: [SimpleNamespace(size=item.size_bytes + 1)]
    )
    with pytest.raises(ValueError, match="remote size mismatch"):
        release_module._verify_remote_size(
            wrong_size_api,
            DEFAULT_HF_DATASET,
            "remote-revision",
            item,
        )

    wrong_file = tmp_path / "wrong.txt"
    wrong_file.write_text("wrong", encoding="utf-8")
    with pytest.raises(ValueError, match="remote SHA-256 mismatch"):
        release_module._verify_remote_digest(
            SimpleNamespace(hf_hub_download=lambda *_args, **_kwargs: str(wrong_file)),
            DEFAULT_HF_DATASET,
            "remote-revision",
            item,
        )

    with pytest.raises(ValueError, match="empty revision"):
        release_module._remote_revision(
            SimpleNamespace(repo_info=lambda *_args, **_kwargs: SimpleNamespace(sha="")),
            DEFAULT_HF_DATASET,
        )

    with pytest.raises(ValueError, match="single data identity"):
        release_module._expected_data_identity(
            (ReleasedFile("README.md", "a", 1), ReleasedFile("stats.json", "b", 1))
        )
    with pytest.raises(ValueError, match="single data identity"):
        release_module._expected_data_identity(
            (
                ReleasedFile("README.md", "a", 1, "a" * 64),
                ReleasedFile("stats.json", "b", 1, "b" * 64),
            )
        )

    invalid_receipt = tmp_path / "invalid-receipt.json"
    invalid_receipt.write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="completion receipt is invalid"):
        release_module._remote_data_identity(
            SimpleNamespace(hf_hub_download=lambda *_args, **_kwargs: str(invalid_receipt)),
            DEFAULT_HF_DATASET,
            "remote-revision",
        )

    missing_identity = tmp_path / "missing-identity.json"
    missing_identity.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no data identity"):
        release_module._remote_data_identity(
            SimpleNamespace(hf_hub_download=lambda *_args, **_kwargs: str(missing_identity)),
            DEFAULT_HF_DATASET,
            "remote-revision",
        )

    legacy_receipt = tmp_path / "legacy-receipt.json"
    legacy_receipt.write_text('{"schema_version": "v1.2"}', encoding="utf-8")
    assert (
        release_module._remote_data_identity(
            SimpleNamespace(hf_hub_download=lambda *_args, **_kwargs: str(legacy_receipt)),
            DEFAULT_HF_DATASET,
            "remote-revision",
        )
        is None
    )

    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: "token")
    monkeypatch.setattr(release_module, "_remote_revision", lambda *_args: "remote-revision")
    monkeypatch.setattr(release_module, "_verify_remote_data_identity", lambda *_args: None)
    monkeypatch.setattr(release_module, "_verify_remote_parquet_data_identity", lambda *_args: None)
    monkeypatch.setattr(
        release_module, "_verify_remote_text_population_identity", lambda *_args: None
    )
    monkeypatch.setattr(
        release_module,
        "_remote_changed_files",
        lambda *_args: (item.relative_path,),
    )
    checked = release_module._default_remote_checker(DEFAULT_HF_DATASET, (item,))
    assert checked == release_module._RemoteCheck(None, (item.relative_path,))


def test_remote_changed_files_reports_only_mismatched_card_files(
    tmp_path: Path,
) -> None:
    local_readme = tmp_path / "README.md"
    local_yaml = tmp_path / "dataset.yaml"
    local_readme.write_text("local card", encoding="utf-8")
    local_yaml.write_text("local metadata", encoding="utf-8")
    remote_readme = tmp_path / "remote-README.md"
    remote_yaml = tmp_path / "remote-dataset.yaml"
    remote_readme.write_text("stale card", encoding="utf-8")
    remote_yaml.write_bytes(local_yaml.read_bytes())
    files = (
        ReleasedFile("README.md", hash_file(local_readme), local_readme.stat().st_size),
        ReleasedFile("dataset.yaml", hash_file(local_yaml), local_yaml.stat().st_size),
    )
    remote_paths = {"README.md": remote_readme, "dataset.yaml": remote_yaml}

    class Api:
        def get_paths_info(
            self,
            _repo_id: str,
            *,
            paths: list[str],
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert revision == "revision"
            assert repo_type == "dataset"
            path = remote_paths[paths[0]]
            return [SimpleNamespace(size=path.stat().st_size)]

        def hf_hub_download(
            self,
            _repo_id: str,
            filename: str,
            *,
            revision: str,
            repo_type: str,
        ) -> str:
            assert revision == "revision"
            assert repo_type == "dataset"
            return str(remote_paths[filename])

    assert release_module._remote_changed_files(Api(), "dataset", "revision", files) == (
        "README.md",
    )


def test_credentialed_release_uploader_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: None)
    with pytest.raises(ValueError, match="Hugging Face"):
        release_module._require_credentialed_uploader()

    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: "token")
    assert release_module._require_credentialed_uploader() is release_module._upload_card_files


def test_release_private_identity_and_remote_adapters_are_exact(
    run_dir: Path,
    tmp_path: Path,
) -> None:
    files = build_card_release_plan(run_dir)
    identity = data_manifest_sha256(run_dir)
    assert release_module._expected_data_identity(files) == identity

    remote_root = tmp_path / "remote"
    for item in files:
        destination = remote_root / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / item.relative_path).read_bytes())

    for relative in ("manifests/sources.json", "manifests/expected_sources.json"):
        destination = remote_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((run_dir / relative).read_bytes())
    parquet_entries = [
        SimpleNamespace(
            path=path.relative_to(run_dir).as_posix(),
            size=path.stat().st_size,
            lfs=SimpleNamespace(sha256=hash_file(path)),
        )
        for path in publishable_paths(run_dir)
        if path.suffix == ".parquet"
    ]

    class Api:
        def repo_info(self, repo_id: str, *, repo_type: str) -> SimpleNamespace:
            assert repo_id == DEFAULT_HF_DATASET
            assert repo_type == "dataset"
            return SimpleNamespace(sha="revision")

        def get_paths_info(
            self,
            repo_id: str,
            *,
            paths: list[str],
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert repo_id == DEFAULT_HF_DATASET
            assert paths in [[files[0].relative_path]]
            assert revision == "revision"
            assert repo_type == "dataset"
            return [SimpleNamespace(size=None)]

        def hf_hub_download(
            self,
            repo_id: str,
            filename: str,
            *,
            revision: str,
            repo_type: str,
        ) -> str:
            assert repo_id == DEFAULT_HF_DATASET
            assert revision == "revision"
            assert repo_type == "dataset"
            return str(remote_root / filename)

        def list_repo_tree(
            self,
            repo_id: str,
            *,
            path_in_repo: str,
            recursive: bool,
            expand: bool,
            revision: str,
            repo_type: str,
        ) -> list[SimpleNamespace]:
            assert repo_id == DEFAULT_HF_DATASET
            assert recursive is True
            assert expand is False
            assert revision == "revision"
            assert repo_type == "dataset"
            return [entry for entry in parquet_entries if entry.path.startswith(path_in_repo + "/")]

    api = Api()
    assert release_module._remote_revision(api, DEFAULT_HF_DATASET) == "revision"
    release_module._verify_remote_size(api, DEFAULT_HF_DATASET, "revision", files[0])
    release_module._verify_remote_digest(api, DEFAULT_HF_DATASET, "revision", files[0])
    assert release_module._remote_data_identity(api, DEFAULT_HF_DATASET, "revision") == identity
    release_module._verify_remote_data_identity(api, DEFAULT_HF_DATASET, "revision", files)


def test_release_completion_and_card_update_fail_closed(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert release_module._completion_data_identity(
        run_dir / "manifests" / "completion_receipt.json"
    ) == data_manifest_sha256(run_dir)
    assert release_module._require_complete_release(run_dir) == data_manifest_sha256(run_dir)

    monkeypatch.setattr(
        release_module,
        "verify_receipt_before_card_refresh",
        lambda _root, errors: errors.append("receipt mismatch"),
    )
    with pytest.raises(ValueError, match="verification failed"):
        release_module._require_complete_release(run_dir)

    monkeypatch.setattr(
        release_module,
        "refresh_card_for_release",
        lambda _root: (_ for _ in ()).throw(RuntimeError("card failed")),
    )
    with pytest.raises(ValueError, match="refusing to release"):
        release_module._update_card_safely(run_dir)

    monkeypatch.setattr(release_module, "compute_data_manifest_sha256", lambda _root: "actual")
    with pytest.raises(ValueError, match="data manifest changed"):
        release_module._require_data_identity(run_dir, "expected")


def test_release_accepts_legacy_card_receipt_before_refresh(run_dir: Path) -> None:
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["card_contract_version"] = 1
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    assert release_module._require_complete_release(run_dir) == data_manifest_sha256(run_dir)


def test_release_publish_helpers_preserve_dry_run_noop_and_upload_contract(
    run_dir: Path,
) -> None:
    files = build_card_release_plan(run_dir)
    uploads: list[tuple[tuple[object, ...], dict[str, object]]] = []
    verifications: list[tuple[str, tuple[ReleasedFile, ...]]] = []

    def uploader(*args: object, **kwargs: object) -> None:
        uploads.append((args, kwargs))

    def verifier(repo_id: str, files: tuple[ReleasedFile, ...]) -> str:
        verifications.append((repo_id, files))
        return "uploaded-revision"

    result = release_module._upload_and_verify(
        run_dir,
        repo_id=DEFAULT_HF_DATASET,
        repo_kind="dataset",
        files=files,
        uploader=uploader,
        verifier=verifier,
    )
    assert result.revision == "uploaded-revision"
    assert result.uploaded is True
    assert uploads == [
        (
            (run_dir,),
            {"repo_id": DEFAULT_HF_DATASET, "repo_kind": "dataset", "files": files},
        )
    ]
    assert verifications == [(DEFAULT_HF_DATASET, files)]

    noop = release_module._publish(
        run_dir,
        repo_id=DEFAULT_HF_DATASET,
        repo_kind="dataset",
        files=files,
        uploader=lambda *_args, **_kwargs: pytest.fail("no-op must not upload"),
        verifier=verifier,
        remote_checker=lambda repo_id, files: "current-revision",
    )
    assert noop.revision == "current-revision"
    assert noop.uploaded is False
    assert (
        release_module._publish_if_requested(
            run_dir,
            apply=False,
            repo_id=DEFAULT_HF_DATASET,
            repo_kind="dataset",
            files=files,
            uploader=uploader,
            verifier=verifier,
            remote_checker=None,
        )
        is None
    )


def test_release_upload_folder_includes_exact_repository_and_commit(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(upload_folder=lambda **kwargs: captured.update(kwargs)),
    )

    release_module._upload_card_files(
        run_dir,
        repo_id=DEFAULT_HF_DATASET,
        repo_kind="dataset",
        files=build_card_release_plan(run_dir),
    )

    assert captured["repo_id"] == DEFAULT_HF_DATASET
    assert captured["repo_type"] == "dataset"
    assert captured["commit_message"] == "Publish dataset card and polygon statistics report"


_REMOTE_VERIFIERS = (
    "_verify_remote_data_identity",
    "_verify_remote_parquet_data_identity",
    "_verify_remote_text_population_identity",
)


def _recording_remote_checker(
    monkeypatch: pytest.MonkeyPatch, *, changed: tuple[str, ...]
) -> tuple[dict[str, tuple[Any, ...]], list[Any]]:
    """Stub the remote calls while recording the exact arguments they receive.

    Recording the arguments is the point: a stub that swallows ``*args`` lets
    a swapped or dropped argument through, which is how this function's whole
    orchestration went unverified.
    """
    import huggingface_hub

    calls: dict[str, tuple[Any, ...]] = {}
    created: list[Any] = []

    class RecordingApi:
        def __init__(self, token: object = None) -> None:
            self.token = token
            created.append(self)

    def record(name: str, result: object = None):
        def hook(*args: object) -> object:
            calls[name] = args
            return result

        return hook

    monkeypatch.setattr(huggingface_hub, "HfApi", RecordingApi)
    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: "secret-token")
    monkeypatch.setattr(release_module, "_remote_revision", record("_remote_revision", "rev-1"))
    for name in _REMOTE_VERIFIERS:
        monkeypatch.setattr(release_module, name, record(name))
    monkeypatch.setattr(
        release_module, "_remote_changed_files", record("_remote_changed_files", changed)
    )
    return calls, created


def test_a_missing_credential_refuses_before_any_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: "")

    with pytest.raises(
        ValueError, match=r"^release requires Hugging Face environment/local credentials$"
    ):
        release_module._default_remote_checker(DEFAULT_HF_DATASET, ())


def test_the_remote_check_threads_every_argument_to_every_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = ReleasedFile(relative_path="README.md", sha256="a" * 64, size_bytes=3)
    files = (item,)
    calls, created = _recording_remote_checker(monkeypatch, changed=())

    checked = release_module._default_remote_checker("owner/repo", files)

    assert len(created) == 1
    api = created[0]
    assert api.token == "secret-token"
    assert calls["_remote_revision"] == (api, "owner/repo")
    for name in _REMOTE_VERIFIERS:
        assert calls[name] == (api, "owner/repo", "rev-1", files), name
    assert calls["_remote_changed_files"] == (api, "owner/repo", "rev-1", files)
    assert checked == release_module._RemoteCheck("rev-1")


def test_an_unchanged_remote_returns_its_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    _recording_remote_checker(monkeypatch, changed=())

    checked = release_module._default_remote_checker("owner/repo", ())

    assert checked == release_module._RemoteCheck("rev-1")
    assert checked.changed_files == ()


def test_a_changed_remote_withholds_the_revision_and_names_the_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _recording_remote_checker(monkeypatch, changed=("README.md", "dataset.yaml"))

    checked = release_module._default_remote_checker("owner/repo", ())

    assert checked == release_module._RemoteCheck(None, ("README.md", "dataset.yaml"))


def _unbound_message(monkeypatch: pytest.MonkeyPatch, count: int) -> str:
    """Raise the unbound-shard error for ``count`` shards and return its text."""
    paths = [Path(f"/outside/shard{index}.parquet") for index in range(count)]
    monkeypatch.setattr(release_module, "_release_inventory", lambda _root: set())
    monkeypatch.setattr(
        release_module, "_unbound_text_population_paths", lambda _paths, _inventory: paths
    )

    with pytest.raises(ValueError) as raised:
        release_module._raise_for_unbound_text_population_paths(Path("/run"), [])
    return str(raised.value)


def test_unbound_shards_are_named_in_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _unbound_message(monkeypatch, 1) == (
        "release requires text population shards in the release artifact inventory; "
        "unbound shards found: /outside/shard0.parquet"
    )


def test_exactly_three_unbound_shards_are_listed_without_an_ellipsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three is the boundary: all three fit, so nothing is elided."""
    assert _unbound_message(monkeypatch, 3) == (
        "release requires text population shards in the release artifact inventory; "
        "unbound shards found: /outside/shard0.parquet, /outside/shard1.parquet, "
        "/outside/shard2.parquet"
    )


def test_more_than_three_unbound_shards_are_truncated_with_an_ellipsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _unbound_message(monkeypatch, 4) == (
        "release requires text population shards in the release artifact inventory; "
        "unbound shards found: /outside/shard0.parquet, /outside/shard1.parquet, "
        "/outside/shard2.parquet..."
    )


def test_bound_shards_raise_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_module, "_release_inventory", lambda _root: set())
    monkeypatch.setattr(release_module, "_unbound_text_population_paths", lambda *_args: [])

    assert release_module._raise_for_unbound_text_population_paths(Path("/run"), []) is None

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
from osm_polygon_website_tag.reporting.artifact_inventory import data_manifest_sha256
from osm_polygon_website_tag.reporting.finalize import finalize_run
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


def test_release_rebuilds_stale_metadata_before_verification(run_dir: Path) -> None:
    (run_dir / "README.md").write_text("stale", encoding="utf-8")
    (run_dir / "stats.json").write_text("{}\n", encoding="utf-8")

    report = release_card_and_stats(run_dir, confirm_repo=DEFAULT_HF_DATASET)

    assert report.recomputed is True
    assert (run_dir / "README.md").read_text(encoding="utf-8").startswith("---\n")
    assert '"schema_version"' in (run_dir / "stats.json").read_text(encoding="utf-8")


def test_unverified_run_is_refused(run_dir: Path) -> None:
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    receipt_before = receipt_path.read_bytes()
    (run_dir / "polygons" / "monaco-latest.parquet").unlink()

    with pytest.raises(ValueError, match="refusing to release"):
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
    assert second.uploaded is False
    assert second.no_op is True
    assert second.revision == "remote-revision"
    assert len(uploader.calls) == 1
    assert len(verifier_calls) == 1


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
            assert filename == "manifests/completion_receipt.json"
            assert revision == "remote-revision"
            assert repo_type == "dataset"
            return str(remote_receipt)

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
    receipt = remote_root / "manifests" / "completion_receipt.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps({"data_manifest_sha256": files[0].data_manifest_sha256}),
        encoding="utf-8",
    )

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

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.release.resolve_hf_token",
        lambda: "token",
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=_Api))

    assert default_hub_verifier(DEFAULT_HF_DATASET, files) == "remote-revision"


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
    assert captured["allow_patterns"] == ["README.md", "stats.json"]


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

    def refuse_remote(*_args: object, **_kwargs: object) -> str:
        raise release_module._RemoteArtifactMismatchError("missing metadata")

    monkeypatch.setattr(release_module, "default_hub_verifier", refuse_remote)
    assert release_module._default_remote_checker(DEFAULT_HF_DATASET, (item,)) is None


def test_credentialed_release_uploader_requires_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: None)
    with pytest.raises(ValueError, match="Hugging Face"):
        release_module._require_credentialed_uploader()

    monkeypatch.setattr(release_module, "resolve_hf_token", lambda: "token")
    assert release_module._require_credentialed_uploader() is release_module._upload_card_files

"""Contract tests for remote Hugging Face Hub release identity verification."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.publishing.receipt_helpers import recording_receipt_reads

import osm_polygon_website_tag.publishing.remote_identity as remote_identity
from osm_polygon_website_tag.publishing.release import (
    ReleasedFile,
    build_card_release_plan,
)
from osm_polygon_website_tag.publishing.remote_identity import default_hub_verifier
from osm_polygon_website_tag.reporting.artifact_inventory import (
    data_manifest_sha256,
    hash_file,
    publishable_paths,
)
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET

_REMOTE_ENTRIES: dict[str, dict[str, int | str]] = {
    "remote/a.parquet": {"size_bytes": 3, "sha256": "ra"},
    "a.parquet": {"size_bytes": 4, "sha256": "la"},
}


_UNBOUNDED = r"^remote Parquet entry lacks bounded identity: data/a\.parquet$"


def _released(identity: str | None) -> ReleasedFile:
    return ReleasedFile(
        relative_path="README.md", sha256="f", size_bytes=1, data_manifest_sha256=identity
    )


def _remote_identity_hub_api(
    remote_root: Path, parquet_entries: list[SimpleNamespace]
) -> type[object]:
    """Build the read-only Hub double shared by matching and tampered releases."""

    class RemoteApi:
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

    return RemoteApi


def test_remote_verification_refuses_data_identity_mismatch(
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
    remote_receipt = remote_root / "manifests" / "completion_receipt.json"
    remote_receipt.parent.mkdir(parents=True, exist_ok=True)
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

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.remote_identity.resolve_hf_token",
        lambda: "token",
    )
    api = _remote_identity_hub_api(remote_root, parquet_entries)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=api))

    with pytest.raises(ValueError, match="data identity"):
        default_hub_verifier(DEFAULT_HF_DATASET, files)


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

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.remote_identity.resolve_hf_token",
        lambda: "token",
    )
    api = _remote_identity_hub_api(remote_root, parquet_entries)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=api))

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

    api = _remote_identity_hub_api(remote_root, parquet_entries)(token="test-token")
    with pytest.raises(ValueError, match="data manifest"):
        remote_identity._verify_remote_data_identity(
            api, DEFAULT_HF_DATASET, "remote-revision", files
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

    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.remote_identity.resolve_hf_token",
        lambda: "token",
    )
    api = _remote_identity_hub_api(remote_root, parquet_entries)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=api))

    with pytest.raises(ValueError, match="data manifest"):
        default_hub_verifier(DEFAULT_HF_DATASET, files)


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

    assert remote_identity._remote_changed_files(Api(), "dataset", "revision", files) == (
        "README.md",
    )


def test_release_private_identity_and_remote_adapters_are_exact(
    run_dir: Path,
    tmp_path: Path,
) -> None:
    files = build_card_release_plan(run_dir)
    identity = data_manifest_sha256(run_dir)
    assert remote_identity._expected_data_identity(files) == identity

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
    assert remote_identity._remote_revision(api, DEFAULT_HF_DATASET) == "revision"
    remote_identity._verify_remote_size(api, DEFAULT_HF_DATASET, "revision", files[0])
    remote_identity._verify_remote_digest(api, DEFAULT_HF_DATASET, "revision", files[0])
    assert remote_identity._remote_data_identity(api, DEFAULT_HF_DATASET, "revision") == identity
    remote_identity._verify_remote_data_identity(api, DEFAULT_HF_DATASET, "revision", files)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (
            {"path": "a.parquet"},
            {"path": "text_population/a.parquet", "size_bytes": 4, "sha256": "la"},
        ),
        (
            {"path": "a.parquet", "remote_path": "remote/a.parquet"},
            {"path": "text_population/a.parquet", "size_bytes": 3, "sha256": "ra"},
        ),
    ],
)
def test_remote_text_population_entry_prefers_the_remote_path(
    item: dict[str, str], expected: dict[str, object]
) -> None:
    assert remote_identity._remote_text_population_entry(item, _REMOTE_ENTRIES) == expected


def test_remote_text_population_entry_rejects_a_missing_logical_path() -> None:
    with pytest.raises(
        remote_identity._RemoteDataMismatchError,
        match=r"^remote text population manifest path is invalid$",
    ):
        remote_identity._remote_text_population_entry({"remote_path": "a.parquet"}, _REMOTE_ENTRIES)


@pytest.mark.parametrize(
    "entry",
    [
        SimpleNamespace(lfs=SimpleNamespace(sha256="abc")),
        SimpleNamespace(size=3),
        SimpleNamespace(size=3, lfs=SimpleNamespace()),
        SimpleNamespace(size=3, lfs=SimpleNamespace(sha256="")),
        SimpleNamespace(size=3, lfs=SimpleNamespace(sha256=5)),
        SimpleNamespace(size=None, lfs=SimpleNamespace(sha256="abc")),
    ],
)
def test_remote_parquet_identity_requires_a_size_and_a_digest(entry: object) -> None:
    with pytest.raises(remote_identity._RemoteArtifactMismatchError, match=_UNBOUNDED):
        remote_identity._validated_remote_parquet_identity(entry, "data/a.parquet")


def test_remote_parquet_identity_names_an_unknown_path() -> None:
    with pytest.raises(
        remote_identity._RemoteArtifactMismatchError,
        match=r"^remote Parquet entry lacks bounded identity: <unknown>$",
    ):
        remote_identity._validated_remote_parquet_identity(SimpleNamespace(), "")


def test_remote_parquet_identity_projects_a_bounded_entry() -> None:
    entry = SimpleN
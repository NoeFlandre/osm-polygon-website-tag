"""Deterministic card/report release to the exact Hugging Face dataset.

The release path recomputes the dataset card, geographic map, and ``stats.json``
from the complete verified run, publishes those metadata files and the completion
receipt as one commit, and verifies the remote files afterwards. Polygon shards
and unrelated Hub files are never touched by this path.

Determinism: the card and map's geographic values are rendered from one global,
unique text-bearing identity summary of the verified run. Regeneration writes
artifacts only when their bytes would change, so a second release over an
unchanged run produces the same plan and the same remote revision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token
from osm_polygon_website_tag.reporting.artifact_inventory import (
    data_manifest_sha256 as compute_data_manifest_sha256,
)
from osm_polygon_website_tag.reporting.artifact_inventory import hash_file
from osm_polygon_website_tag.reporting.artifact_inventory import (
    parquet_manifest_sha256 as compute_parquet_manifest_sha256,
)
from osm_polygon_website_tag.reporting.card import refresh_card_for_release
from osm_polygon_website_tag.reporting.finalize import replace_receipt_atomic
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.verification.receipt import (
    verify_receipt_before_card_refresh,
)
from osm_polygon_website_tag.reporting.verify import VerificationReport, verify_release_results
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET
from osm_polygon_website_tag.runtime.run_state import STATUS_COMPLETE, load_run

CARD_RELEASE_FILES = (
    "README.md",
    "dataset.yaml",
    "stats.json",
    POLYGON_DENSITY_ASSET_REL_PATH,
    "manifests/completion_receipt.json",
)
_CARD_ARTIFACTS = ("README.md", "stats.json", "dataset.yaml", POLYGON_DENSITY_ASSET_REL_PATH)
_REMOTE_SOURCE_MANIFESTS = (
    "manifests/expected_sources.json",
    "manifests/sources.json",
)


@dataclass(frozen=True)
class ReleasedFile:
    """One published metadata file and its exact identity."""

    relative_path: str
    sha256: str
    size_bytes: int
    data_manifest_sha256: str | None = None
    parquet_manifest_sha256: str | None = None


@dataclass(frozen=True)
class CardReleaseReport:
    """Evidence for one card/report release run."""

    repo_id: str
    run_dir: str
    files: tuple[ReleasedFile, ...]
    verified_shards: tuple[str, ...]
    recomputed: bool
    published: bool
    revision: str | None
    data_manifest_sha256: str = ""
    parquet_manifest_sha256: str = ""
    uploaded: bool = False
    no_op: bool = False
    changed_files: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "files": [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "data_manifest_sha256": item.data_manifest_sha256,
                    "parquet_manifest_sha256": item.parquet_manifest_sha256,
                }
                for item in self.files
            ],
            "published": self.published,
            "recomputed": self.recomputed,
            "data_manifest_sha256": self.data_manifest_sha256,
            "parquet_manifest_sha256": self.parquet_manifest_sha256,
            "no_op": self.no_op,
            "changed_files": list(self.changed_files),
            "repo_id": self.repo_id,
            "revision": self.revision,
            "run_dir": self.run_dir,
            "uploaded": self.uploaded,
            "verified_shards": list(self.verified_shards),
        }


class HubVerifier(Protocol):
    """Confirm the released files exist remotely and return the repo revision."""

    def __call__(self, repo_id: str, files: tuple[ReleasedFile, ...]) -> str: ...


class RemoteChecker(Protocol):
    """Return the current revision when the remote already matches exactly."""

    def __call__(self, repo_id: str, files: tuple[ReleasedFile, ...]) -> str | None: ...


@dataclass(frozen=True)
class _PublicationResult:
    """Result of one apply-mode remote operation."""

    revision: str
    uploaded: bool


def _publication_report_fields(
    files: tuple[ReleasedFile, ...],
    publication: _PublicationResult | None,
) -> tuple[str | None, bool, bool, tuple[str, ...]]:
    """Return stable report fields for dry-run, upload, and no-op outcomes."""
    if publication is None:
        return None, False, False, ()
    if not publication.uploaded:
        return publication.revision, False, True, ()
    return (
        publication.revision,
        True,
        False,
        tuple(item.relative_path for item in files),
    )


class _RemoteArtifactMismatchError(ValueError):
    """The remote metadata needs the planned upload."""


class _RemoteDataMismatchError(ValueError):
    """The remote data cannot be proven identical to the local release."""


def build_card_release_plan(
    run_dir: Path | str,
    *,
    data_manifest_sha256: str | None = None,
) -> tuple[ReleasedFile, ...]:
    """Return the exact card/report files that a release would upload."""
    root = Path(run_dir)
    identity = data_manifest_sha256 or compute_data_manifest_sha256(root)
    parquet_identity = compute_parquet_manifest_sha256(root)
    items: list[ReleasedFile] = []
    for name in CARD_RELEASE_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing card release artifact: {path}")
        items.append(
            ReleasedFile(
                relative_path=name,
                sha256=hash_file(path),
                size_bytes=path.stat().st_size,
                data_manifest_sha256=identity,
                parquet_manifest_sha256=parquet_identity,
            )
        )
    return tuple(items)


def _require_exact_repo(confirm_repo: str, repo_id: str, repo_kind: str) -> None:
    if repo_id != DEFAULT_HF_DATASET:
        raise ValueError(
            f"release repository must be canonical {DEFAULT_HF_DATASET!r}; "
            f"overrides are not allowed (got {repo_id!r})"
        )
    if repo_kind != "dataset":
        raise ValueError("release repository kind must be the canonical dataset")
    if confirm_repo != repo_id:
        raise ValueError(f"repository confirmation must equal {repo_id!r} (got {confirm_repo!r})")


def _require_complete_release(root: Path) -> str:
    """Require a complete run with a completion receipt before regeneration."""
    state = load_run(root)
    if state.metadata.get("status") != STATUS_COMPLETE:
        raise ValueError("release requires a COMPLETE run")
    receipt = _completion_receipt_path(root)
    identity = _completion_data_identity(receipt)
    receipt_errors: list[str] = []
    verify_receipt_before_card_refresh(root, receipt_errors)
    if receipt_errors:
        raise ValueError(
            f"release completion receipt verification failed; refusing to release: {receipt_errors}"
        )
    return identity or compute_data_manifest_sha256(root)


def _completion_receipt_path(root: Path) -> Path:
    """Return the trusted completion receipt path."""
    receipt = root / "manifests" / "completion_receipt.json"
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError(f"release requires a completion receipt: {receipt}")
    return receipt


def _read_receipt_payload(
    receipt: Path,
    *,
    error_type: type[ValueError],
    label: str,
) -> dict[str, Any]:
    """Read a JSON completion receipt and require its object shape."""
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(f"{label} is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise error_type(f"{label} has no data identity")
    return payload


def _receipt_data_identity(
    payload: dict[str, Any],
    *,
    error_type: type[ValueError],
    label: str,
) -> str | None:
    """Return a receipt identity, accepting only the documented v1.2 omission."""
    identity = payload.get("data_manifest_sha256")
    if isinstance(identity, str) and identity:
        return identity
    if payload.get("schema_version") == "v1.2" and "data_manifest_sha256" not in payload:
        return None
    raise error_type(f"{label} has no data identity")


def _completion_data_identity(receipt: Path) -> str | None:
    """Read the data identity from a completion receipt."""
    payload = _read_receipt_payload(
        receipt,
        error_type=ValueError,
        label="release completion receipt",
    )
    return _receipt_data_identity(
        payload,
        error_type=ValueError,
        label="release completion receipt",
    )


def _require_verified(report: VerificationReport, run_dir: Path) -> None:
    if not report.ok:
        raise ValueError(f"verification failed for {run_dir}; refusing to release: {report.errors}")


def _recompute_card(run_dir: Path, expected_data_identity: str) -> bool:
    """Rebuild the card and report; return whether their bytes changed."""
    before = {name: _digest_or_none(run_dir / name) for name in _CARD_ARTIFACTS}
    _update_card_safely(run_dir)
    after = {name: _digest_or_none(run_dir / name) for name in _CARD_ARTIFACTS}
    _require_data_identity(run_dir, expected_data_identity)
    receipt_needs_data_identity = (
        _completion_data_identity(_completion_receipt_path(run_dir)) is None
    )
    if before == after and not receipt_needs_data_identity:
        return False
    replace_receipt_atomic(run_dir)
    return True


def _update_card_safely(run_dir: Path) -> None:
    """Normalize card-update failures to a release refusal."""
    try:
        refresh_card_for_release(run_dir)
    except Exception as exc:
        raise ValueError(f"verification failed for {run_dir}; refusing to release: {exc}") from exc


def _require_data_identity(run_dir: Path, expected: str) -> None:
    """Refuse to re-release a run whose data changed after completion."""
    if compute_data_manifest_sha256(run_dir) != expected:
        raise ValueError("release data manifest changed; refusing to release")


def _digest_or_none(path: Path) -> str | None:
    return hash_file(path) if path.is_file() else None


def _upload_card_files(
    run_dir: Path,
    *,
    repo_id: str,
    repo_kind: str,
    files: tuple[ReleasedFile, ...],
) -> None:
    """Upload exactly the released metadata files and nothing else."""
    from huggingface_hub import upload_folder

    upload_folder(
        repo_id=repo_id,
        repo_type=repo_kind,
        folder_path=str(run_dir),
        allow_patterns=[item.relative_path for item in files],
        commit_message="Publish dataset card and polygon statistics report",
    )


def _remote_revision(api: Any, repo_id: str) -> str:
    """Return the repository's current commit revision."""
    info = api.repo_info(repo_id, repo_type="dataset")
    revision = str(getattr(info, "sha", "") or "")
    if not revision:
        raise ValueError(f"hub repository {repo_id} returned an empty revision")
    return revision


def _verify_remote_size(api: Any, repo_id: str, revision: str, item: ReleasedFile) -> None:
    """Check the remote entry exists at ``revision`` with the expected size."""
    entries = api.get_paths_info(
        repo_id,
        paths=[item.relative_path],
        revision=revision,
        repo_type="dataset",
    )
    entry = next(iter(entries), None)
    if entry is None:
        raise _RemoteArtifactMismatchError(
            f"remote file missing in revision {revision}: {item.relative_path}"
        )
    size = getattr(entry, "size", None)
    if size is not None and int(size) != item.size_bytes:
        raise _RemoteArtifactMismatchError(
            f"remote size mismatch for {item.relative_path}: local={item.size_bytes}, remote={size}"
        )


def _verify_remote_digest(api: Any, repo_id: str, revision: str, item: ReleasedFile) -> None:
    """Check the remote content hashes to the published identity."""
    local_path = api.hf_hub_download(
        repo_id,
        item.relative_path,
        revision=revision,
        repo_type="dataset",
    )
    digest = hash_file(Path(local_path))
    if digest != item.sha256:
        raise _RemoteArtifactMismatchError(
            f"remote SHA-256 mismatch for {item.relative_path}: "
            f"local={item.sha256}, remote={digest}"
        )


def _expected_data_identity(files: tuple[ReleasedFile, ...]) -> str:
    """Return the single data identity bound to every release file."""
    identities = {item.data_manifest_sha256 for item in files}
    if len(identities) != 1:
        raise ValueError("release plan has no single data identity")
    identity = next(iter(identities))
    if identity is None:
        raise ValueError("release plan has no single data identity")
    return identity


def _expected_parquet_data_identity(files: tuple[ReleasedFile, ...]) -> str:
    """Return the single Parquet-shard identity bound to every release file."""
    identities = {item.parquet_manifest_sha256 for item in files}
    if len(identities) != 1:
        raise ValueError("release plan has no single Parquet data identity")
    identity = next(iter(identities))
    if identity is None:
        raise ValueError("release plan has no single Parquet data identity")
    return identity


def _remote_data_identity(api: Any, repo_id: str, revision: str) -> str | None:
    """Read the completion receipt's data identity at the remote revision."""
    path = api.hf_hub_download(
        repo_id,
        "manifests/completion_receipt.json",
        revision=revision,
        repo_type="dataset",
    )
    receipt = _read_receipt_payload(
        Path(path),
        error_type=_RemoteDataMismatchError,
        label="remote completion receipt",
    )
    return _receipt_data_identity(
        receipt,
        error_type=_RemoteDataMismatchError,
        label="remote completion receipt",
    )


def _remote_source_manifest_entries(
    api: Any,
    repo_id: str,
    revision: str,
) -> list[dict[str, int | str]]:
    """Read bounded identities for every remote source manifest."""
    entries: list[dict[str, int | str]] = []
    for relative_path in _REMOTE_SOURCE_MANIFESTS:
        try:
            downloaded = api.hf_hub_download(
                repo_id,
                relative_path,
                revision=revision,
                repo_type="dataset",
            )
            path = Path(downloaded)
            if not path.is_file():
                raise FileNotFoundError(path)
        except Exception as exc:
            raise _RemoteDataMismatchError(
                f"remote source manifest unavailable: {relative_path}: {exc}"
            ) from exc
        entries.append(
            {
                "path": relative_path,
                "size_bytes": path.stat().st_size,
                "sha256": hash_file(path),
            }
        )
    return entries


def _remote_data_manifest_sha256(api: Any, repo_id: str, revision: str) -> str:
    """Hash remote source manifests and Parquet metadata as one data identity."""
    entries = [
        *_remote_source_manifest_entries(api, repo_id, revision),
        *_remote_parquet_entries(api, repo_id, revision).values(),
    ]
    canonical = json.dumps(
        sorted(entries, key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_remote_data_identity(
    api: Any,
    repo_id: str,
    revision: str,
    files: tuple[ReleasedFile, ...],
) -> None:
    """Refuse a release when remote data differs from the local data manifest."""
    expected = _expected_data_identity(files)
    actual = _remote_data_manifest_sha256(api, repo_id, revision)
    if actual != expected:
        raise _RemoteDataMismatchError(
            f"remote data manifest mismatch: local={expected}, remote={actual}"
        )
    receipt_identity = _remote_data_identity(api, repo_id, revision)
    if receipt_identity is not None and receipt_identity != expected:
        raise _RemoteDataMismatchError(
            f"remote data identity mismatch: local={expected}, remote={receipt_identity}"
        )


def _remote_parquet_data_identity(api: Any, repo_id: str, revision: str) -> str:
    """Hash remote Parquet metadata without downloading potentially huge shards."""
    entries = _remote_parquet_entries(api, repo_id, revision)
    canonical = json.dumps(
        sorted(entries.values(), key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _remote_parquet_entries(
    api: Any,
    repo_id: str,
    revision: str,
) -> dict[str, dict[str, int | str]]:
    """Collect every remote Parquet entry from the managed data directories."""
    list_repo_tree = getattr(api, "list_repo_tree", None)
    if not callable(list_repo_tree):
        raise _RemoteArtifactMismatchError("remote API cannot inspect Parquet shards")
    entries: dict[str, dict[str, int | str]] = {}
    for directory in ("polygons", "analysis_observations", "rejections", "analysis"):
        remote_entries = _remote_parquet_tree(
            list_repo_tree,
            repo_id,
            directory,
            revision,
        )
        _merge_remote_parquet_entries(entries, remote_entries)
    if not entries:
        raise _RemoteArtifactMismatchError("remote Parquet inventory is empty")
    return entries


def _remote_parquet_tree(
    list_repo_tree: Any,
    repo_id: str,
    directory: str,
    revision: str,
) -> list[Any]:
    """Read one remote Parquet directory through the Hub tree API."""
    try:
        return list(
            list_repo_tree(
                repo_id,
                path_in_repo=directory,
                recursive=True,
                expand=False,
                revision=revision,
                repo_type="dataset",
            )
        )
    except Exception as exc:
        raise _RemoteArtifactMismatchError(
            f"remote Parquet inventory unavailable for {directory}: {exc}"
        ) from exc


def _merge_remote_parquet_entries(
    entries: dict[str, dict[str, int | str]],
    remote_entries: list[Any],
) -> None:
    """Validate and merge one remote tree response."""
    for entry in remote_entries:
        identity = _remote_parquet_entry_identity(entry)
        if identity is None:
            continue
        path = str(identity["path"])
        if path in entries:
            raise _RemoteArtifactMismatchError(f"remote Parquet entry is duplicated: {path}")
        entries[path] = identity


def _remote_parquet_entry_identity(entry: Any) -> dict[str, int | str] | None:
    """Return a bounded identity for a remote Parquet file, if it is one."""
    path = str(getattr(entry, "path", "") or "")
    if not path.endswith(".parquet"):
        return None
    return _validated_remote_parquet_identity(entry, path)


def _validated_remote_parquet_identity(
    entry: Any,
    path: str,
) -> dict[str, int | str]:
    """Validate size and LFS SHA-256 metadata for one remote Parquet file."""
    size = getattr(entry, "size", None)
    lfs = getattr(entry, "lfs", None)
    digest = getattr(lfs, "sha256", None)
    if size is None or not isinstance(digest, str) or not digest:
        raise _RemoteArtifactMismatchError(
            f"remote Parquet entry lacks bounded identity: {path or '<unknown>'}"
        )
    return {"path": path, "size_bytes": int(size), "sha256": digest}


def _verify_remote_parquet_data_identity(
    api: Any,
    repo_id: str,
    revision: str,
    files: tuple[ReleasedFile, ...],
) -> None:
    """Refuse remote data whose bounded Parquet metadata differs from local."""
    expected = _expected_parquet_data_identity(files)
    actual = _remote_parquet_data_identity(api, repo_id, revision)
    if actual != expected:
        raise _RemoteDataMismatchError(
            f"remote Parquet data identity mismatch: local={expected}, remote={actual}"
        )


def default_hub_verifier(repo_id: str, files: tuple[ReleasedFile, ...]) -> str:
    """Confirm each released file's remote identity and return the revision."""
    from huggingface_hub import HfApi

    token = resolve_hf_token()
    if not token:
        raise ValueError("release requires Hugging Face environment/local credentials")
    api = HfApi(token=token)
    revision = _remote_revision(api, repo_id)
    _verify_remote_data_identity(api, repo_id, revision, files)
    _verify_remote_parquet_data_identity(api, repo_id, revision, files)
    for item in files:
        _verify_remote_size(api, repo_id, revision, item)
        _verify_remote_digest(api, repo_id, revision, item)
    return revision


def _default_remote_checker(repo_id: str, files: tuple[ReleasedFile, ...]) -> str | None:
    """Return a revision for an exact remote no-op, or request an upload."""
    try:
        return default_hub_verifier(repo_id, files)
    except _RemoteArtifactMismatchError:
        return None


def _require_credentialed_uploader() -> Callable[..., None]:
    """Return the real uploader, refusing when no credential is configured."""
    if not resolve_hf_token():
        raise ValueError("release requires Hugging Face environment/local credentials")
    return _upload_card_files


def _upload_and_verify(
    run_dir: Path,
    *,
    repo_id: str,
    repo_kind: str,
    files: tuple[ReleasedFile, ...],
    uploader: Callable[..., None] | None,
    verifier: HubVerifier | None,
) -> _PublicationResult:
    """Upload the planned files and verify the resulting remote revision."""
    if uploader is None:
        uploader = _require_credentialed_uploader()
    upload = uploader
    upload(run_dir, repo_id=repo_id, repo_kind=repo_kind, files=files)
    if verifier is None:
        verifier = default_hub_verifier
    revision = verifier(repo_id, files)
    if not revision:
        raise ValueError("hub verification returned an empty revision")
    return _PublicationResult(revision, uploaded=True)


def _publish(
    run_dir: Path,
    *,
    repo_id: str,
    repo_kind: str,
    files: tuple[ReleasedFile, ...],
    uploader: Callable[..., None] | None,
    verifier: HubVerifier | None,
    remote_checker: RemoteChecker | None,
) -> _PublicationResult:
    """No-op an exact remote release, otherwise upload and verify it."""
    if remote_checker is not None:
        current = remote_checker(repo_id, files)
        if current:
            return _PublicationResult(current, uploaded=False)
    return _upload_and_verify(
        run_dir,
        repo_id=repo_id,
        repo_kind=repo_kind,
        files=files,
        uploader=uploader,
        verifier=verifier,
    )


def _publish_if_requested(
    run_dir: Path,
    *,
    apply: bool,
    repo_id: str,
    repo_kind: str,
    files: tuple[ReleasedFile, ...],
    uploader: Callable[..., None] | None,
    verifier: HubVerifier | None,
    remote_checker: RemoteChecker | None,
) -> _PublicationResult | None:
    """Keep dry-run local and route apply mode through the remote gate."""
    if not apply:
        return None
    checker = remote_checker
    if checker is None and uploader is None and verifier is None:
        checker = _default_remote_checker
    return _publish(
        run_dir,
        repo_id=repo_id,
        repo_kind=repo_kind,
        files=files,
        uploader=uploader,
        verifier=verifier,
        remote_checker=checker,
    )


def release_card_and_stats(
    run_dir: Path | str,
    *,
    confirm_repo: str,
    repo_id: str = DEFAULT_HF_DATASET,
    repo_kind: str = "dataset",
    apply: bool = False,
    uploader: Callable[..., None] | None = None,
    verifier: HubVerifier | None = None,
    remote_checker: RemoteChecker | None = None,
) -> CardReleaseReport:
    """Recompute, verify, and optionally publish the card and statistics report.

    ``apply=False`` performs the full local compute-and-verify pass and returns
    the exact plan that would be uploaded, without touching the network.
    """
    root = Path(run_dir)
    _require_exact_repo(confirm_repo, repo_id, repo_kind)
    expected_data_identity = _require_complete_release(root)
    recomputed = _recompute_card(root, expected_data_identity)
    report = verify_release_results(root)
    _require_verified(report, root)
    identity = compute_data_manifest_sha256(root)
    files = build_card_release_plan(root, data_manifest_sha256=identity)
    publication = _publish_if_requested(
        root,
        apply=apply,
        repo_id=repo_id,
        repo_kind=repo_kind,
        files=files,
        uploader=uploader,
        verifier=verifier,
        remote_checker=remote_checker,
    )
    revision, uploaded, no_op, changed_files = _publication_report_fields(files, publication)
    return CardReleaseReport(
        repo_id=repo_id,
        run_dir=str(root),
        files=files,
        verified_shards=tuple(report.checked_shards),
        recomputed=recomputed,
        published=apply,
        revision=revision,
        data_manifest_sha256=identity,
        parquet_manifest_sha256=compute_parquet_manifest_sha256(root),
        uploaded=uploaded,
        no_op=no_op,
        changed_files=changed_files,
    )


__all__ = [
    "CARD_RELEASE_FILES",
    "CardReleaseReport",
    "ReleasedFile",
    "build_card_release_plan",
    "default_hub_verifier",
    "release_card_and_stats",
]

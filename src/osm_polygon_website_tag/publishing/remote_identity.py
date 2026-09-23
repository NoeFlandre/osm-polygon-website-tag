"""Remote Hugging Face Hub identity verification for card/report releases.

Checks that the exact Hub revision holds the same source manifests, bounded
Parquet metadata, text-population shards, and card files as the local release
plan. ``release`` orchestrates these checks; this module never uploads.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token
from osm_polygon_website_tag.reporting.artifact_inventory import hash_file

if TYPE_CHECKING:
    from osm_polygon_website_tag.publishing.release import ReleasedFile


_REMOTE_SOURCE_MANIFESTS = (
    "manifests/expected_sources.json",
    "manifests/sources.json",
)


class _RemoteArtifactMismatchError(ValueError):
    """The remote metadata needs the planned upload."""


class _RemoteDataMismatchError(ValueError):
    """The remote data cannot be proven identical to the local release."""


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


def _expected_text_population_entries(
    files: tuple[ReleasedFile, ...],
) -> tuple[tuple[str, int, str], ...]:
    """Return the selected text-shard identities bound to a release plan."""
    entries = {item.text_population_entries for item in files}
    if len(entries) != 1:
        raise ValueError("release plan has no single text population manifest")
    return next(iter(entries))


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
    parquet_entries = _remote_parquet_entries(api, repo_id, revision)
    entries = [
        *_remote_source_manifest_entries(api, repo_id, revision),
        *parquet_entries.values(),
        *_remote_text_population_manifest_entries(api, repo_id, revision, parquet_entries),
    ]
    canonical = json.dumps(
        sorted(entries, key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _remote_text_population_manifest_entries(
    api: Any,
    repo_id: str,
    revision: str,
    parquet_entries: dict[str, dict[str, int | str]],
) -> list[dict[str, int | str]]:
    """Project the receipt-bound reducer inputs onto remote published shards."""
    payload = _remote_text_population_receipt(api, repo_id, revision)
    expected = payload.get("text_population_manifest")
    if not isinstance(expected, list):
        return []
    return [_remote_text_population_entry(item, parquet_entries) for item in expected]


def _remote_text_population_receipt(api: Any, repo_id: str, revision: str) -> dict[str, Any]:
    """Download and validate the remote completion receipt."""
    try:
        downloaded = api.hf_hub_download(
            repo_id,
            "manifests/completion_receipt.json",
            revision=revision,
            repo_type="dataset",
        )
        payload = _read_receipt_payload(
            Path(downloaded),
            error_type=_RemoteDataMismatchError,
            label="remote completion receipt",
        )
    except _RemoteDataMismatchError:
        raise
    except Exception as exc:
        raise _RemoteDataMismatchError(
            f"remote completion receipt unavailable for text population identity: {exc}"
        ) from exc
    return payload


def _remote_text_population_entry(
    item: Any,
    parquet_entries: dict[str, dict[str, int | str]],
) -> dict[str, int | str]:
    """Project one receipt-bound text shard onto its remote Parquet identity."""
    if not isinstance(item, dict):
        raise _RemoteDataMismatchError("remote text population manifest entry is invalid")
    logical = item.get("path")
    remote_path = item.get("remote_path", logical)
    if not isinstance(logical, str) or not isinstance(remote_path, str):
        raise _RemoteDataMismatchError("remote text population manifest path is invalid")
    remote = parquet_entries.get(remote_path)
    if remote is None:
        raise _RemoteDataMismatchError(f"remote text population shard missing: {remote_path}")
    return {
        "path": f"text_population/{logical}",
        "size_bytes": remote["size_bytes"],
        "sha256": remote["sha256"],
    }


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
            f"remote data identity mismatch (data manifest): local={expected}, remote={actual}"
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


def _verify_remote_text_population_identity(
    api: Any,
    repo_id: str,
    revision: str,
    files: tuple[ReleasedFile, ...],
) -> None:
    """Independently verify every selected text shard exists remotely unchanged."""
    expected = _expected_text_population_entries(files)
    if not expected:
        return
    remote = _remote_parquet_entries(api, repo_id, revision)
    for path, size_bytes, digest in expected:
        _verify_remote_text_population_entry(remote, path, size_bytes, digest)


def _verify_remote_text_population_entry(
    remote: dict[str, dict[str, int | str]],
    path: str,
    size_bytes: int,
    digest: str,
) -> None:
    """Verify one selected text shard against bounded remote metadata."""
    actual = remote.get(path)
    if actual is None:
        raise _RemoteDataMismatchError(f"remote text population shard missing: {path}")
    if actual["size_bytes"] != size_bytes or actual["sha256"] != digest:
        raise _RemoteDataMismatchError(f"remote text population shard mismatch: {path}")


def _remote_changed_files(
    api: Any,
    repo_id: str,
    revision: str,
    files: tuple[ReleasedFile, ...],
) -> tuple[str, ...]:
    """Return only card files whose remote bytes differ from the local plan."""
    changed: list[str] = []
    for item in files:
        try:
            _verify_remote_size(api, repo_id, revision, item)
            _verify_remote_digest(api, repo_id, revision, item)
        except _RemoteArtifactMismatchError:
            changed.append(item.relative_path)
    return tuple(changed)


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
    _verify_remote_text_population_identity(api, repo_id, revision, files)
    for item in files:
        _verify_remote_size(api, repo_id, revision, item)
        _verify_remote_digest(api, repo_id, revision, item)
    return revision

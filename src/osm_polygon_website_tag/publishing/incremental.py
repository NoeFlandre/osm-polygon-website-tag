"""Small, resumable upload plans for one enriched polygon shard."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from osm_polygon_website_tag.publishing.publish import _upload_folder
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geographic.models import MAP_CONTRACT_VERSION
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET
from osm_polygon_website_tag.runtime.run_state import atomic_write_json, hash_shard

# ---------------------------------------------------------------------------
# On-disk checkpoint contract (schema v2)
# ---------------------------------------------------------------------------
#
# ``uploaded_polygons.json`` records operational resume state. The current
# shape is ``CheckpointV2`` (see below):
#
# * ``schema_version`` is the string literal ``"v2"`` (typed as
#   :data:`typing.Literal["v2"]`). Legacy checkpoints that omit the key
#   are migrated silently only when every entry is well-formed.
# * ``global_bundle`` carries per-card-asset SHA-256 hashes (lowercase,
#   64-character hex) plus the ``map_contract_version`` integer. Empty or
#   partial bundles are valid because either marker may be absent at
#   intermediate stages.
# * ``sources`` maps ``<name>.osm.pbf`` filenames to
#   ``{"polygon_sha256": <hex>}`` records.
#
# Any malformed entry raises
# ``ValueError("invalid uploaded polygon checkpoint: <reason>")`` and the
# checkpoint file is rewritten only after reconciliation succeeds. The
# checkpoint file is excluded from the completion receipt and from the
# publish plan; remote SHA-256 hashes are authoritative during apply-mode
# reconciliation.


_CHECKPOINT_SCHEMA_VERSION = "v2"
_SOURCE_FILENAME_SUFFIX = ".osm.pbf"
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# Known keys for ``global_bundle``. Any other key is rejected to keep the
# typed-checkpoint contract closed and reviewer-auditable.
_GLOBAL_BUNDLE_HASH_KEYS: frozenset[str] = frozenset(
    {"readme_sha256", "dataset_yaml_sha256", "map_sha256"}
)
# Known fields for a per-source entry. ``polygon_sha256`` is the only
# tracked field today; future fields expand this set.
_SOURCE_ENTRY_KEYS: frozenset[str] = frozenset({"polygon_sha256"})


class _SourceCheckpointEntry(TypedDict):
    polygon_sha256: str


class _GlobalBundleStateV2(TypedDict, total=False):
    """Partial card-asset SHA-256 hashes plus the map contract version.

    Every key is optional: an empty ``global_bundle`` is the legitimate
    default state. Keys outside the documented set are rejected at the
    validation boundary so unknown entries cannot silently survive a
    round-trip.
    """

    readme_sha256: str
    dataset_yaml_sha256: str
    map_sha256: str
    map_contract_version: int


class CheckpointV2(TypedDict):
    schema_version: Literal["v2"]
    global_bundle: _GlobalBundleStateV2
    sources: dict[str, _SourceCheckpointEntry]


def _invalid(reason: str) -> ValueError:
    return ValueError(f"invalid uploaded polygon checkpoint: {reason}")


def _set_typed_dict(bundle: _GlobalBundleStateV2, key: str, value: object) -> None:
    """Assign a key/value pair into a TypedDict whose keys are not all
    literal-known at the call site.

    Using ``dict.__setitem__`` bypasses static checks that demand a string
    literal key while preserving the TypedDict's runtime shape. The
    runtime dict shape is the only thing TypedDict guards.
    """
    dict.__setitem__(cast("dict[str, object]", bundle), key, value)


def _validate_hex_sha256(value: object, *, field: str) -> str:
    """Return ``value`` if it is a 64-character lowercase hex SHA-256.

    Centralises every SHA-256 validation site so callers never have to
    open-code the same regex. The error message references ``field`` so
    remote reconciliation failures can be located quickly.
    """
    if not isinstance(value, str) or not _HEX_SHA256_RE.fullmatch(value):
        raise _invalid(f"{field} must be a 64-character lowercase hex SHA-256 string")
    return value


def _validate_global_bundle(value: object) -> _GlobalBundleStateV2:
    if not isinstance(value, dict):
        raise _invalid("global_bundle must be a JSON object")
    bundle: _GlobalBundleStateV2 = {}
    for key, val in value.items():
        if not isinstance(key, str):
            raise _invalid(f"global_bundle key {key!r} must be a string")
        if key in _GLOBAL_BUNDLE_HASH_KEYS:
            sha = _validate_hex_sha256(val, field=f"global_bundle[{key!r}]")
            _set_typed_dict(bundle, key, sha)
        elif key == "map_contract_version":
            # Reject booleans explicitly; ``bool`` subclasses ``int`` in
            # Python so an ``isinstance(val, int)`` check would otherwise
            # let ``True`` masquerade as a contract version.
            if isinstance(val, bool) or not isinstance(val, int):
                raise _invalid(
                    f"global_bundle[{key!r}] must be a non-bool integer, got {type(val).__name__}"
                )
            _set_typed_dict(bundle, key, val)
        else:
            raise _invalid(f"global_bundle has unknown field {key!r}")
    return bundle


def _validate_source_entry(source_name: str, value: object) -> _SourceCheckpointEntry:
    if not isinstance(value, dict):
        raise _invalid(f"sources[{source_name!r}] must be a JSON object")
    sha = _validate_hex_sha256(
        value.get("polygon_sha256"), field=f"sources[{source_name!r}].polygon_sha256"
    )
    unknown = sorted(str(extra) for extra in value if extra not in _SOURCE_ENTRY_KEYS)
    if unknown:
        raise _invalid(f"sources[{source_name!r}] has unknown field(s) {unknown!r}")
    return {"polygon_sha256": sha}


def _validate_sources_v2(value: object) -> dict[str, _SourceCheckpointEntry]:
    if not isinstance(value, dict):
        raise _invalid("sources must be a JSON object")
    sources: dict[str, _SourceCheckpointEntry] = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            raise _invalid(f"sources key {key!r} must be a string")
        if not key.endswith(_SOURCE_FILENAME_SUFFIX):
            raise _invalid(f"sources key {key!r} must end with '{_SOURCE_FILENAME_SUFFIX}'")
        sources[key] = _validate_source_entry(key, entry)
    return sources


def _validate_legacy_sources(raw: dict[object, object]) -> dict[str, _SourceCheckpointEntry]:
    """Migrate a legacy flat dict to the v2 ``sources`` mapping.

    Valid legacy entries (string ``<name>.osm.pbf`` key, dict value, valid
    64-character hex ``polygon_sha256``) are migrated. Anything else
    raises :class:`ValueError`. The caller selects this migration path only
    when the top-level ``schema_version`` key is absent; an explicit
    ``schema_version`` value is rejected by ``_parse_checkpoint``.
    """
    sources: dict[str, _SourceCheckpointEntry] = {}
    for key, value in raw.items():
        if key == "schema_version":
            raise _invalid("legacy checkpoint must omit schema_version")
        if not isinstance(key, str):
            raise _invalid(f"legacy sources key {key!r} must be a string")
        if not key.endswith(_SOURCE_FILENAME_SUFFIX):
            raise _invalid(f"legacy sources key {key!r} must end with '{_SOURCE_FILENAME_SUFFIX}'")
        if not isinstance(value, dict):
            raise _invalid(f"legacy sources[{key!r}] must be a JSON object")
        sha = _validate_hex_sha256(
            value.get("polygon_sha256"),
            field=f"legacy sources[{key!r}].polygon_sha256",
        )
        sources[key] = {"polygon_sha256": sha}
    return sources


def _parse_checkpoint(raw: object) -> CheckpointV2:
    if not isinstance(raw, dict):
        raise _invalid("root must be a JSON object")
    # ``json.loads`` produces an untyped JSON object; this cast records the
    # runtime dictionary shape after the ``isinstance`` guard without
    # pretending that keys or values are already strings.
    raw_dict = cast("dict[object, object]", raw)
    # Distinguish a missing ``schema_version`` key (legacy migration) from
    # an explicit ``schema_version: null`` (rejected). ``"schema_version"
    # not in raw_dict`` uses dict membership rather than ``.get(...)`` so
    # the legacy and null cases diverge cleanly.
    if "schema_version" not in raw_dict:
        sources = _validate_legacy_sources(raw_dict)
        return {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "global_bundle": {},
            "sources": sources,
        }
    schema_version = raw_dict["schema_version"]
    if schema_version == _CHECKPOINT_SCHEMA_VERSION:
        return {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "global_bundle": _validate_global_bundle(raw_dict.get("global_bundle", {})),
            "sources": _validate_sources_v2(raw_dict.get("sources", {})),
        }
    raise _invalid(f"unsupported schema_version {schema_version!r}")


@dataclass(frozen=True)
class IncrementalPublishPlan:
    """Exact managed files selected for one incremental upload."""

    source_filename: str
    upload_paths: list[Path]
    shard_changed: bool
    bundle_changed: bool


def _checkpoint_path(run_dir: Path) -> Path:
    return run_dir / "manifests" / "uploaded_polygons.json"


def load_upload_checkpoint(run_dir: Path | str) -> CheckpointV2:
    """Load the resumable per-source upload checkpoint.

    Malformed JSON, encoding errors, and structural violations all raise
    :class:`ValueError` with the documented ``"invalid uploaded polygon
    checkpoint: <reason>"`` prefix so callers have a single recoverable
    failure mode.
    """
    run_dir = Path(run_dir)
    path = _checkpoint_path(run_dir)
    if not path.is_file():
        return {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "global_bundle": {},
            "sources": {},
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _invalid(f"malformed JSON ({exc.msg} at line {exc.lineno} col {exc.colno})") from exc
    except UnicodeDecodeError as exc:
        raise _invalid(f"file is not valid UTF-8 (at byte {exc.start})") from exc
    return _parse_checkpoint(raw)


def reconcile_upload_checkpoint(
    run_dir: Path | str,
    *,
    repo_id: str,
    token: str,
) -> CheckpointV2:
    """Reconcile the local upload checkpoint with exact remote shard hashes.

    An interrupted large-folder upload can commit a remote shard before the
    local checkpoint is persisted. The remote repository is therefore the
    source of truth for the set of already published polygon shards.

    Every remote SHA-256 is validated with the shared helper
    :func:`_validate_hex_sha256` **before** the checkpoint file is
    rewritten; a malformed remote hash leaves the existing
    ``uploaded_polygons.json`` byte-identical.
    """
    root = Path(run_dir)
    remote = remote_polygon_shard_hashes(repo_id=repo_id, token=token)
    # Eagerly validate every remote hash so an in-flight HTTP retry never
    # leaks a malformed value past the load boundary. Any failure here
    # aborts before the existing checkpoint is touched.
    validated_remote: dict[str, str] = {}
    for filename, sha256 in remote.items():
        if not isinstance(filename, str) or not filename.endswith(_SOURCE_FILENAME_SUFFIX):
            raise _invalid(
                f"remote source key {filename!r} must end with '{_SOURCE_FILENAME_SUFFIX}'"
            )
        validated_remote[filename] = _validate_hex_sha256(
            sha256, field=f"remote shards[{filename!r}].sha256"
        )
    local_paths = {f"{path.stem}.osm.pbf": path for path in (root / "polygons").glob("*.parquet")}
    sources: dict[str, _SourceCheckpointEntry] = {}
    for filename, remote_sha256 in validated_remote.items():
        local_path = local_paths.get(filename)
        if local_path is not None and hash_shard(local_path) == remote_sha256:
            sources[filename] = {"polygon_sha256": remote_sha256}
    checkpoint = load_upload_checkpoint(root)
    checkpoint["sources"] = sources
    _checkpoint_path(root).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_checkpoint_path(root), checkpoint)
    return checkpoint


def remote_polygon_shard_hashes(*, repo_id: str, token: str) -> dict[str, str]:
    """Return remote polygon source filenames mapped to their SHA-256 hashes.

    The returned dictionary is **not** validated here; callers must run
    :func:`_validate_hex_sha256` over each value before persisting it to
    the checkpoint. Validation is delegated to the reconciliation caller
    so the same error message is used for on-disk and remote inputs.
    """
    from huggingface_hub import HfApi
    from huggingface_hub.utils import EntryNotFoundError

    if not token:
        raise ValueError("remote shard reconciliation requires a Hugging Face token")
    hashes: dict[str, str] = {}
    api = HfApi(token=token)
    try:
        items = api.list_repo_tree(
            repo_id,
            path_in_repo="polygons",
            recursive=False,
            expand=True,
            repo_type="dataset",
        )
        for item in items:
            path = getattr(item, "path", None)
            if not isinstance(path, str) or not path.endswith(".parquet"):
                continue
            lfs = getattr(item, "lfs", None)
            sha256 = getattr(lfs, "sha256", None)
            if not isinstance(sha256, str):
                raise ValueError(f"remote polygon shard has no SHA-256 metadata: {path}")
            hashes[f"{Path(path).stem}.osm.pbf"] = sha256
    except EntryNotFoundError:
        return {}
    return hashes


def _bundle_state(run_dir: Path) -> _GlobalBundleStateV2:
    """Hash the deterministic card assets and surface their contract version.

    Returns a strongly typed :data:`_GlobalBundleStateV2` so the v2
    checkpoint contract stays closed at the publication layer.
    """
    map_path = run_dir / POLYGON_DENSITY_ASSET_REL_PATH
    if not map_path.is_file():
        raise ValueError(f"missing incremental map artifact: {map_path}")
    return {
        "readme_sha256": hash_shard(run_dir / "README.md"),
        "dataset_yaml_sha256": hash_shard(run_dir / "dataset.yaml"),
        "map_sha256": hash_shard(map_path),
        "map_contract_version": MAP_CONTRACT_VERSION,
    }


def incremental_publish_changed_shard(
    run_dir: Path | str,
    source: Path,
    *,
    repo_id: str = DEFAULT_HF_DATASET,
    repo_kind: str = "dataset",
    dry_run: bool = True,
    uploader: Callable[..., None] | None = None,
) -> IncrementalPublishPlan:
    """Upload only the stale shard and/or global card bundle."""
    root = Path(run_dir)
    source_filename = source.name
    shard = root / "polygons" / f"{source_filename.removesuffix('.osm.pbf')}.parquet"
    if not shard.is_file():
        raise FileNotFoundError(shard)
    for relative in ("README.md", "dataset.yaml", POLYGON_DENSITY_ASSET_REL_PATH):
        path = root / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing incremental artifact: {path}")

    checkpoint = load_upload_checkpoint(root)
    sources = checkpoint["sources"]
    global_bundle = checkpoint["global_bundle"]
    current_bundle = _bundle_state(root)
    current_shard_sha = hash_shard(shard)
    prior_source = sources.get(source_filename, {})
    shard_changed = prior_source.get("polygon_sha256") != current_shard_sha
    bundle_changed = any(global_bundle.get(key) != value for key, value in current_bundle.items())
    upload_paths: list[Path] = []
    if shard_changed:
        upload_paths.append(shard)
    if bundle_changed:
        upload_paths.extend(
            [
                root / "README.md",
                root / "dataset.yaml",
                root / POLYGON_DENSITY_ASSET_REL_PATH,
            ]
        )
    plan = IncrementalPublishPlan(
        source_filename=source_filename,
        upload_paths=upload_paths,
        shard_changed=shard_changed,
        bundle_changed=bundle_changed,
    )
    if dry_run or not upload_paths:
        return plan
    upload = _upload_folder if uploader is None else uploader
    upload(
        root,
        repo_id=repo_id,
        repo_kind=repo_kind,
        artifact_paths=upload_paths,
    )
    sources[source_filename] = {"polygon_sha256": current_shard_sha}
    checkpoint["global_bundle"] = current_bundle
    _checkpoint_path(root).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_checkpoint_path(root), checkpoint)
    return plan


def persist_successful_upload(run_dir: Path | str, source: Path) -> None:
    """Persist the v2 checkpoint after an externally wrapped upload succeeds."""
    root = Path(run_dir)
    checkpoint = load_upload_checkpoint(root)
    shard = root / "polygons" / f"{source.name.removesuffix('.osm.pbf')}.parquet"
    checkpoint["sources"][source.name] = {"polygon_sha256": hash_shard(shard)}
    checkpoint["global_bundle"] = _bundle_state(root)
    _checkpoint_path(root).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_checkpoint_path(root), checkpoint)


__all__ = [
    "CheckpointV2",
    "IncrementalPublishPlan",
    "incremental_publish_changed_shard",
    "load_upload_checkpoint",
    "persist_successful_upload",
    "reconcile_upload_checkpoint",
    "remote_polygon_shard_hashes",
]

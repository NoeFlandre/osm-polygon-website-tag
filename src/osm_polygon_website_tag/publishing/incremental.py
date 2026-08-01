"""Small, resumable upload plans for one enriched polygon shard."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.publishing.publish import _upload_folder
from osm_polygon_website_tag.reporting.geographic.layout import POLYGON_DENSITY_ASSET_REL_PATH
from osm_polygon_website_tag.reporting.geographic.models import MAP_CONTRACT_VERSION
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET
from osm_polygon_website_tag.runtime.run_state import atomic_write_json, hash_shard


@dataclass(frozen=True)
class IncrementalPublishPlan:
    """Exact managed files selected for one incremental upload."""

    source_filename: str
    upload_paths: list[Path]
    shard_changed: bool
    bundle_changed: bool


def _checkpoint_path(run_dir: Path) -> Path:
    return run_dir / "manifests" / "uploaded_polygons.json"


def load_upload_checkpoint(run_dir: Path | str) -> dict[str, Any]:
    """Load the resumable per-source upload checkpoint."""
    run_dir = Path(run_dir)
    path = _checkpoint_path(run_dir)
    if not path.is_file():
        return {"schema_version": "v2", "global_bundle": {}, "sources": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("invalid uploaded polygon checkpoint")
    if raw.get("schema_version") == "v2":
        global_bundle = raw.get("global_bundle", {})
        sources = raw.get("sources", {})
        if not isinstance(global_bundle, dict) or not isinstance(sources, dict):
            raise ValueError("invalid uploaded polygon checkpoint")
        if any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in sources.items()
        ):
            raise ValueError("invalid uploaded polygon checkpoint")
        return {
            "schema_version": "v2",
            "global_bundle": dict(global_bundle),
            "sources": dict(sources),
        }
    sources = {
        key: {"polygon_sha256": value.get("polygon_sha256")}
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, dict) and "polygon_sha256" in value
    }
    return {"schema_version": "v2", "global_bundle": {}, "sources": sources}


def reconcile_upload_checkpoint(
    run_dir: Path | str,
    *,
    repo_id: str,
    token: str,
) -> dict[str, Any]:
    """Reconcile the local upload checkpoint with exact remote shard hashes.

    An interrupted large-folder upload can commit a remote shard before the
    local checkpoint is persisted. The remote repository is therefore the
    source of truth for the set of already published polygon shards.
    """
    root = Path(run_dir)
    remote = remote_polygon_shard_hashes(repo_id=repo_id, token=token)
    local_paths = {f"{path.stem}.osm.pbf": path for path in (root / "polygons").glob("*.parquet")}
    sources: dict[str, dict[str, str]] = {}
    for filename, remote_sha256 in remote.items():
        local_path = local_paths.get(filename)
        if local_path is not None and hash_shard(local_path) == remote_sha256:
            sources[filename] = {"polygon_sha256": remote_sha256}
    checkpoint = load_upload_checkpoint(root)
    checkpoint["sources"] = sources
    atomic_write_json(_checkpoint_path(root), checkpoint)
    return checkpoint


def remote_polygon_shard_hashes(*, repo_id: str, token: str) -> dict[str, str]:
    """Return remote polygon source filenames mapped to their SHA-256 hashes."""
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
            if not isinstance(sha256, str) or len(sha256) != 64:
                raise ValueError(f"remote polygon shard has no SHA-256 metadata: {path}")
            hashes[f"{Path(path).stem}.osm.pbf"] = sha256
    except EntryNotFoundError:
        return {}
    return hashes


def _bundle_state(run_dir: Path) -> dict[str, str | int]:
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
    "IncrementalPublishPlan",
    "incremental_publish_changed_shard",
    "load_upload_checkpoint",
    "persist_successful_upload",
    "reconcile_upload_checkpoint",
    "remote_polygon_shard_hashes",
]

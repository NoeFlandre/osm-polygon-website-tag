"""Read-only publish plan and Hugging Face upload helpers.

Two distinct operations are exposed:

* :func:`build_publish_plan` -- read-only: returns a manifest of every
  artifact that *would* be uploaded. Never touches the network.
* :func:`publish_to_hf` -- uploads the artifacts to a Hugging Face
  dataset repository, **only after** running the verifier. Refuses
  to upload if verification fails. Never creates the remote
  repository (use :func:`create_repo` for that). Never rebuilds the
  README card (the card on disk is the card that ships).

The CLI is a dry-run unless ``--apply`` is passed. At the Python API
level, passing ``dry_run=False`` is required for real publication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token
from osm_polygon_website_tag.reporting.verify import verify_results
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET
from osm_polygon_website_tag.runtime.run_state import STATUS_COMPLETE, load_run


@dataclass
class PublishPlan:
    """Manifest of every artifact that ``publish_to_hf`` would upload."""

    repo_id: str
    repo_kind: str
    artifact_paths: list[Path] = field(default_factory=list)
    staging_paths: list[Path] = field(default_factory=list)
    readme_path: Path | None = None


def build_publish_plan(
    run_dir: Path | str,
    *,
    repo_id: str = DEFAULT_HF_DATASET,
    repo_kind: str = "dataset",
) -> PublishPlan:
    """List every artifact that would be published from ``run_dir``.

    Reads-only. Excludes ``staging/`` (the DuckDB spill dir).
    """
    run_dir = Path(run_dir)
    plan = PublishPlan(repo_id=repo_id, repo_kind=repo_kind)
    receipt_path = run_dir / "manifests" / "completion_receipt.json"
    if receipt_path.is_file():
        import json

        receipt = json.loads(receipt_path.read_text())
        plan.artifact_paths = [run_dir / entry["path"] for entry in receipt.get("artifacts", [])]
        plan.artifact_paths.append(receipt_path)
        readme = run_dir / "README.md"
        plan.readme_path = readme if readme.is_file() else None
        return plan
    for sub in ("polygons", "analysis_observations", "rejections", "analysis", "manifests"):
        d = run_dir / sub
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file():
                if sub == "staging":
                    plan.staging_paths.append(p)
                else:
                    plan.artifact_paths.append(p)
    readme = run_dir / "README.md"
    if readme.exists():
        plan.readme_path = readme
    return plan


def publish_to_hf(
    run_dir: Path | str,
    *,
    repo_id: str = DEFAULT_HF_DATASET,
    repo_kind: str = "dataset",
    dry_run: bool = True,
) -> PublishPlan:
    """Publish ``run_dir`` to a Hugging Face repository.

    Runs the verifier first; refuses to upload on verification failure.
    Never creates the remote repository; never rebuilds the README card.

    Authentication is resolved only from environment/local Hugging Face
    credentials; no token is accepted as a function or CLI argument.
    """
    run_dir = Path(run_dir)
    report = verify_results(run_dir)
    if not report.ok:
        raise ValueError(f"verification failed for {run_dir}; refusing to publish: {report.errors}")
    plan = build_publish_plan(run_dir, repo_id=repo_id, repo_kind=repo_kind)
    if dry_run:
        return plan
    state = load_run(run_dir)
    if state.metadata.get("status") != STATUS_COMPLETE:
        raise ValueError("publication requires a COMPLETE run")
    token = resolve_hf_token()
    if not token:
        raise ValueError("publish requires Hugging Face environment/local credentials")
    _upload_folder(
        run_dir,
        repo_id=repo_id,
        repo_kind=repo_kind,
        artifact_paths=plan.artifact_paths,
    )
    return plan


def create_repo(
    *,
    repo_id: str,
    repo_kind: str = "dataset",
    exist_ok: bool = False,
) -> str:
    """Create a remote HF repository. Explicit operation; never implicit.

    Returns the repo_id on success. Refuses to overwrite unless
    ``exist_ok=True``. Requires a token when ``dry_run`` is implicit.
    """
    token = resolve_hf_token()
    if not token:
        raise ValueError("create-repo requires Hugging Face environment/local credentials")
    return _create_repo_remote(repo_id=repo_id, repo_kind=repo_kind, exist_ok=exist_ok)


def _upload_folder(
    run_dir: Path,
    *,
    repo_id: str,
    repo_kind: str,
    artifact_paths: list[Path],
) -> None:
    """Delegate to the resumable Hugging Face large-folder uploader. Kept behind a tiny
    wrapper so tests can patch it without touching the rest of the
    module."""
    from huggingface_hub import upload_large_folder

    folder = run_dir
    # Upload only paths bound by the completion receipt.
    kwargs: dict[str, Any] = dict(  # noqa: C408
        repo_id=repo_id,
        repo_type=repo_kind,
        folder_path=str(folder),
        allow_patterns=[path.relative_to(run_dir).as_posix() for path in artifact_paths],
    )
    upload_large_folder(**kwargs)


def _create_repo_remote(*, repo_id: str, repo_kind: str, exist_ok: bool) -> str:
    from huggingface_hub import create_repo as hf_create_repo

    hf_create_repo(
        repo_id=repo_id,
        repo_type=repo_kind,
        exist_ok=exist_ok,
        private=False,
    )
    return repo_id

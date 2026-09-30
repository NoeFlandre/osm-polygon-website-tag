"""Dataset and metrics publishing CLI adapters."""

from __future__ import annotations

from typing import Annotated

import typer

from osm_polygon_website_tag.publishing.publish import (
    build_publish_plan,
    create_repo,
    publish_to_hf,
    repo_exists,
)
from osm_polygon_website_tag.publishing.release import release_card_and_stats
from osm_polygon_website_tag.publishing.trackio import (
    build_trackio_snapshot,
    publish_trackio_snapshot,
)
from osm_polygon_website_tag.runtime.config import (
    DEFAULT_HF_DATASET,
    DEFAULT_TRACKIO_PROJECT,
    DEFAULT_TRACKIO_SPACE,
)

from . import RepoId, RunDir, _configured_hf_dataset_repo, _json


def publish_command(
    run_dir: RunDir,
    repo_id: RepoId = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually upload (default: dry-run)."),
    ] = False,
) -> int:
    """Publish or dry-run a complete dataset."""
    plan = publish_to_hf(run_dir, repo_id=_configured_hf_dataset_repo(repo_id), dry_run=not apply)
    _json({"dry_run": not apply, "artifact_count": len(plan.artifact_paths)})
    return 0


def publish_plan_command(
    run_dir: RunDir,
    repo_id: RepoId = None,
) -> int:
    """List the publication plan."""
    plan = build_publish_plan(run_dir, repo_id=_configured_hf_dataset_repo(repo_id))
    _json(
        {
            # Do not echo values sourced from Settings; only reveal an explicit CLI option.
            "repo_id": repo_id,
            "artifact_count": len(plan.artifact_paths),
            "readme": str(plan.readme_path) if plan.readme_path else None,
        }
    )
    return 0


def create_repo_command(
    repo_id: Annotated[
        str, typer.Option("--repo-id", help="Hugging Face dataset repository to create.")
    ],
    exist_ok: Annotated[
        bool, typer.Option("--exist-ok", help="Succeed if the repository already exists.")
    ] = False,
    apply: Annotated[
        bool, typer.Option("--apply", help="Create it; without this, only report the plan.")
    ] = False,
) -> int:
    """Create the Hugging Face dataset repository (dry run unless --apply)."""
    if not apply:
        _json({"applied": False, "exists": repo_exists(repo_id=repo_id), "repo_id": repo_id})
        return 0
    repo = create_repo(repo_id=repo_id, exist_ok=exist_ok)
    typer.echo(repo)
    return 0


def publish_trackio_command(
    run_dir: RunDir,
    space_id: Annotated[
        str,
        typer.Option("--space-id", help="Public Hugging Face Trackio Space."),
    ] = DEFAULT_TRACKIO_SPACE,
    project: Annotated[
        str,
        typer.Option("--project", help="Trackio project name."),
    ] = DEFAULT_TRACKIO_PROJECT,
    dataset_repo: Annotated[
        str | None,
        typer.Option(
            "--dataset-repo",
            help="Dataset repository represented by the metrics (defaults to HF_DATASET_REPO).",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Actually create/update the public Trackio Space."),
    ] = False,
) -> int:
    """Preview or publish metrics for one finalized dataset snapshot."""
    snapshot = build_trackio_snapshot(
        run_dir, dataset_repo=_configured_hf_dataset_repo(dataset_repo)
    )
    remote = (
        publish_trackio_snapshot(snapshot, space_id=space_id, project=project) if apply else None
    )
    _json(
        {
            "dry_run": not apply,
            "space_id": space_id,
            "project": project,
            "run_name": snapshot.run_name,
            "manifest_digest": snapshot.manifest_digest,
            "metrics": snapshot.metrics,
            "remote": remote,
        },
        sort_keys=True,
    )
    return 0


def release_stats_command(
    run_dir: RunDir,
    confirm_repo: Annotated[
        str,
        typer.Option("--confirm-repo", help="Exact dataset repository confirmation."),
    ],
    repo_id: Annotated[
        str, typer.Option("--repo-id", help="Canonical release dataset repository.")
    ] = DEFAULT_HF_DATASET,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Publish and verify (default: dry-run)."),
    ] = False,
) -> int:
    """Publish only the dataset card and the statistics report."""
    report = release_card_and_stats(
        run_dir,
        confirm_repo=confirm_repo,
        repo_id=repo_id,
        apply=apply,
    )
    _json(report.to_payload(), sort_keys=True)
    return 0

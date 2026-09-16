"""Deterministic card/report release to the exact Hugging Face dataset.

The release path recomputes the dataset card and ``stats.json`` from the
complete verified run, publishes only those two documents, and verifies the
remote files afterwards. Polygon shards, manifests, and unrelated Hub files are
never touched by this path.

Determinism: the card and the report are rendered from every published row of
the verified run. Regeneration writes ``stats.json`` only when its bytes would
change, so a second release over an unchanged run is a no-op that produces the
same plan and the same remote revision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token
from osm_polygon_website_tag.reporting.artifact_inventory import hash_file
from osm_polygon_website_tag.reporting.card import build_card
from osm_polygon_website_tag.reporting.finalize import replace_receipt_atomic
from osm_polygon_website_tag.reporting.verify import VerificationReport, verify_results
from osm_polygon_website_tag.runtime.config import DEFAULT_HF_DATASET

CARD_RELEASE_FILES = ("README.md", "stats.json")


@dataclass(frozen=True)
class ReleasedFile:
    """One published metadata file and its exact identity."""

    relative_path: str
    sha256: str
    size_bytes: int


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

    def to_payload(self) -> dict[str, Any]:
        return {
            "files": [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in self.files
            ],
            "published": self.published,
            "recomputed": self.recomputed,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "run_dir": self.run_dir,
            "verified_shards": list(self.verified_shards),
        }


class HubVerifier(Protocol):
    """Confirm the released files exist remotely and return the repo revision."""

    def __call__(self, repo_id: str, files: tuple[ReleasedFile, ...]) -> str: ...


def build_card_release_plan(run_dir: Path | str) -> tuple[ReleasedFile, ...]:
    """Return the exact card/report files that a release would upload."""
    root = Path(run_dir)
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
            )
        )
    return tuple(items)


def _require_exact_repo(confirm_repo: str, repo_id: str) -> None:
    if confirm_repo != repo_id:
        raise ValueError(f"repository confirmation must equal {repo_id!r} (got {confirm_repo!r})")


def _require_verified(report: VerificationReport, run_dir: Path) -> None:
    if not report.ok:
        raise ValueError(f"verification failed for {run_dir}; refusing to release: {report.errors}")


def _recompute_card(run_dir: Path) -> bool:
    """Rebuild the card and report; return whether their bytes changed."""
    before = {name: _digest_or_none(run_dir / name) for name in CARD_RELEASE_FILES}
    build_card(run_dir)
    after = {name: _digest_or_none(run_dir / name) for name in CARD_RELEASE_FILES}
    if before == after:
        return False
    replace_receipt_atomic(run_dir)
    return True


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
        raise ValueError(f"remote file missing in revision {revision}: {item.relative_path}")
    size = getattr(entry, "size", None)
    if size is not None and int(size) != item.size_bytes:
        raise ValueError(
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
        raise ValueError(
            f"remote SHA-256 mismatch for {item.relative_path}: "
            f"local={item.sha256}, remote={digest}"
        )


def default_hub_verifier(repo_id: str, files: tuple[ReleasedFile, ...]) -> str:
    """Confirm each released file's remote identity and return the revision."""
    from huggingface_hub import HfApi

    api = HfApi(token=resolve_hf_token())
    revision = _remote_revision(api, repo_id)
    for item in files:
        _verify_remote_size(api, repo_id, revision, item)
        _verify_remote_digest(api, repo_id, revision, item)
    return revision


def _require_credentialed_uploader() -> Callable[..., None]:
    """Return the real uploader, refusing when no credential is configured."""
    if not resolve_hf_token():
        raise ValueError("release requires Hugging Face environment/local credentials")
    return _upload_card_files


def _publish(
    run_dir: Path,
    *,
    repo_id: str,
    repo_kind: str,
    files: tuple[ReleasedFile, ...],
    uploader: Callable[..., None] | None,
    verifier: HubVerifier | None,
) -> str:
    """Upload the released files and return the verified remote revision."""
    upload = uploader or _require_credentialed_uploader()
    upload(run_dir, repo_id=repo_id, repo_kind=repo_kind, files=files)
    verify_remote = verifier or default_hub_verifier
    revision = verify_remote(repo_id, files)
    if not revision:
        raise ValueError("hub verification returned an empty revision")
    return revision


def release_card_and_stats(
    run_dir: Path | str,
    *,
    confirm_repo: str,
    repo_id: str = DEFAULT_HF_DATASET,
    repo_kind: str = "dataset",
    apply: bool = False,
    uploader: Callable[..., None] | None = None,
    verifier: HubVerifier | None = None,
) -> CardReleaseReport:
    """Recompute, verify, and optionally publish the card and statistics report.

    ``apply=False`` performs the full local compute-and-verify pass and returns
    the exact plan that would be uploaded, without touching the network.
    """
    _require_exact_repo(confirm_repo, repo_id)
    root = Path(run_dir)
    report = verify_results(root)
    _require_verified(report, root)
    recomputed = _recompute_card(root)
    if recomputed:
        _require_verified(verify_results(root), root)
    files = build_card_release_plan(root)
    revision = (
        _publish(
            root,
            repo_id=repo_id,
            repo_kind=repo_kind,
            files=files,
            uploader=uploader,
            verifier=verifier,
        )
        if apply
        else None
    )
    return CardReleaseReport(
        repo_id=repo_id,
        run_dir=str(root),
        files=files,
        verified_shards=tuple(report.checked_shards),
        recomputed=recomputed,
        published=apply,
        revision=revision,
    )


__all__ = [
    "CARD_RELEASE_FILES",
    "CardReleaseReport",
    "ReleasedFile",
    "build_card_release_plan",
    "default_hub_verifier",
    "release_card_and_stats",
]

"""Command-line interface for osm-polygon-website-tag.

Each subcommand maps to a single library function. The CLI never
rebuilds state from scratch -- it loads existing run state, calls the
library function, and reports the result.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analyze import analyze_results
from .card import build_card
from .card_stats import compute_card_stats
from .config import DEFAULT_HF_DATASET
from .extraction import extract_pbf
from .finalize import finalize_run
from .publish import build_publish_plan, create_repo, publish_to_hf
from .run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_ENRICHED,
    STATUS_EXTRACTED,
    STATUS_EXTRACTING,
    STATUS_INITIALIZED,
    expected_source_inventory,
    initialise_run,
    load_run,
    snapshot_source_fingerprint,
    source_inventory_matches,
    transition_status,
    upsert_run_metadata,
)
from .safety import assert_path_safe_against, normalize_path
from .verify import verify_results
from .workflow import run_all


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="osm-polygon-website-tag")
    sub = p.add_subparsers(dest="subcommand")

    init = sub.add_parser("init", help="Initialise a new run directory.")
    init.add_argument("--output-root", type=Path, required=True)
    init.add_argument("--run-id")
    init.add_argument("--source-root", type=Path, required=True)
    init.add_argument(
        "--expected-source",
        type=Path,
        action="append",
        required=True,
        help="Expected source PBF path; repeat once per source.",
    )
    init.set_defaults(func=_cmd_init)

    extract = sub.add_parser("extract", help="Extract one .osm.pbf file.")
    extract.add_argument("pbf_path", type=Path)
    extract.add_argument("--run-dir", type=Path, required=True)
    extract.set_defaults(func=_cmd_extract)

    analyze = sub.add_parser("analyze-results", help="Run the analyzer.")
    analyze.add_argument("--run-dir", type=Path, required=True)
    analyze.set_defaults(func=_cmd_analyze)

    card = sub.add_parser("build-card", help="Build the README card.")
    card.add_argument("--run-dir", type=Path, required=True)
    card.set_defaults(func=_cmd_card)

    verify = sub.add_parser("verify-results", help="Verify the run.")
    verify.add_argument("--run-dir", type=Path, required=True)
    verify.set_defaults(func=_cmd_verify)

    finalize = sub.add_parser("finalize-run", help="Finalize the run.")
    finalize.add_argument("--run-dir", type=Path, required=True)
    finalize.set_defaults(func=_cmd_finalize)

    plan = sub.add_parser("publish-plan", help="List the publish plan.")
    plan.add_argument("--run-dir", type=Path, required=True)
    plan.add_argument("--repo-id", default=DEFAULT_HF_DATASET)
    plan.set_defaults(func=_cmd_publish_plan)

    pub = sub.add_parser("publish", help="Publish (or dry-run) to HF.")
    pub.add_argument("--run-dir", type=Path, required=True)
    pub.add_argument("--repo-id", default=DEFAULT_HF_DATASET)
    pub.add_argument("--apply", action="store_true", help="Actually upload (default: dry-run).")
    pub.set_defaults(func=_cmd_publish)

    cr = sub.add_parser("create-repo", help="Create the HF repo.")
    cr.add_argument("--repo-id", required=True)
    cr.add_argument("--exist-ok", action="store_true")
    cr.set_defaults(func=_cmd_create_repo)

    stats = sub.add_parser("card-stats", help="Recompute and print card stats.")
    stats.add_argument("--run-dir", type=Path, required=True)
    stats.set_defaults(func=_cmd_card_stats)

    run = sub.add_parser("run-all", help="Run or resume the complete PBF inventory.")
    run.add_argument("--source-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--repo-id", default=DEFAULT_HF_DATASET)
    run.add_argument(
        "--apply", action="store_true", help="Upload after each PBF and at completion."
    )
    run.add_argument(
        "--ensure-repo",
        action="store_true",
        help="Create the HF dataset repo if needed (only with --apply).",
    )
    run.set_defaults(func=_cmd_run_all)

    return p


def _cmd_init(args: argparse.Namespace) -> int:
    source_root = normalize_path(args.source_root)
    output_root = assert_path_safe_against(args.output_root, source_root)
    sources = [normalize_path(source) for source in args.expected_source]
    for source in sources:
        if not source.is_relative_to(source_root):
            raise ValueError(f"expected source is outside source root: {source}")
    fingerprints = [snapshot_source_fingerprint(source) for source in args.expected_source]
    run_dir, _ = initialise_run(
        output_root,
        run_id=args.run_id,
        expected_sources=fingerprints,
    )
    state = load_run(run_dir)
    upsert_run_metadata(state, {"source_root": str(source_root)})
    print(str(run_dir))
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    state_path = run_dir / "manifests" / "run.json"
    if not state_path.is_file():
        raise ValueError("extract requires a run created by the init command")
    state = load_run(run_dir)
    pbf_path = Path(args.pbf_path)
    fingerprint = snapshot_source_fingerprint(pbf_path)
    expected = expected_source_inventory(run_dir)
    if {
        "filename": fingerprint.filename,
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
    } not in expected:
        raise ValueError(f"source is not in exact expected inventory: {pbf_path.name}")
    status = state.metadata.get("status")
    if status == STATUS_INITIALIZED:
        transition_status(state, STATUS_EXTRACTING)
    elif status != STATUS_EXTRACTING:
        raise ValueError(f"extract requires initialized/extracting state, got {status!r}")
    extract_pbf(pbf_path, run_dir, run_state=state)
    if source_inventory_matches(run_dir):
        transition_status(state, STATUS_EXTRACTED)
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    state = load_run(args.run_dir)
    if state.metadata.get("status") != STATUS_ENRICHED:
        raise ValueError("analyze-results requires enriched state; use run-all for enrichment")
    summary = analyze_results(args.run_dir)
    transition_status(state, STATUS_ANALYZED)
    print(json.dumps(summary.__dict__, default=str, indent=2))
    return 0


def _cmd_card(args: argparse.Namespace) -> int:
    state = load_run(args.run_dir)
    if state.metadata.get("status") != STATUS_ANALYZED:
        raise ValueError("build-card requires analyzed state")
    path = build_card(args.run_dir)
    transition_status(state, STATUS_CARD_BUILT)
    print(str(path))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    report = verify_results(args.run_dir)
    print(json.dumps({"ok": report.ok, "errors": report.errors}, indent=2))
    return 0 if report.ok else 1


def _cmd_finalize(args: argparse.Namespace) -> int:
    report = finalize_run(args.run_dir)
    print(json.dumps({"ok": report.ok, "digest": report.receipt.get("manifest_digest")}, indent=2))
    return 0 if report.ok else 1


def _cmd_publish_plan(args: argparse.Namespace) -> int:
    plan = build_publish_plan(args.run_dir, repo_id=args.repo_id)
    print(
        json.dumps(
            {
                "repo_id": plan.repo_id,
                "artifact_count": len(plan.artifact_paths),
                "readme": str(plan.readme_path) if plan.readme_path else None,
            },
            indent=2,
        )
    )
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    plan = publish_to_hf(
        args.run_dir,
        repo_id=args.repo_id,
        dry_run=not args.apply,
    )
    print(
        json.dumps(
            {
                "dry_run": not args.apply,
                "artifact_count": len(plan.artifact_paths),
            },
            indent=2,
        )
    )
    return 0


def _cmd_create_repo(args: argparse.Namespace) -> int:
    repo = create_repo(repo_id=args.repo_id, exist_ok=args.exist_ok)
    print(repo)
    return 0


def _cmd_card_stats(args: argparse.Namespace) -> int:
    stats = compute_card_stats(args.run_dir)
    print(json.dumps(stats.__dict__, default=str, indent=2))
    return 0


def _cmd_run_all(args: argparse.Namespace) -> int:
    if args.ensure_repo and not args.apply:
        raise ValueError("--ensure-repo requires --apply")
    result = run_all(
        source_root=args.source_root,
        output_root=args.output_root,
        run_id=args.run_id,
        repo_id=args.repo_id,
        apply=args.apply,
        ensure_repo=args.ensure_repo,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    payload = {
        **result.__dict__,
        "run_dir": str(result.run_dir),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


_ = compute_card_stats  # imported for command dispatch


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

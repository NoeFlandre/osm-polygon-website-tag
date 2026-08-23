"""Tests for the CLI dispatcher."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.application import cli
from osm_polygon_website_tag.application.cli import app, main
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import POLYGON_PUBLIC_SCHEMA
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.runtime.run_state import (
    RunState,
    SourceFingerprint,
    hash_shard,
    initialise_run,
    record_processed_source,
    snapshot_source_fingerprint,
)


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


def _row():
    return {
        "polygon_id": "p1",
        "region": "monaco",
        "source_pbf": "monaco-latest.osm.pbf",
        "osm_type": "way",
        "osm_id": 100,
        "osm_version": 1,
        "osm_timestamp": _ts(),
        "website": "https://example.com",
        "contact_website": None,
        "has_website": True,
        "has_contact_website": False,
        "has_any_website": True,
        "preferred_website": "https://example.com",
        "preferred_website_source": "website",
        "website_class": "absolute_url",
        "contact_website_class": None,
        "website_hostname": "example.com",
        "contact_website_hostname": None,
        "wikidata": "Q42",
        "wikidata_qid": "Q42",
        "wikidata_class": "canonical_qid",
        "name": None,
        "tags": "{}",
        "tag_keys": "[]",
        "tag_count": 0,
        "osm_primary_tag": "building",
        "geometry": json.dumps({"type": "Polygon", "coordinates": []}),
        "centroid": json.dumps({"type": "Point", "coordinates": [0.0, 0.0]}),
        "lat": 0.0,
        "lon": 0.0,
        "bbox": "[0.0,0.0,0.0,0.0]",
        "area_m2": 50.0,
        "area_km2": 5e-5,
        "area_bucket": "10-100m2",
        "centroid_kind": "lambert_azimuthal_equal_area",
        "schema_version": "v1.2",
        "website_text": "example text",
        "website_word_count": 2,
        "website_text_status": "success",
        "contact_website_text": None,
        "contact_website_word_count": None,
        "contact_website_text_status": "absent",
    }


def _setup_run(tmp_path: Path) -> Path:
    run_dir, state = initialise_run(tmp_path, run_id="r")
    p = tmp_path / "monaco-latest.osm.pbf"
    p.write_bytes(b"data")
    fp = snapshot_source_fingerprint(p)
    pub = run_dir / "polygons" / "monaco-latest.parquet"
    pub.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([_row()], schema=POLYGON_PUBLIC_SCHEMA), pub, compression="snappy"
    )
    obs = run_dir / "analysis_observations" / "monaco-latest.parquet"
    obs.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([], schema=COMPARISON_OBSERVATION_SCHEMA),
        obs,
        compression="snappy",
    )
    rej = run_dir / "rejections" / "monaco-latest.parquet"
    rej.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([], schema=REJECTION_SCHEMA), rej, compression="snappy")
    record_processed_source(
        state,
        fp,
        public_row_count=1,
        observation_row_count=0,
        rejection_count=0,
        public_shard_sha256=hash_shard(pub),
        observation_shard_sha256=hash_shard(obs),
        rejection_shard_sha256=hash_shard(rej),
    )
    return run_dir


def test_cli_help_exits_2() -> None:
    rc = main([])
    assert rc == 2


def test_cli_exposes_explicit_typer_app() -> None:
    assert hasattr(cli, "app")


def test_cli_extract_helpers_expose_concrete_state_types() -> None:
    assert get_type_hints(cli._validate_expected_extract_source)["fingerprint"] is SourceFingerprint
    assert get_type_hints(cli._prepare_extract_status)["state"] is RunState


def test_typer_help_lists_every_public_command() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "init",
        "extract",
        "analyze-results",
        "build-card",
        "verify-results",
        "refresh-card",
        "finalize-run",
        "finalize-snapshot",
        "publish-plan",
        "publish",
        "create-repo",
        "card-stats",
        "publish-trackio",
        "run-all",
    ):
        assert command in result.stdout


def test_cli_finalize_snapshot_reports_result(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "finalize_snapshot",
        lambda _run_dir: SimpleNamespace(
            ok=True,
            receipt={"manifest_digest": "a" * 64},
            verification=SimpleNamespace(errors=[]),
        ),
    )

    assert main(["finalize-snapshot", "--run-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "digest": "a" * 64,
        "errors": [],
        "ok": True,
    }


def test_run_all_help_lists_bounded_worker_options() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["run-all", "--help"])

    assert result.exit_code == 0
    assert "--area-workers" in result.stdout
    assert "--max-in-flight-areas" in result.stdout
    assert "--fetch-workers" in result.stdout


def test_application_progress_adapter_module_exists() -> None:
    assert importlib.util.find_spec("osm_polygon_website_tag.application.progress") is not None


def test_cli_init_records_exact_expected_sources(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "monaco-latest.osm.pbf"
    source.write_bytes(b"synthetic")
    output_root = tmp_path / "runs"

    rc = main(
        [
            "init",
            "--output-root",
            str(output_root),
            "--run-id",
            "r1",
            "--source-root",
            str(source_root),
            "--expected-source",
            str(source),
        ]
    )

    assert rc == 0
    manifest = json.loads((output_root / "r1" / "manifests" / "expected_sources.json").read_text())
    assert manifest == [
        {
            "filename": source.name,
            "mtime_ns": source.stat().st_mtime_ns,
            "size_bytes": source.stat().st_size,
        }
    ]


def test_cli_init_rejects_output_inside_source_root(tmp_path: Path, capsys) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "monaco-latest.osm.pbf"
    source.write_bytes(b"synthetic")

    rc = main(
        [
            "init",
            "--output-root",
            str(source_root / "runs"),
            "--run-id",
            "unsafe",
            "--source-root",
            str(source_root),
            "--expected-source",
            str(source),
        ]
    )

    assert rc == 2
    assert not (source_root / "runs").exists()
    assert capsys.readouterr().err.startswith("error: ")


def test_cli_rejects_hf_token_arguments() -> None:
    assert main(["publish", "--run-dir", "/tmp/run", "--hf-token", "secret"]) == 2
    assert main(["create-repo", "--repo-id", "owner/name", "--hf-token", "secret"]) == 2


def test_cli_extract_preserves_real_counts(make_pbf, tmp_path: Path) -> None:
    source_dir = make_pbf(
        """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
<node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="1"/>
<node id="3" lat="1" lon="1"/><node id="4" lat="1" lon="0"/>
<way id="10" version="1" timestamp="2024-01-01T00:00:00Z">
<nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
<tag k="building" v="yes"/><tag k="website" v="https://example.com"/>
</way></osm>""",
        name="monaco-latest.osm.pbf",
    )
    source = next(source_dir.iterdir())
    source_root = source.parent
    output_root = tmp_path / "runs"
    assert (
        main(
            [
                "init",
                "--output-root",
                str(output_root),
                "--run-id",
                "r1",
                "--source-root",
                str(source_root),
                "--expected-source",
                str(source),
            ]
        )
        == 0
    )

    assert main(["extract", str(source), "--run-dir", str(output_root / "r1")]) == 0

    manifest = json.loads((output_root / "r1" / "manifests" / "sources.json").read_text())
    assert manifest[0]["public_row_count"] == 1
    assert manifest[0]["observation_row_count"] == 1
    metadata = json.loads((output_root / "r1" / "manifests" / "run.json").read_text())
    assert metadata["status"] == "extracted"


def test_cli_verify_results_returns_zero_on_pass(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["verify-results", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_publish_trackio_defaults_to_dry_run(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from osm_polygon_website_tag.publishing.trackio import TrackioSnapshot

    snapshot = TrackioSnapshot(
        run_name="dataset-abc",
        manifest_digest="a" * 64,
        dataset_repo="owner/dataset",
        metrics={"dataset_public_polygon_rows": 3},
    )
    monkeypatch.setattr(cli, "build_trackio_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        cli,
        "publish_trackio_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("published")),
    )

    assert main(["publish-trackio", "--run-dir", str(tmp_path / "run")]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_cli_verify_results_returns_nonzero_on_failure(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    (run_dir / "polygons" / "monaco-latest.parquet").write_bytes(b"junk")
    rc = main(["verify-results", "--run-dir", str(run_dir)])
    assert rc == 1


def test_cli_card_stats_runs(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["card-stats", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_publish_plan_runs(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["publish-plan", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_create_repo_requires_token() -> None:
    rc = main(["create-repo", "--repo-id", "foo/bar"])
    assert rc != 0


def test_cli_publish_dry_run(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["publish", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_analyze_card_refresh_and_finalize_commands_delegate(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    run_dir = tmp_path / "run"
    state = SimpleNamespace(metadata={"status": "enriched"})
    monkeypatch.setattr(cli, "load_run", lambda _run_dir: state)
    monkeypatch.setattr(cli, "analyze_results", lambda _run_dir: SimpleNamespace(value=1))
    monkeypatch.setattr(cli, "build_card", lambda _run_dir: run_dir / "README.md")
    monkeypatch.setattr(
        cli,
        "refresh_card_run",
        lambda _run_dir: SimpleNamespace(ok=True, verification=SimpleNamespace(errors=[])),
    )
    monkeypatch.setattr(
        cli,
        "finalize_run",
        lambda _run_dir: SimpleNamespace(ok=True, receipt={"manifest_digest": "a" * 64}),
    )
    monkeypatch.setattr(
        cli,
        "transition_status",
        lambda _state, new_status: state.metadata.__setitem__("status", new_status),
    )

    assert cli.analyze_command(run_dir) == 0
    assert cli.card_command(run_dir) == 0
    assert cli.refresh_card_command(run_dir) == 0
    assert cli.finalize_command(run_dir) == 0
    assert '"digest"' in capsys.readouterr().out


def test_cli_run_all_command_closes_progress_and_reports_result(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    events: list[bool] = []

    class FakeProgress:
        def close(self, *, completed: bool) -> None:
            events.append(completed)

    monkeypatch.setattr(cli, "ProgressReporter", FakeProgress)
    monkeypatch.setattr(
        cli,
        "run_all",
        lambda **_kwargs: SimpleNamespace(complete=True, run_dir=tmp_path / "run", sources=3),
    )

    assert (
        cli.run_all_command(
            source_root=tmp_path / "source",
            output_root=tmp_path / "runs",
            run_id="run",
            repo_id="owner/dataset",
            apply=False,
            ensure_repo=False,
            area_workers=1,
            max_in_flight_areas=1,
            fetch_workers=1,
        )
        == 0
    )
    assert events == [True]
    assert '"complete": true' in capsys.readouterr().out

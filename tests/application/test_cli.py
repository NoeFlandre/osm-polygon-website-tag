"""Tests for the CLI dispatcher."""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import typer
from huggingface_hub.errors import HfHubHTTPError
from tests.fixtures.polygon_shards import v1_2_polygon_row as _row

from osm_polygon_website_tag.application import cli
from osm_polygon_website_tag.application.cli import (
    app,
    grid5000,
    languages,
    main,
    publish,
    run,
    sentences,
    verify,
)
from osm_polygon_website_tag.contracts.comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.contracts.rejection_schema import REJECTION_SCHEMA
from osm_polygon_website_tag.pipeline import sentence_run
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.publishing import publish as publish_module
from osm_polygon_website_tag.reporting.geometry_stats import compute_geometry_stats
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    RunState,
    SourceManifestEntry,
    hash_shard,
    initialise_run,
    load_run,
    record_processed_source,
    snapshot_source_fingerprint,
    transition_status,
    upsert_run_metadata,
)
from osm_polygon_website_tag.web.politeness import HostPolicy


def _ts():
    return pa.scalar(0, type=pa.timestamp("us", tz="UTC")).as_py()


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
        "geometry-stats",
        "publish-trackio",
        "run-all",
        "detect-languages",
        "grid5000-prepare",
        "grid5000-run",
        "grid5000-sync",
        "grid5000-prepare-sentences",
        "grid5000-run-sentences",
        "grid5000-sync-sentences",
    ):
        assert command in result.stdout


def test_cli_help_snapshot_preserves_every_command_option_and_default() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    root_result = runner.invoke(app, ["--help"], color=False, terminal_width=100)
    commands = sorted(
        name for command in app.registered_commands if (name := command.name) is not None
    )
    command_results = {
        name: runner.invoke(app, [name, "--help"], color=False, terminal_width=100)
        for name in commands
    }

    assert root_result.exit_code == 0
    assert all(result.exit_code == 0 for result in command_results.values())
    actual = {
        "root": root_result.output,
        "commands": {name: result.output for name, result in command_results.items()},
    }
    expected_path = Path(__file__).parents[1] / "fixtures" / "cli_help.json"

    assert actual == json.loads(expected_path.read_text(encoding="utf-8"))


def test_cli_command_names_dispatch_to_their_public_adapters() -> None:
    registered_app = typer.Typer(
        name="osm-polygon-website-tag",
        help="Analyze and publish OSM polygons carrying website tags.",
        no_args_is_help=True,
        rich_markup_mode=None,
    )
    cli._register_commands(registered_app)

    expected_callbacks = {
        "init": run.init_command,
        "extract": run.extract_command,
        "analyze-results": verify.analyze_command,
        "build-card": verify.card_command,
        "verify-results": verify.verify_command,
        "refresh-card": verify.refresh_card_command,
        "finalize-run": verify.finalize_command,
        "finalize-snapshot": verify.finalize_snapshot_command,
        "publish-plan": publish.publish_plan_command,
        "publish": publish.publish_command,
        "release-stats": publish.release_stats_command,
        "create-repo": publish.create_repo_command,
        "card-stats": verify.card_stats_command,
        "geometry-stats": verify.geometry_stats_command,
        "publish-trackio": publish.publish_trackio_command,
        "run-all": run.run_all_command,
        "detect-languages": languages.detect_languages_command,
        "segment-sentences": sentences.segment_sentences_command,
        "grid5000-prepare": grid5000.grid5000_prepare_command,
        "grid5000-run": grid5000.grid5000_run_command,
        "grid5000-sync": grid5000.grid5000_sync_command,
        "grid5000-prepare-sentences": grid5000.grid5000_prepare_sentences_command,
        "grid5000-run-sentences": grid5000.grid5000_run_sentences_command,
        "grid5000-sync-sentences": grid5000.grid5000_sync_sentences_command,
    }

    for command_app in (app, registered_app):
        callbacks = {
            command.name: command.callback
            for command in command_app.registered_commands
            if command.name
        }
        assert callbacks == expected_callbacks

    from typer.testing import CliRunner

    runner = CliRunner()
    expected_help = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "cli_help.json").read_text(encoding="utf-8")
    )["commands"]
    actual_help = {
        name: runner.invoke(
            registered_app, [name, "--help"], color=False, terminal_width=100
        ).output
        for name in sorted(expected_callbacks)
    }
    assert actual_help == expected_help


def test_cli_run_examples_use_the_portable_data_root_fallback() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    for command in ("init", "extract", "publish", "release-stats", "run-all"):
        result = runner.invoke(app, [command, "--help"])

        assert result.exit_code == 0
        assert "${OSM_POLY_DATA_DIR:-./data}/runs" in result.stdout


def test_cli_grid5000_commands_use_the_explicit_bundle_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    language_bundle = SimpleNamespace(
        payload=lambda: {"source_shard": "source.parquet", "aaa": "first when sorted"}
    )
    sentence_bundle = SimpleNamespace(
        model=SimpleNamespace(filename="sat.bin", revision="revision-1"),
        payload=lambda: {
            "sentence_shard": "sentences.parquet",
            "aaa": "first when sorted",
        },
    )
    result = SimpleNamespace(payload=lambda: {"shard_sha256": "a" * 64, "completed": True})
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    validations: list[tuple[Path, str]] = []

    def require_under_data_root(path: Path, *, label: str) -> Path:
        validations.append((Path(path), label))
        return Path(path)

    monkeypatch.setattr(grid5000, "require_under_data_root", require_under_data_root)
    monkeypatch.setattr(
        grid5000,
        "prepare_language_bundle",
        lambda *args, **kwargs: calls.append(("prepare", args, kwargs)) or language_bundle,
    )
    monkeypatch.setattr(
        grid5000,
        "run_language_bundle",
        lambda *args, **kwargs: calls.append(("run", args, kwargs)) or result,
    )
    monkeypatch.setattr(
        grid5000,
        "sync_language_bundle",
        lambda *args, **kwargs: calls.append(("sync", args, kwargs)) or result,
    )
    monkeypatch.setattr(
        grid5000,
        "prepare_sentence_bundle",
        lambda *args, **kwargs: (
            calls.append(("prepare_sentences", args, kwargs)) or sentence_bundle
        ),
    )
    monkeypatch.setattr(
        grid5000,
        "load_sentence_bundle",
        lambda *args, **kwargs: calls.append(("load_sentences", args, kwargs)) or sentence_bundle,
    )
    splitter = object()
    monkeypatch.setattr(
        grid5000,
        "load_sat_splitter_from_path",
        lambda *args, **kwargs: calls.append(("load_splitter", args, kwargs)) or splitter,
    )
    monkeypatch.setattr(
        grid5000,
        "run_sentence_bundle",
        lambda *args, **kwargs: calls.append(("run_sentences", args, kwargs)) or result,
    )
    monkeypatch.setattr(
        grid5000,
        "sync_sentence_bundle",
        lambda *args, **kwargs: calls.append(("sync_sentences", args, kwargs)) or result,
    )

    assert (
        main(
            [
                "grid5000-prepare",
                "--run-dir",
                str(tmp_path / "run"),
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--model-path",
                str(tmp_path / "model.bin"),
                "--commit",
                "abc123",
                "--shard",
                "source.parquet",
                "--time-budget-seconds",
                "123",
                "--batch-rows",
                "7",
            ]
        )
        == 0
    )
    prepare_output = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "grid5000-run",
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--time-budget-seconds",
                "12.5",
                "--batch-rows",
                "4",
                "--job-id",
                "job-1",
            ]
        )
        == 0
    )
    run_output = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "grid5000-sync",
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
        == 0
    )
    sync_output = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "grid5000-prepare-sentences",
                "--run-dir",
                str(tmp_path / "run"),
                "--bundle-dir",
                str(tmp_path / "sentence-bundle"),
                "--model-dir",
                str(tmp_path / "sat-model"),
                "--model-revision",
                "revision-1",
                "--commit",
                "def456",
                "--time-budget-seconds",
                "321",
                "--batch-rows",
                "9",
                "--max-rows",
                "11",
            ]
        )
        == 0
    )
    prepare_sentences_output = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "grid5000-run-sentences",
                "--bundle-dir",
                str(tmp_path / "sentence-bundle"),
                "--time-budget-seconds",
                "10.5",
                "--batch-rows",
                "3",
                "--job-id",
                "job-2",
            ]
        )
        == 0
    )
    run_sentences_output = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                "grid5000-sync-sentences",
                "--bundle-dir",
                str(tmp_path / "sentence-bundle"),
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
        == 0
    )
    sync_sentences_output = json.loads(capsys.readouterr().out)

    assert all(
        list(payload) == sorted(payload)
        for payload in (
            prepare_output,
            run_output,
            sync_output,
            prepare_sentences_output,
            run_sentences_output,
            sync_sentences_output,
        )
    )

    assert [name for name, _args, _kwargs in calls] == [
        "prepare",
        "run",
        "sync",
        "prepare_sentences",
        "load_sentences",
        "load_splitter",
        "run_sentences",
        "sync_sentences",
    ]
    assert calls[0][1] == (tmp_path / "run", tmp_path / "bundle")
    assert calls[0][2] == {
        "model_path": tmp_path / "model.bin",
        "commit": "abc123",
        "time_budget_seconds": 123,
        "batch_rows": 7,
        "shard_name": "source.parquet",
    }
    assert calls[1][1] == (tmp_path / "bundle",)
    assert calls[1][2] == {
        "time_budget_seconds": 12.5,
        "batch_rows": 4,
        "job_id": "job-1",
    }
    assert calls[2][1] == (tmp_path / "bundle", tmp_path / "run")
    assert calls[3][1] == (tmp_path / "run", tmp_path / "sentence-bundle")
    assert calls[3][2] == {
        "model_dir": tmp_path / "sat-model",
        "model_revision": "revision-1",
        "commit": "def456",
        "time_budget_seconds": 321,
        "batch_rows": 9,
        "max_rows": 11,
    }
    assert calls[4][1:] == ((tmp_path / "sentence-bundle",), {})
    assert calls[5] == (
        "load_splitter",
        (tmp_path / "sentence-bundle" / "sat.bin",),
        {"revision": "revision-1"},
    )
    assert calls[6][1] == (tmp_path / "sentence-bundle",)
    assert calls[6][2] == {
        "splitter": splitter,
        "time_budget_seconds": 10.5,
        "batch_rows": 3,
        "job_id": "job-2",
    }
    assert calls[7][1] == (tmp_path / "sentence-bundle", tmp_path / "run")
    assert validations == [
        (tmp_path / "run", "run directory"),
        (tmp_path / "bundle", "Grid'5000 bundle directory"),
        (tmp_path / "model.bin", "GlotLID model path"),
        (tmp_path / "bundle", "Grid'5000 bundle directory"),
        (tmp_path / "run", "run directory"),
        (tmp_path / "run", "run directory"),
        (tmp_path / "sentence-bundle", "Grid'5000 bundle directory"),
        (tmp_path / "sat-model", "SaT model directory"),
        (tmp_path / "sentence-bundle", "Grid'5000 bundle directory"),
        (tmp_path / "run", "run directory"),
    ]
    assert prepare_output == {
        "aaa": "first when sorted",
        "bundle_dir": str(tmp_path / "bundle"),
        "source_shard": "source.parquet",
    }
    assert run_output == {"completed": True, "shard_sha256": "a" * 64}
    assert sync_output == {
        "bundle_dir": str(tmp_path / "bundle"),
        "run_dir": str(tmp_path / "run"),
        "completed": True,
        "shard_sha256": "a" * 64,
    }
    assert prepare_sentences_output == {
        "aaa": "first when sorted",
        "bundle_dir": str(tmp_path / "sentence-bundle"),
        "sentence_shard": "sentences.parquet",
    }
    assert run_sentences_output == {"completed": True, "shard_sha256": "a" * 64}
    assert sync_sentences_output == {
        "bundle_dir": str(tmp_path / "sentence-bundle"),
        "run_dir": str(tmp_path / "run"),
        "completed": True,
        "shard_sha256": "a" * 64,
    }


def test_cli_finalize_snapshot_reports_result(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        verify,
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
    assert "--detect-languages" in result.stdout


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

    assert rc == 3  # invalid input
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
    monkeypatch.setattr(publish, "build_trackio_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        publish,
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


def test_cli_detect_languages_rejects_a_run_outside_the_data_root_before_model_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _setup_run(tmp_path)
    monkeypatch.setattr(
        languages,
        "load_glotlid_detector",
        lambda *_args, **_kwargs: pytest.fail("unsafe run must not load GlotLID"),
        raising=False,
    )

    assert main(["detect-languages", "--run-dir", str(run_dir)]) == 3  # invalid input


def test_cli_detect_languages_loads_one_model_and_updates_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    run_dir = _setup_run(tmp_path)
    state = load_run(run_dir)
    transition_status(state, "extracting")
    transition_status(state, "extracted")
    transition_status(state, "enriching")
    transition_status(state, "enriched")
    cache_dir = tmp_path / "model-cache"
    detector = SimpleNamespace(
        identity=ModelIdentity("repo", "model.bin", "revision", "a" * 64),
        predict=lambda texts: [LanguagePrediction("eng_Latn", 0.9) for _text in texts],
    )
    loaded: list[Path] = []
    monkeypatch.setattr(languages, "require_under_data_root", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(languages, "model_cache_dir", {"glotlid": cache_dir}.__getitem__)
    monkeypatch.setattr(
        languages, "load_glotlid_detector", lambda path: loaded.append(path) or detector
    )

    assert main(["detect-languages", "--run-dir", str(run_dir)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {"changed_shards": 1, "run_dir": str(run_dir)}
    assert loaded == [cache_dir]
    assert pq.read_schema(run_dir / "polygons" / "monaco-latest.parquet").equals(
        POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True
    )
    assert load_run(run_dir).metadata["status"] == "enriched"
    assert load_run(run_dir).sources["monaco-latest.osm.pbf"]["public_shard_sha256"] == hash_shard(
        run_dir / "polygons" / "monaco-latest.parquet"
    )


def test_cli_detect_languages_forwards_batch_and_time_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _setup_run(tmp_path)
    state = load_run(run_dir)
    transition_status(state, "extracting")
    transition_status(state, "extracted")
    transition_status(state, "enriching")
    transition_status(state, "enriched")
    detector = SimpleNamespace(identity=ModelIdentity("repo", "file", "revision", "a" * 64))
    observed: dict[str, object] = {}

    monkeypatch.setattr(languages, "require_under_data_root", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(
        languages, "model_cache_dir", {"glotlid": tmp_path / "model-cache"}.__getitem__
    )
    monkeypatch.setattr(languages, "load_glotlid_detector", lambda _path: detector)
    monkeypatch.setattr(languages, "shard_needs_language_detection", lambda _path: True)

    def detect(_path, *, detector, batch_rows, time_budget_seconds):
        observed.update(
            detector=detector,
            batch_rows=batch_rows,
            time_budget_seconds=time_budget_seconds,
        )
        return SimpleNamespace(
            row_count=1,
            shard_sha256="b" * 64,
            changed=False,
            completed=False,
            processed_rows=1,
        )

    monkeypatch.setattr(languages, "detect_language_shard", detect)

    assert (
        main(
            [
                "detect-languages",
                "--run-dir",
                str(run_dir),
                "--batch-rows",
                "1",
                "--time-budget-seconds",
                "2",
            ]
        )
        == 0
    )

    assert observed["detector"] is detector
    assert observed["batch_rows"] == 1
    time_budget = cast(float, observed["time_budget_seconds"])
    assert 0 < time_budget <= 2.0


@pytest.mark.parametrize("value", [True, "invalid", float("nan"), float("inf"), 0, -1])
def test_cli_rejects_invalid_language_budget_before_reading_run(
    value: object,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="time_budget_seconds must be positive"):
        languages.detect_languages_command(
            tmp_path / "missing", time_budget_seconds=cast(float, value)
        )


def test_cli_detect_languages_rejects_frozen_snapshot_before_model_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = _setup_run(tmp_path)
    state = load_run(run_dir)
    for status in ("extracting", "extracted", "enriching", "enriched", "analyzed", "card_built"):
        transition_status(state, status)
    transition_status(state, "verified")
    transition_status(state, STATUS_COMPLETE)
    upsert_run_metadata(state, {"snapshot_status": "done"})
    monkeypatch.setattr(languages, "require_under_data_root", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(
        languages,
        "load_glotlid_detector",
        lambda *_args, **_kwargs: pytest.fail("frozen snapshot must not load GlotLID"),
    )

    assert main(["detect-languages", "--run-dir", str(run_dir)]) == 3  # invalid input


def test_cli_frozen_snapshot_error_message_is_stable() -> None:
    with pytest.raises(ValueError) as exc_info:
        languages._reject_frozen_language_run(
            RunState(
                Path("run"),
                "run",
                metadata={"status": STATUS_COMPLETE, "snapshot_status": "done"},
            )
        )

    assert str(exc_info.value) == "cannot add languages to a frozen snapshot"


def test_cli_card_stats_runs(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["card-stats", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_geometry_stats_prints_the_machine_readable_report(tmp_path: Path, capsys) -> None:
    run_dir = _setup_run(tmp_path)

    rc = main(["geometry-stats", "--run-dir", str(run_dir)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "v2"
    assert payload["row_count"] == compute_geometry_stats(run_dir).row_count


def test_cli_publish_plan_runs(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["publish-plan", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_publish_plan_uses_but_does_not_echo_hf_dataset_repo_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HF_DATASET_REPO", "someone/else")
    run_dir = _setup_run(tmp_path)
    configured_repo_ids: list[str | None] = []

    def build_plan(_run_dir: Path, *, repo_id: str | None) -> SimpleNamespace:
        configured_repo_ids.append(repo_id)
        return SimpleNamespace(repo_id=repo_id, artifact_paths=(), readme_path=None)

    monkeypatch.setattr(publish, "build_publish_plan", build_plan)

    rc = main(["publish-plan", "--run-dir", str(run_dir)])

    output = capsys.readouterr().out
    assert rc == 0
    assert configured_repo_ids == ["someone/else"]
    assert "someone/else" not in output
    assert json.loads(output)["repo_id"] is None


def test_cli_publish_plan_echoes_explicit_repo_id_without_echoing_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HF_DATASET_REPO", "configured/hidden")
    run_dir = _setup_run(tmp_path)

    rc = main(
        [
            "publish-plan",
            "--run-dir",
            str(run_dir),
            "--repo-id",
            "explicit/visible",
        ]
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "configured/hidden" not in output
    assert json.loads(output)["repo_id"] == "explicit/visible"


def test_cli_create_repo_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a credential the command must refuse before touching the Hub."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setattr(publish_module, "resolve_hf_token", lambda: None)
    reached: list[str] = []
    monkeypatch.setattr(
        publish_module,
        "_create_repo_remote",
        lambda **kwargs: reached.append(str(kwargs)) or "foo/bar",
    )

    rc = main(["create-repo", "--repo-id", "foo/bar", "--apply"])

    assert rc != 0
    assert reached == []


def test_cli_publish_dry_run(tmp_path: Path) -> None:
    run_dir = _setup_run(tmp_path)
    rc = main(["publish", "--run-dir", str(run_dir)])
    assert rc == 0


def test_cli_release_stats_refuses_noncanonical_repository(tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "release-stats",
            "--run-dir",
            str(tmp_path / "missing-run"),
            "--confirm-repo",
            "someone-else/osm-polygon-website-tag",
            "--repo-id",
            "someone-else/osm-polygon-website-tag",
        ]
    )

    assert rc == 3  # invalid input
    assert "canonical" in capsys.readouterr().err


def test_cli_analyze_card_refresh_and_finalize_commands_delegate(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    run_dir = tmp_path / "run"
    state = SimpleNamespace(metadata={"status": "enriched"})
    monkeypatch.setattr(verify, "load_run", lambda _run_dir: state)
    monkeypatch.setattr(verify, "analyze_results", lambda _run_dir: SimpleNamespace(value=1))
    monkeypatch.setattr(verify, "build_card", lambda _run_dir: run_dir / "README.md")
    monkeypatch.setattr(
        verify,
        "refresh_card_run",
        lambda _run_dir: SimpleNamespace(ok=True, verification=SimpleNamespace(errors=[])),
    )
    monkeypatch.setattr(
        verify,
        "finalize_run",
        lambda _run_dir: SimpleNamespace(ok=True, receipt={"manifest_digest": "a" * 64}),
    )
    monkeypatch.setattr(
        verify,
        "transition_status",
        lambda _state, new_status: state.metadata.__setitem__("status", new_status),
    )

    assert verify.analyze_command(run_dir) == 0
    assert verify.card_command(run_dir) == 0
    assert verify.refresh_card_command(run_dir) == 0
    assert verify.finalize_command(run_dir) == 0
    assert '"digest"' in capsys.readouterr().out


def test_verify_cli_analysis_and_card_commands_preserve_state_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    state = SimpleNamespace(metadata={"status": STATUS_ENRICHED})
    calls: list[tuple[str, object]] = []

    def analyze_results(path: Path) -> SimpleNamespace:
        calls.append(("analyze_results", path))
        return SimpleNamespace(value=1)

    def build_card(path: Path) -> Path:
        calls.append(("build_card", path))
        return run_dir / "README.md"

    def transition(state_value: SimpleNamespace, new_status: str) -> None:
        assert state_value is state
        calls.append(("transition_status", new_status))
        state.metadata["status"] = new_status

    monkeypatch.setattr(verify, "load_run", lambda path: calls.append(("load_run", path)) or state)
    monkeypatch.setattr(verify, "analyze_results", analyze_results)
    monkeypatch.setattr(verify, "build_card", build_card)
    monkeypatch.setattr(verify, "transition_status", transition)

    assert verify.analyze_command(run_dir) == 0
    assert json.loads(capsys.readouterr().out) == {"value": 1}
    assert state.metadata["status"] == STATUS_ANALYZED

    assert verify.card_command(run_dir) == 0
    assert capsys.readouterr().out == f"{run_dir / 'README.md'}\n"
    assert state.metadata["status"] == STATUS_CARD_BUILT
    assert calls == [
        ("load_run", run_dir),
        ("analyze_results", run_dir),
        ("transition_status", STATUS_ANALYZED),
        ("load_run", run_dir),
        ("build_card", run_dir),
        ("transition_status", STATUS_CARD_BUILT),
    ]


def test_verify_cli_report_commands_forward_paths_and_emit_exact_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(
        verify,
        "refresh_card_run",
        lambda path: (
            calls.append(("refresh_card_run", path))
            or SimpleNamespace(ok=True, verification=SimpleNamespace(errors=["refreshed"]))
        ),
    )
    monkeypatch.setattr(
        verify,
        "finalize_run",
        lambda path: (
            calls.append(("finalize_run", path))
            or SimpleNamespace(ok=True, receipt={"manifest_digest": "final-digest"})
        ),
    )
    monkeypatch.setattr(
        verify,
        "finalize_snapshot",
        lambda path: (
            calls.append(("finalize_snapshot", path))
            or SimpleNamespace(
                ok=True,
                receipt={"manifest_digest": "snapshot-digest"},
                verification=SimpleNamespace(errors=["snapshot-check"]),
            )
        ),
    )
    monkeypatch.setattr(
        verify,
        "verify_results",
        lambda path: calls.append(("verify_results", path)) or SimpleNamespace(ok=True, errors=[]),
    )

    assert verify.refresh_card_command(run_dir) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "errors": ["refreshed"]}
    assert verify.finalize_command(run_dir) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "digest": "final-digest"}
    assert verify.finalize_snapshot_command(run_dir) == 0
    assert json.loads(capsys.readouterr().out) == {
        "digest": "snapshot-digest",
        "errors": ["snapshot-check"],
        "ok": True,
    }
    assert verify.verify_command(run_dir) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "errors": []}

    assert calls == [
        ("refresh_card_run", run_dir),
        ("finalize_run", run_dir),
        ("finalize_snapshot", run_dir),
        ("verify_results", run_dir),
    ]


def test_verify_cli_statistics_commands_render_each_backend_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    geometry = object()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        verify,
        "compute_card_stats",
        lambda path: calls.append(("compute_card_stats", path)) or SimpleNamespace(row_count=2),
    )
    monkeypatch.setattr(
        verify,
        "compute_geometry_stats",
        lambda path: calls.append(("compute_geometry_stats", path)) or geometry,
    )
    monkeypatch.setattr(
        verify,
        "render_geometry_stats",
        lambda result: calls.append(("render_geometry_stats", result)) or "geometry-report",
    )

    assert verify.card_stats_command(run_dir) == 0
    assert json.loads(capsys.readouterr().out) == {"row_count": 2}
    assert verify.geometry_stats_command(run_dir) == 0
    assert capsys.readouterr().out == "geometry-report"
    assert calls == [
        ("compute_card_stats", run_dir),
        ("compute_geometry_stats", run_dir),
        ("render_geometry_stats", geometry),
    ]


@pytest.mark.parametrize(
    ("command_name", "backend_name", "expected"),
    [
        ("refresh_card_command", "refresh_card_run", {"ok": False, "errors": ["broken"]}),
        ("verify_command", "verify_results", {"ok": False, "errors": ["broken"]}),
        ("finalize_command", "finalize_run", {"ok": False, "digest": "digest"}),
        (
            "finalize_snapshot_command",
            "finalize_snapshot",
            {"digest": "digest", "errors": ["broken"], "ok": False},
        ),
    ],
)
def test_verify_cli_report_adapters_exit_one_with_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command_name: str,
    backend_name: str,
    expected: dict[str, object],
) -> None:
    run_dir = tmp_path / "run"
    report = SimpleNamespace(
        ok=False,
        verification=SimpleNamespace(errors=["broken"]),
        errors=["broken"],
        receipt={"manifest_digest": "digest"},
    )
    calls: list[Path] = []

    def backend(path: Path) -> SimpleNamespace:
        calls.append(path)
        return report

    monkeypatch.setattr(verify, backend_name, backend)

    with pytest.raises(typer.Exit) as exc_info:
        getattr(verify, command_name)(run_dir)

    assert exc_info.value.exit_code == 1
    assert calls == [run_dir]
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize(
    ("command_name", "backend_name", "expected_status", "message"),
    [
        (
            "analyze_command",
            "analyze_results",
            "extracting",
            "analyze-results requires enriched state; use run-all for enrichment",
        ),
        ("card_command", "build_card", "enriched", "build-card requires analyzed state"),
    ],
)
def test_verify_cli_commands_refuse_the_wrong_run_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    backend_name: str,
    expected_status: str,
    message: str,
) -> None:
    run_dir = tmp_path / "run"
    state = SimpleNamespace(metadata={"status": expected_status})
    monkeypatch.setattr(verify, "load_run", lambda _path: state)
    monkeypatch.setattr(
        verify, backend_name, lambda _path: pytest.fail("invalid state reached work")
    )

    with pytest.raises(ValueError, match=message):
        getattr(verify, command_name)(run_dir)


def test_cli_run_all_command_closes_progress_and_reports_result(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    events: list[bool] = []
    calls: list[dict[str, object]] = []

    class FakeProgress:
        def __init__(self, *, quiet: bool) -> None:
            assert quiet is False

        def close(self, *, completed: bool) -> None:
            events.append(completed)

    monkeypatch.setattr(run, "ProgressReporter", FakeProgress)
    monkeypatch.setattr(
        run,
        "run_all",
        lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(complete=True, run_dir=tmp_path / "run", sources=3)
        ),
    )

    assert (
        run.run_all_command(
            source_root=tmp_path / "source",
            output_root=tmp_path / "runs",
            run_id="run",
            repo_id="owner/dataset",
            apply=False,
            ensure_repo=False,
            area_workers=1,
            max_in_flight_areas=1,
            fetch_workers=1,
            host_concurrency=3,
            host_delay_seconds=0.5,
            detect_languages=True,
        )
        == 0
    )
    assert events == [True]
    assert calls[0]["detect_languages"] is True
    assert calls[0]["host_policy"] == HostPolicy(concurrency=3, delay_seconds=0.5)
    assert '"complete": true' in capsys.readouterr().out


def test_cli_segment_sentences_loads_the_pinned_model_and_updates_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    run_dir = _setup_run(tmp_path)
    state = load_run(run_dir)
    transition_status(state, "extracting")
    transition_status(state, "extracted")
    transition_status(state, "enriching")
    transition_status(state, "enriched")
    model_dir = tmp_path / "sat-3l-sm"
    model_dir.mkdir()
    loaded: list[tuple[Path, str]] = []
    splitter = SimpleNamespace(
        identity=ModelIdentity("segment-any-text/sat-3l-sm", "sat-3l-sm", "abc1234", "a" * 64),
        split=lambda texts: [[text] for text in texts],
    )

    monkeypatch.setattr(sentences, "require_under_data_root", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(
        sentences,
        "load_sat_splitter_from_path",
        lambda path, *, revision: loaded.append((path, revision)) or splitter,
    )
    monkeypatch.setattr(sentences, "shard_needs_sentence_segmentation", lambda _path: True)

    observed: dict[str, object] = {}

    def segment(_path, *, splitter, batch_rows, time_budget_seconds):
        observed.update(
            splitter=splitter, batch_rows=batch_rows, time_budget_seconds=time_budget_seconds
        )
        return SimpleNamespace(
            row_count=1,
            shard_sha256="b" * 64,
            changed=True,
            completed=True,
            processed_rows=1,
        )

    monkeypatch.setattr(sentences, "run_sentence_shards", _passthrough_sentence_run(segment))

    assert (
        main(
            [
                "segment-sentences",
                "--run-dir",
                str(run_dir),
                "--model-dir",
                str(model_dir),
                "--model-revision",
                "abc1234",
                "--batch-rows",
                "4",
            ]
        )
        == 0
    )

    assert loaded == [(model_dir, "abc1234")]
    assert observed["splitter"] is splitter
    assert observed["batch_rows"] == 4
    assert observed["time_budget_seconds"] is None
    assert json.loads(capsys.readouterr().out) == {"changed_shards": 1, "run_dir": str(run_dir)}
    assert load_run(run_dir).sources["monaco-latest.osm.pbf"]["public_shard_sha256"] == "b" * 64


def _passthrough_sentence_run(segment):
    """Run the real orchestrator with an injected per-shard segmenter."""

    def run(shards, **kwargs):
        return sentence_run.run_sentence_shards(shards, segment=segment, **kwargs)

    return run


def test_cli_segment_sentences_reports_nothing_to_do(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """A fully segmented run must not load the model at all."""
    run_dir = _setup_run(tmp_path)
    monkeypatch.setattr(sentences, "require_under_data_root", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(sentences, "shard_needs_sentence_segmentation", lambda _path: False)
    monkeypatch.setattr(
        sentences,
        "load_sat_splitter_from_path",
        lambda *_args, **_kwargs: pytest.fail("no shard needs the model"),
    )

    assert (
        main(
            [
                "segment-sentences",
                "--run-dir",
                str(run_dir),
                "--model-dir",
                str(tmp_path / "sat"),
                "--model-revision",
                "abc1234",
                "--time-budget-seconds",
                "5",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {
        "changed_shards": 0,
        "completed": True,
        "processed_rows": 0,
        "run_dir": str(run_dir),
    }


def test_cli_sentence_grid5000_commands_use_the_explicit_bundle_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    bundle = SimpleNamespace(payload=lambda: {"shards": [{"name": "source.parquet"}]})
    result = SimpleNamespace(payload=lambda: {"completed": True, "shards": []})
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(grid5000, "require_under_data_root", lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(
        grid5000,
        "prepare_sentence_bundle",
        lambda *args, **kwargs: calls.append(("prepare", args, kwargs)) or bundle,
    )
    monkeypatch.setattr(
        grid5000,
        "run_sentence_bundle",
        lambda *args, **kwargs: calls.append(("run", args, kwargs)) or result,
    )
    monkeypatch.setattr(
        grid5000,
        "sync_sentence_bundle",
        lambda *args, **kwargs: calls.append(("sync", args, kwargs)) or result,
    )
    monkeypatch.setattr(
        grid5000,
        "load_sat_splitter_from_path",
        lambda model_dir, *, revision: SimpleNamespace(model_dir=model_dir, revision=revision),
    )
    monkeypatch.setattr(
        grid5000,
        "load_sentence_bundle",
        lambda bundle_dir: SimpleNamespace(
            model=SimpleNamespace(filename="sat-3l-sm", revision="137da05")
        ),
    )

    assert (
        main(
            [
                "grid5000-prepare-sentences",
                "--run-dir",
                str(tmp_path / "run"),
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--model-dir",
                str(tmp_path / "sat-3l-sm"),
                "--model-revision",
                "137da05",
                "--commit",
                "abc123",
            ]
        )
        == 0
    )
    assert main(["grid5000-run-sentences", "--bundle-dir", str(tmp_path / "bundle")]) == 0
    assert (
        main(
            [
                "grid5000-sync-sentences",
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
        == 0
    )

    assert [name for name, _args, _kwargs in calls] == ["prepare", "run", "sync"]
    assert calls[0][1] == (tmp_path / "run", tmp_path / "bundle")
    assert calls[0][2]["model_dir"] == tmp_path / "sat-3l-sm"
    assert calls[0][2]["model_revision"] == "137da05"
    assert calls[1][1] == (tmp_path / "bundle",)
    assert calls[2][1] == (tmp_path / "bundle", tmp_path / "run")
    assert "source.parquet" in capsys.readouterr().out


def test_cli_json_serialization_is_sorted_and_path_safe(capsys) -> None:
    cli._json({"z": Path("run"), "a": 1}, sort_keys=True)

    assert capsys.readouterr().out == '{\n  "a": 1,\n  "z": "run"\n}\n'


def test_cli_json_serialization_preserves_order_by_default(capsys) -> None:
    cli._json({"z": 1, "a": 2})

    assert capsys.readouterr().out == '{\n  "z": 1,\n  "a": 2\n}\n'


def test_cli_language_shard_runner_records_only_completed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shards = [Path("a.parquet"), Path("b.parquet")]
    detector = object()
    state = RunState(Path("run"), "run")
    results = iter(
        (
            SimpleNamespace(
                row_count=3,
                shard_sha256="a" * 64,
                changed=True,
                completed=True,
                processed_rows=3,
            ),
            SimpleNamespace(
                row_count=2,
                shard_sha256="b" * 64,
                changed=False,
                completed=True,
                processed_rows=2,
            ),
        )
    )
    calls: list[tuple[Path, object, int, float | None]] = []
    records: list[tuple[Path, object]] = []

    def detect(
        shard: Path,
        *,
        detector: object,
        batch_rows: int,
        time_budget_seconds: float | None,
    ) -> object:
        calls.append((shard, detector, batch_rows, time_budget_seconds))
        return next(results)

    monkeypatch.setattr(languages, "detect_language_shard", detect)
    monkeypatch.setattr(
        languages,
        "_record_completed_language_shard",
        lambda received_state, shard, result: records.append((shard, result)),
    )

    assert languages._run_language_shards(
        shards,
        detector=detector,
        state=state,
        batch_rows=16,
        time_budget_seconds=None,
    ) == languages._LanguageRunProgress(1, 5, completed=True)
    assert calls == [(shards[0], detector, 16, None), (shards[1], detector, 16, None)]
    assert [shard for shard, _result in records] == shards


def test_cli_language_shard_runner_stops_on_incomplete_or_exhausted_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = Path("a.parquet")
    detector = object()
    state = RunState(Path("run"), "run")
    records: list[object] = []
    monkeypatch.setattr(
        languages,
        "detect_language_shard",
        lambda *_args, **_kwargs: SimpleNamespace(
            row_count=4,
            shard_sha256="a" * 64,
            changed=True,
            completed=False,
            processed_rows=4,
        ),
    )
    monkeypatch.setattr(
        languages,
        "_record_completed_language_shard",
        lambda *_args: records.append(True),
    )

    assert languages._run_language_shards(
        [shard],
        detector=detector,
        state=state,
        batch_rows=8,
        time_budget_seconds=None,
    ) == languages._LanguageRunProgress(0, 4, completed=False)
    assert records == []

    monkeypatch.setattr(languages, "_remaining_language_budget", lambda *_args, **_kwargs: 0.0)
    assert languages._run_language_shards(
        [shard],
        detector=detector,
        state=state,
        batch_rows=8,
        time_budget_seconds=1.0,
    ) == languages._LanguageRunProgress(0, 0, completed=False)


def test_cli_language_private_helpers_preserve_budget_and_payload_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(languages, "monotonic", lambda: 8.5)
    assert languages._language_start_time(None) is None
    assert languages._language_start_time(2.0) == 8.5
    assert languages._remaining_language_budget(None, started_at=None) is None
    assert languages._remaining_language_budget(10.0, started_at=8.0) == 9.5
    assert not languages._language_budget_exhausted(None)
    assert not languages._language_budget_exhausted(0.1)
    assert languages._language_budget_exhausted(0.0)
    assert languages._language_budget_exhausted(-0.1)
    assert languages._language_command_payload(
        Path("run"), changed_shards=1, completed=True, processed_rows=2, bounded=False
    ) == {"changed_shards": 1, "run_dir": "run"}
    assert languages._language_command_payload(
        Path("run"), changed_shards=1, completed=False, processed_rows=2, bounded=True
    ) == {
        "changed_shards": 1,
        "completed": False,
        "processed_rows": 2,
        "run_dir": "run",
    }


@pytest.mark.parametrize(
    ("time_budget_seconds", "started_at"),
    [(None, 8.0), (10.0, None)],
)
def test_cli_remaining_budget_requires_both_clock_inputs(
    time_budget_seconds: float | None,
    started_at: float | None,
) -> None:
    assert languages._remaining_language_budget(time_budget_seconds, started_at=started_at) is None


def test_cli_language_private_state_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    state = RunState(
        Path("run"),
        "run",
        sources={"a.osm.pbf": SourceManifestEntry(filename="a.osm.pbf", size_bytes=0, mtime_ns=0)},
        metadata={"status": STATUS_ANALYZED},
    )
    languages._validate_language_shard_membership(state, [Path("a.parquet")])
    with pytest.raises(ValueError, match="not in the source manifest"):
        languages._validate_language_shard_membership(state, [Path("missing.parquet")])

    frozen = RunState(
        Path("run"),
        "run",
        metadata={"status": STATUS_COMPLETE, "snapshot_status": "done"},
    )
    with pytest.raises(ValueError, match="frozen snapshot"):
        languages._reject_frozen_language_run(frozen)
    languages._reject_frozen_language_run(
        RunState(Path("run"), "run", metadata={"status": STATUS_COMPLETE})
    )

    transitions: list[tuple[object, str]] = []
    monkeypatch.setattr(
        languages,
        "transition_status",
        lambda received_state, status: transitions.append((received_state, status)),
    )
    languages._prepare_language_command_state(state)
    assert transitions == [(state, STATUS_ENRICHING)]
    transitions.clear()
    enriching = RunState(Path("run"), "run", metadata={"status": STATUS_ENRICHING})
    languages._prepare_language_command_state(enriching)
    assert transitions == []
    languages._finish_language_command_state(enriching)
    assert transitions == [(enriching, STATUS_ENRICHED)]
    with pytest.raises(ValueError) as exc_info:
        languages._prepare_language_command_state(
            RunState(Path("run"), "run", metadata={"status": "initialized"})
        )
    assert str(exc_info.value) == "detect-languages requires an extracted/enriched run"


@pytest.mark.parametrize(
    ("status", "snapshot_status"),
    [(STATUS_COMPLETE, "pending"), (STATUS_ENRICHED, "done")],
)
def test_cli_frozen_snapshot_guard_requires_both_markers(
    status: str,
    snapshot_status: str,
) -> None:
    languages._reject_frozen_language_run(
        RunState(
            Path("run"),
            "run",
            metadata={"status": status, "snapshot_status": snapshot_status},
        )
    )


@pytest.mark.parametrize("status", [STATUS_CARD_BUILT, STATUS_COMPLETE])
def test_cli_language_state_preparation_accepts_late_statuses(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = RunState(Path("run"), "run", metadata={"status": status})
    transitions: list[str] = []
    monkeypatch.setattr(
        languages, "transition_status", lambda _state, value: transitions.append(value)
    )

    languages._prepare_language_command_state(state)

    assert transitions == [STATUS_ENRICHING]


def test_cli_main_preserves_app_exit_and_error_contracts(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    calls: list[dict[str, object]] = []

    def app_ok(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli, "app", app_ok)
    assert cli.main(["ok"]) == 0
    assert calls == [
        {
            "args": ["ok"],
            "prog_name": "osm-polygon-website-tag",
            "standalone_mode": False,
        }
    ]

    monkeypatch.setattr(cli, "app", lambda **_kwargs: (_ for _ in ()).throw(SystemExit(None)))
    assert cli.main(["exit-none"]) == 0
    monkeypatch.setattr(cli, "app", lambda **_kwargs: (_ for _ in ()).throw(SystemExit(7)))
    assert cli.main(["exit-seven"]) == 7

    monkeypatch.setattr(
        cli,
        "app",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad input")),
    )
    assert cli.main(["bad"]) == 3
    assert capsys.readouterr().err == "error: bad input\n"


def test_cli_records_completed_language_shard_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        languages,
        "update_public_shard_metadata",
        lambda state, **kwargs: calls.append((state, kwargs)),
    )
    state = RunState(Path("run"), "run")
    result = SimpleNamespace(row_count=7, shard_sha256="a" * 64)

    languages._record_completed_language_shard(state, Path("monaco-latest.parquet"), result)

    assert calls == [
        (
            state,
            {
                "filename": "monaco-latest.osm.pbf",
                "row_count": 7,
                "shard_sha256": "a" * 64,
            },
        )
    ]


def _raising_app(error: BaseException) -> Any:
    def app(**_kwargs: object) -> None:
        raise error

    return app


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValueError("bad state"), 3),
        (json.JSONDecodeError("corrupt run.json", "{", 0), 3),
        (FileNotFoundError("missing run.json"), 3),
        (
            HfHubHTTPError(
                "401 Unauthorized",
                response=httpx.Response(401, request=httpx.Request("GET", "https://hf.co")),
            ),
            4,
        ),
        (httpx.ConnectError("DNS lookup failed"), 4),
    ],
)
def test_each_error_class_exits_with_its_documented_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
) -> None:
    monkeypatch.delenv(cli.DEBUG_ENV, raising=False)
    monkeypatch.setattr(cli, "app", _raising_app(error))

    assert cli.main(["x"]) == code
    err = capsys.readouterr().err
    assert err == f"error: {error}\n"
    assert "Traceback" not in err


@pytest.mark.parametrize("error", [KeyboardInterrupt(), typer.Abort()])
def test_ctrl_c_exits_130_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: BaseException
) -> None:
    monkeypatch.setattr(cli, "app", _raising_app(error))

    assert cli.main(["x"]) == 130
    assert capsys.readouterr().err == "interrupted\n"


def test_a_click_usage_error_keeps_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["verify-results"]) == 2
    assert "Missing option '--run-dir'" in capsys.readouterr().err


def test_an_exit_code_returned_by_the_app_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "app", lambda **_kwargs: 5)

    assert cli.main(["x"]) == 5


def test_a_missing_run_dir_is_an_invalid_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["detect-languages", "--run-dir", str(tmp_path / "missing")])

    assert code == 3
    assert "Traceback" not in capsys.readouterr().err


def test_debug_flag_brings_the_traceback_back(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cli.main(["--debug", "detect-languages", "--run-dir", str(tmp_path / "missing")])
    # One invocation only.
    assert cli._debug == {"enabled": False}


def test_debug_environment_variable_brings_the_traceback_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cli.DEBUG_ENV, "1")
    monkeypatch.setattr(cli, "app", _raising_app(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        cli.main(["x"])


def test_debug_environment_variable_must_be_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cli.DEBUG_ENV, "0")
    monkeypatch.setattr(cli, "app", _raising_app(OSError("disk")))

    assert cli.main(["x"]) == 3


def test_the_exit_code_table_is_documented() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "cli.md").read_text(encoding="utf-8")

    for code in {code for _type, code in cli._EXIT_CODES} | {0, 1, 2, cli.EXIT_INTERRUPTED}:
        assert f"| `{code}` |" in doc
    assert cli.DEBUG_ENV in doc


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    from importlib.metadata import version

    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"{version('osm-polygon-website-tag')}\n"


def _stderr_for(
    flags: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setenv("OSM_POLY_DATA_DIR", "/data-root")
    monkeypatch.setattr(verify, "compute_card_stats", lambda _run_dir: SimpleNamespace(ok=True))
    assert cli.main([*flags, "card-stats", "--run-dir", "/run"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}
    return captured.err


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], ""),
        (["-q"], ""),
        (["-v"], "INFO osm_polygon_website_tag: osm-polygon-website-tag {version}\n"),
        (
            ["-vv"],
            "INFO osm_polygon_website_tag: osm-polygon-website-tag {version}\n"
            "DEBUG osm_polygon_website_tag: data root: custom\n",
        ),
        (
            ["-v", "-v", "-v"],
            "INFO osm_polygon_website_tag: osm-polygon-website-tag {version}\n"
            "DEBUG osm_polygon_website_tag: data root: custom\n",
        ),
    ],
)
def test_verbosity_flags_control_the_stderr_log(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    expected: str,
) -> None:
    from importlib.metadata import version

    err = _stderr_for(flags, monkeypatch, capsys)

    assert err == expected.format(version=version("osm-polygon-website-tag"))


def test_quiet_logs_only_errors() -> None:
    assert cli._log_level(0, quiet=True) == logging.ERROR
    assert cli._log_level(0, quiet=False) == logging.WARNING


def test_verbose_and_quiet_cannot_be_combined(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["-v", "-q", "card-stats", "--run-dir", "/run"]) == 2
    assert "--verbose and --quiet cannot be combined" in capsys.readouterr().err


def test_global_options_last_one_invocation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stderr_for(["-q", "-vv"][:1], monkeypatch, capsys)

    assert cli._quiet == {"enabled": False}
    assert cli._LOGGER.handlers == []
    assert cli._LOGGER.level == logging.NOTSET


def test_run_all_silences_progress_when_quiet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[bool] = []

    class Reporter:
        def __init__(self, *, quiet: bool) -> None:
            seen.append(quiet)

        def close(self, *, completed: bool) -> None:
            pass

    def fail_run(**_kwargs: object) -> None:
        raise ValueError("stop")

    monkeypatch.setattr(run, "ProgressReporter", Reporter)
    monkeypatch.setattr(run, "run_all", fail_run)
    argv = ["run-all", "--source-root", str(tmp_path), "--output-root", str(tmp_path / "o")]
    argv += ["--run-id", "r"]

    assert cli.main(["-q", *argv]) == 3
    assert cli.main(argv) == 3
    assert seen == [True, False]


def test_create_repo_is_a_dry_run_without_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    writes: list[object] = []
    checked: list[str] = []
    monkeypatch.setattr(publish, "create_repo", lambda **kwargs: writes.append(kwargs))
    monkeypatch.setattr(publish, "repo_exists", lambda *, repo_id: checked.append(repo_id) or True)

    assert main(["create-repo", "--repo-id", "owner/name"]) == 0

    assert writes == []
    assert checked == ["owner/name"]
    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "exists": True,
        "repo_id": "owner/name",
    }


def test_create_repo_with_apply_creates_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        publish, "create_repo", lambda **kwargs: calls.append(kwargs) or "owner/name"
    )

    assert main(["create-repo", "--repo-id", "owner/name", "--exist-ok", "--apply"]) == 0

    assert calls == [{"repo_id": "owner/name", "exist_ok": True}]
    assert capsys.readouterr().out == "owner/name\n"


def test_repo_exists_asks_the_hub_with_the_resolved_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    seen: list[tuple[object, ...]] = []

    class FakeApi:
        def __init__(self, *, token: str | None) -> None:
            seen.append(("token", token))

        def repo_exists(self, repo_id: str, *, repo_type: str) -> bool:
            seen.append((repo_id, repo_type))
            return False

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(publish_module, "resolve_hf_token", lambda: "tok")

    assert publish_module.repo_exists(repo_id="o/n") is False
    assert seen == [("token", "tok"), ("o/n", "dataset")]


def test_every_option_of_every_command_has_help_text() -> None:
    import typer.main

    missing: list[str] = []

    def walk(command: object, path: str) -> None:
        missing.extend(
            f"{path or '<root>'} {param.opts[0]}"
            for param in getattr(command, "params", [])
            if param.param_type_name == "option" and not getattr(param, "help", None)
        )
        for name, sub in getattr(command, "commands", {}).items():
            walk(sub, f"{path} {name}".strip())

    walk(typer.main.get_command(app), "")

    assert missing == []


@pytest.mark.parametrize(
    "command", ["init", "extract", "run-all", "publish", "release-stats", "grid5000-prepare"]
)
def test_main_workflow_commands_show_an_example(command: str) -> None:
    import typer.main

    click_command = typer.main.get_command(app).commands[command]  # ty: ignore[unresolved-attribute]

    assert click_command.epilog.startswith("Example: osm-polygon-website-tag ")


def test_debug_log_never_shows_the_data_root_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "/data-root" not in _stderr_for(["-vv"], monkeypatch, capsys)


def test_debug_log_says_when_the_default_data_root_is_used(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(verify, "compute_card_stats", lambda _run_dir: SimpleNamespace(ok=True))
    monkeypatch.setenv("OSM_POLY_DATA_DIR", "  ")

    assert cli.main(["-vv", "card-stats", "--run-dir", "/run"]) == 0

    assert "data root: default\n" in capsys.readouterr().err


def test_publish_trackio_without_the_package_is_a_clean_classified_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from osm_polygon_website_tag.publishing import trackio

    def missing(_name: str) -> object:
        raise ModuleNotFoundError("trackio")

    monkeypatch.setattr(publish, "build_trackio_snapshot", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(trackio.importlib, "import_module", missing)

    code = cli.main(["publish-trackio", "--run-dir", str(tmp_path), "--apply"])

    assert code == cli.EXIT_MISSING_DEPENDENCY == 5
    assert "error: Trackio publishing requires the optional 'trackio' package" in (
        capsys.readouterr().err
    )

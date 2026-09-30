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
    callbacks = {
        command.name: command.callback for command in app.registered_commands if command.name
    }

    assert callbacks == {
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

    assert 
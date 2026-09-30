"""Behavioral boundaries for stage-specific CLI adapters."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import osm_polygon_website_tag.application.cli as cli
from osm_polygon_website_tag.application.cli import languages, publish, run, sentences
from osm_polygon_website_tag.web.politeness import HostPolicy


def test_extract_adapter_validates_inventory_and_forwards_worker_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    state_file = run_dir / "manifests" / "run.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{}", encoding="utf-8")
    source = tmp_path / "source.osm.pbf"
    state = SimpleNamespace(metadata={"status": "initialized"})
    fingerprint = SimpleNamespace(filename=source.name, size_bytes=4, mtime_ns=17)
    events: list[tuple[object, ...]] = []

    monkeypatch.setattr(run, "load_run", lambda path: events.append(("load", path)) or state)
    monkeypatch.setattr(
        run,
        "snapshot_source_fingerprint",
        lambda path: events.append(("fingerprint", path)) or fingerprint,
    )
    monkeypatch.setattr(
        run,
        "expected_source_inventory",
        lambda path: (
            events.append(("inventory", path))
            or [{"filename": source.name, "size_bytes": 4, "mtime_ns": 17}]
        ),
    )

    def transition(current: object, status: str) -> None:
        assert current is state
        events.append(("status", status))
        state.metadata["status"] = status

    monkeypatch.setattr(run, "transition_status", transition)
    monkeypatch.setattr(
        run,
        "extract_pbf",
        lambda *args, **kwargs: events.append(("extract", *args, kwargs)),
    )
    monkeypatch.setattr(run, "source_inventory_matches", lambda path: True)

    assert run.extract_command(source, run_dir, area_workers=3, max_in_flight_areas=11) == 0

    assert events == [
        ("load", run_dir),
        ("fingerprint", source),
        ("inventory", run_dir),
        ("status", "extracting"),
        (
            "extract",
            source,
            run_dir,
            {"run_state": state, "area_workers": 3, "max_in_flight_areas": 11},
        ),
        ("status", "extracted"),
    ]


def test_init_rejects_expected_sources_outside_the_declared_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    outside_source = tmp_path / "outside.osm.pbf"
    outside_source.write_bytes(b"test")
    output_root = tmp_path / "runs"
    monkeypatch.setattr(
        run,
        "snapshot_source_fingerprint",
        lambda _path: pytest.fail("invalid source must be rejected before fingerprinting"),
    )

    assert (
        cli.main(
            [
                "init",
                "--output-root",
                str(output_root),
                "--source-root",
                str(source_root),
                "--expected-source",
                str(outside_source),
            ]
        )
        == 3
    )
    assert "expected source is outside source root" in capsys.readouterr().err
    assert not output_root.exists()


def test_run_all_adapter_forwards_every_option_and_serializes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "runs"
    events: list[tuple[object, ...]] = []
    calls: list[dict[str, object]] = []
    reporters: list[object] = []

    class Reporter:
        def __init__(self, *, quiet: bool) -> None:
            reporters.append(self)
            events.append(("reporter", quiet))

        def close(self, *, completed: bool) -> None:
            events.append(("close", completed))

    monkeypatch.setattr(run, "ProgressReporter", Reporter)
    monkeypatch.setattr(run, "_configured_hf_dataset_repo", lambda repo_id: repo_id)

    def backend(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(complete=True, run_dir=output_root / "run-1", sources=2)

    monkeypatch.setattr(run, "run_all", backend)

    assert (
        run.run_all_command(
            source_root=source_root,
            output_root=output_root,
            run_id="run-1",
            repo_id="owner/dataset",
            apply=True,
            ensure_repo=True,
            area_workers=2,
            max_in_flight_areas=7,
            fetch_workers=9,
            host_concurrency=3,
            host_delay_seconds=0.25,
            detect_languages=True,
        )
        == 0
    )

    assert events == [("reporter", False), ("close", True)]
    assert calls == [
        {
            "source_root": source_root,
            "output_root": output_root,
            "run_id": "run-1",
            "repo_id": "owner/dataset",
            "apply": True,
            "ensure_repo": True,
            "progress": reporters[0],
            "area_workers": 2,
            "max_in_flight_areas": 7,
            "fetch_workers": 9,
            "host_policy": HostPolicy(concurrency=3, delay_seconds=0.25),
            "detect_languages": True,
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "complete": True,
        "run_dir": str(output_root / "run-1"),
        "sources": 2,
    }


def test_run_all_closes_progress_as_incomplete_when_backend_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class Reporter:
        def __init__(self, *, quiet: bool) -> None:
            assert quiet is False

        def close(self, *, completed: bool) -> None:
            closed.append(completed)

    def fail(**_kwargs: object) -> None:
        raise RuntimeError("run stopped")

    monkeypatch.setattr(run, "ProgressReporter", Reporter)
    monkeypatch.setattr(run, "run_all", fail)

    with pytest.raises(RuntimeError, match="run stopped"):
        run.run_all_command(
            source_root=tmp_path / "source",
            output_root=tmp_path / "runs",
            run_id="run-1",
        )
    assert closed == [False]


def test_run_all_requires_apply_before_ensuring_remote_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        run,
        "ProgressReporter",
        lambda **_kwargs: pytest.fail("invalid options must be rejected first"),
    )

    with pytest.raises(ValueError, match="--ensure-repo requires --apply"):
        run.run_all_command(
            source_root=tmp_path / "source",
            output_root=tmp_path / "runs",
            run_id="run-1",
            ensure_repo=True,
        )


def test_language_command_skips_model_loading_when_all_shards_are_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    shard = run_dir / "polygons" / "site.parquet"
    shard.parent.mkdir(parents=True)
    shard.touch()
    state = SimpleNamespace(metadata={"status": "enriched"}, sources={"site.osm.pbf": {}})
    loaded: list[Path] = []
    monkeypatch.setattr(languages, "require_under_data_root", lambda path, **_kwargs: path)
    monkeypatch.setattr(languages, "load_run", lambda path: loaded.append(path) or state)
    monkeypatch.setattr(languages, "shard_needs_language_detection", lambda _path: False)
    monkeypatch.setattr(
        languages,
        "load_glotlid_detector",
        lambda _path: pytest.fail("completed shards must not load the model"),
    )

    assert languages.detect_languages_command(run_dir, time_budget_seconds=10.0) == 0

    assert loaded == [run_dir]
    assert json.loads(capsys.readouterr().out) == {
        "changed_shards": 0,
        "completed": True,
        "processed_rows": 0,
        "run_dir": str(run_dir),
    }


def test_language_command_rejects_shards_outside_the_source_manifest_before_model_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    shard = run_dir / "polygons" / "orphan.parquet"
    shard.parent.mkdir(parents=True)
    shard.touch()
    state = SimpleNamespace(metadata={"status": "enriched"}, sources={})
    monkeypatch.setattr(languages, "require_under_data_root", lambda path, **_kwargs: path)
    monkeypatch.setattr(languages, "load_run", lambda _path: state)
    monkeypatch.setattr(
        languages,
        "load_glotlid_detector",
        lambda _path: pytest.fail("an unlisted shard must be rejected before model loading"),
    )

    with pytest.raises(ValueError, match="language shard is not in the source manifest"):
        languages.detect_languages_command(run_dir)


def test_sentence_command_forwards_model_and_budget_and_reports_partial_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    shard = run_dir / "polygons" / "site.parquet"
    shard.parent.mkdir(parents=True)
    shard.touch()
    model_dir = tmp_path / "data" / "models" / "sat"
    state = SimpleNamespace(metadata={"status": "enriched"}, sources={"site.osm.pbf": {}})
    validated: list[tuple[Path, str]] = []
    splitter = object()
    load_calls: list[tuple[Path, str]] = []
    run_calls: list[tuple[list[Path], dict[str, object]]] = []
    recorded: list[tuple[object, Path, object]] = []

    def require(path: Path, *, label: str) -> Path:
        validated.append((path, label))
        return path

    def load_splitter(path: Path, *, revision: str) -> object:
        load_calls.append((path, revision))
        return splitter

    def run_shards(paths: list[Path], **kwargs: object) -> SimpleNamespace:
        run_calls.append((paths, kwargs))
        return SimpleNamespace(changed_shards=1, completed=False, processed_rows=5)

    monkeypatch.setattr(sentences, "require_under_data_root", require)
    monkeypatch.setattr(sentences, "load_run", lambda _path: state)
    monkeypatch.setattr(sentences, "shard_needs_sentence_segmentation", lambda _path: True)
    monkeypatch.setattr(
        sentences,
        "_record_completed_language_shard",
        lambda current, path, result: recorded.append((current, path, result)),
    )
    monkeypatch.setattr(sentences, "load_sat_splitter_from_path", load_splitter)
    monkeypatch.setattr(sentences, "run_sentence_shards", run_shards)

    assert (
        sentences.segment_sentences_command(
            run_dir,
            model_dir=model_dir,
            model_revision="revision-7",
            batch_rows=3,
            time_budget_seconds=2.5,
        )
        == 0
    )

    assert validated == [
        (run_dir, "run directory"),
        (model_dir, "SaT model directory"),
    ]
    assert load_calls == [(model_dir, "revision-7")]
    assert len(run_calls) == 1
    paths, kwargs = run_calls[0]
    assert paths == [shard]
    assert kwargs.keys() == {
        "splitter",
        "record",
        "batch_rows",
        "time_budget_seconds",
    }
    assert kwargs["splitter"] is splitter
    assert kwargs["batch_rows"] == 3
    assert kwargs["time_budget_seconds"] == 2.5
    callback = cast(Callable[[Path, object], object], kwargs["record"])
    result = object()
    callback(shard, result)
    assert recorded == [(state, shard, result)]
    assert json.loads(capsys.readouterr().out) == {
        "changed_shards": 1,
        "completed": False,
        "processed_rows": 5,
        "run_dir": str(run_dir),
    }


def test_sentence_command_validates_options_before_reading_the_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_rows must be positive"):
        sentences.segment_sentences_command(
            tmp_path / "missing-run",
            model_dir=tmp_path / "missing-model",
            model_revision="revision-1",
            batch_rows=0,
        )


def test_sentence_command_rejects_frozen_snapshots_before_loading_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    shard = run_dir / "polygons" / "site.parquet"
    shard.parent.mkdir(parents=True)
    shard.touch()
    state = SimpleNamespace(
        metadata={"status": "complete", "snapshot_status": "done"},
        sources={"site.osm.pbf": {}},
    )
    monkeypatch.setattr(sentences, "require_under_data_root", lambda path, **_kwargs: path)
    monkeypatch.setattr(sentences, "load_run", lambda _path: state)
    monkeypatch.setattr(
        sentences,
        "load_sat_splitter_from_path",
        lambda *_args, **_kwargs: pytest.fail("a frozen snapshot must not load the model"),
    )

    with pytest.raises(ValueError, match="cannot add languages to a frozen snapshot"):
        sentences.segment_sentences_command(
            run_dir,
            model_dir=tmp_path / "data" / "models" / "sat",
            model_revision="revision-1",
        )


def test_publish_adapter_keeps_upload_dry_run_and_forwards_explicit_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    calls: list[tuple[Path, str | None, bool]] = []
    monkeypatch.setattr(
        publish,
        "_configured_hf_dataset_repo",
        lambda repo_id: "resolved/dataset" if repo_id is None else repo_id,
    )

    def upload(path: Path, *, repo_id: str, dry_run: bool) -> SimpleNamespace:
        calls.append((path, repo_id, dry_run))
        return SimpleNamespace(artifact_paths=(Path("one"), Path("two")))

    monkeypatch.setattr(publish, "publish_to_hf", upload)

    assert publish.publish_command(run_dir, repo_id="explicit/dataset") == 0

    assert calls == [(run_dir, "explicit/dataset", True)]
    assert json.loads(capsys.readouterr().out) == {"artifact_count": 2, "dry_run": True}


def test_publish_adapter_applies_only_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    calls: list[tuple[Path, str | None, bool]] = []
    monkeypatch.setattr(publish, "_configured_hf_dataset_repo", lambda repo_id: repo_id)
    monkeypatch.setattr(
        publish,
        "publish_to_hf",
        lambda path, *, repo_id, dry_run: (
            calls.append((path, repo_id, dry_run))
            or SimpleNamespace(artifact_paths=(Path("one"), Path("two")))
        ),
    )

    assert publish.publish_command(run_dir, repo_id="owner/dataset", apply=True) == 0

    assert calls == [(run_dir, "owner/dataset", False)]
    assert json.loads(capsys.readouterr().out) == {"artifact_count": 2, "dry_run": False}


def test_publish_plan_reports_resolved_artifact_count_and_readme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    readme = run_dir / "README.md"
    calls: list[tuple[Path, str | None]] = []
    monkeypatch.setattr(publish, "_configured_hf_dataset_repo", lambda repo_id: "owner/configured")
    monkeypatch.setattr(
        publish,
        "build_publish_plan",
        lambda path, *, repo_id: (
            calls.append((path, repo_id))
            or SimpleNamespace(artifact_paths=(Path("one"), Path("two")), readme_path=readme)
        ),
    )

    assert publish.publish_plan_command(run_dir) == 0

    assert calls == [(run_dir, "owner/configured")]
    assert json.loads(capsys.readouterr().out) == {
        "repo_id": None,
        "artifact_count": 2,
        "readme": str(readme),
    }


def test_trackio_adapter_previews_resolved_snapshot_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    snapshot = SimpleNamespace(
        run_name="dataset-1",
        manifest_digest="digest",
        metrics={"row_count": 4},
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        publish, "_configured_hf_dataset_repo", lambda repo_id: "configured/dataset"
    )
    monkeypatch.setattr(
        publish,
        "build_trackio_snapshot",
        lambda path, *, dataset_repo: calls.append((path, dataset_repo)) or snapshot,
    )
    monkeypatch.setattr(
        publish,
        "publish_trackio_snapshot",
        lambda *_args, **_kwargs: pytest.fail("preview must not publish"),
    )

    assert publish.publish_trackio_command(run_dir, apply=False) == 0

    assert calls == [(run_dir, "configured/dataset")]
    assert json.loads(capsys.readouterr().out) == {
        "dry_run": True,
        "manifest_digest": "digest",
        "metrics": {"row_count": 4},
        "project": "osm-polygon-website-tag",
        "remote": None,
        "run_name": "dataset-1",
        "space_id": "NoeFlandre/osm-polygon-website-tag-metrics",
    }


def test_trackio_adapter_publishes_the_selected_snapshot_when_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    snapshot = SimpleNamespace(
        run_name="dataset-2",
        manifest_digest="digest-2",
        metrics={"row_count": 8},
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(publish, "_configured_hf_dataset_repo", lambda repo_id: repo_id)
    monkeypatch.setattr(
        publish,
        "build_trackio_snapshot",
        lambda path, *, dataset_repo: calls.append(("build", path, dataset_repo)) or snapshot,
    )
    monkeypatch.setattr(
        publish,
        "publish_trackio_snapshot",
        lambda value, *, space_id, project: (
            calls.append(("publish", value, space_id, project))
            or {"revision": "published-revision"}
        ),
    )

    assert (
        publish.publish_trackio_command(
            run_dir,
            space_id="owner/metrics",
            project="custom-project",
            dataset_repo="owner/dataset",
            apply=True,
        )
        == 0
    )

    assert calls == [
        ("build", run_dir, "owner/dataset"),
        ("publish", snapshot, "owner/metrics", "custom-project"),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "dry_run": False,
        "space_id": "owner/metrics",
        "project": "custom-project",
        "run_name": "dataset-2",
        "manifest_digest": "digest-2",
        "metrics": {"row_count": 8},
        "remote": {"revision": "published-revision"},
    }


def test_release_stats_adapter_forwards_confirmation_and_serializes_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = SimpleNamespace(to_payload=lambda: {"published": False, "digest": "digest"})
    calls: list[tuple[object, ...]] = []

    def release(path: Path, *, confirm_repo: str, repo_id: str, apply: bool) -> SimpleNamespace:
        calls.append((path, confirm_repo, repo_id, apply))
        return report

    monkeypatch.setattr(publish, "release_card_and_stats", release)

    assert (
        publish.release_stats_command(
            tmp_path / "run",
            confirm_repo="owner/dataset",
            repo_id="owner/dataset",
            apply=False,
        )
        == 0
    )

    assert calls == [(tmp_path / "run", "owner/dataset", "owner/dataset", False)]
    assert json.loads(capsys.readouterr().out) == {"published": False, "digest": "digest"}


def test_release_stats_adapter_forwards_apply_and_canonical_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = SimpleNamespace(to_payload=lambda: {"published": True, "digest": "release-digest"})
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        publish,
        "release_card_and_stats",
        lambda path, *, confirm_repo, repo_id, apply: (
            calls.append((path, confirm_repo, repo_id, apply)) or report
        ),
    )

    assert (
        publish.release_stats_command(
            tmp_path / "run",
            confirm_repo="NoeFlandre/osm-polygon-website-tag",
            repo_id="NoeFlandre/osm-polygon-website-tag",
            apply=True,
        )
        == 0
    )

    assert calls == [
        (
            tmp_path / "run",
            "NoeFlandre/osm-polygon-website-tag",
            "NoeFlandre/osm-polygon-website-tag",
            True,
        )
    ]
    assert json.loads(capsys.readouterr().out) == {
        "published": True,
        "digest": "release-digest",
    }

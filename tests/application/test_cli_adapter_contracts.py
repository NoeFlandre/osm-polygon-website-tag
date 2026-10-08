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
from osm_polygon_website_tag.runtime.run_state import RunState, load_run
from osm_polygon_website_tag.web.politeness import HostPolicy


@pytest.mark.parametrize("inventory_matches", [True, False], ids=["complete", "partial"])
def test_extract_adapter_forwards_worker_limits_and_persists_the_resulting_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inventory_matches: bool
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "source.osm.pbf"
    source.write_bytes(b"pbf!")
    output_root = tmp_path / "runs"
    assert run.init_command(output_root, source_root, [source], run_id="run-1") == 0
    run_dir = output_root / "run-1"
    extractions: list[tuple[Path, Path, RunState, int, int]] = []

    def extract(
        pbf_path: Path,
        extract_dir: Path,
        *,
        run_state: RunState,
        area_workers: int,
        max_in_flight_areas: int,
    ) -> None:
        extractions.append((pbf_path, extract_dir, run_state, area_workers, max_in_flight_areas))

    monkeypatch.setattr(run, "extract_pbf", extract)
    monkeypatch.setattr(run, "source_inventory_matches", lambda _path: inventory_matches)

    assert run.extract_command(source, run_dir, area_workers=3, max_in_flight_areas=11) == 0

    [(pbf_path, extract_dir, run_state, area_workers, in_flight)] = extractions
    assert (pbf_path, extract_dir, area_workers, in_flight) == (source, run_dir, 3, 11)
    assert run_state.run_dir == run_dir
    expected_status = "extracted" if inventory_matches else "extracting"
    assert load_run(run_dir).metadata["status"] == expected_status


def test_extract_rejects_a_source_missing_from_the_expected_inventory_with_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    state_file = run_dir / "manifests" / "run.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{}", encoding="utf-8")
    source = tmp_path / "missing-from-inventory.osm.pbf"
    state = SimpleNamespace(metadata={"status": "initialized"})
    fingerprint = SimpleNamespace(filename=source.name, size_bytes=4, mtime_ns=17)
    monkeypatch.setattr(run, "load_run", lambda _path: state)
    monkeypatch.setattr(run, "snapshot_source_fingerprint", lambda _path: fingerprint)
    monkeypatch.setattr(run, "expected_source_inventory", lambda _path: [])
    monkeypatch.setattr(
        run,
        "extract_pbf",
        lambda *_args, **_kwargs: pytest.fail("an unlisted source must not be extracted"),
    )

    with pytest.raises(
        ValueError,
        match=r"^source is not in exact expected inventory: missing-from-inventory\.osm\.pbf$",
    ):
        run.extract_command(source, run_dir)


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


def test_init_records_normalized_source_root_and_expected_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "places.osm.pbf"
    source.write_bytes(b"pbf fixture")
    output_root = tmp_path / "runs"

    assert run.init_command(output_root, source_root, [source], run_id="run-1") == 0

    run_dir = output_root / "run-1"
    state = run.load_run(run_dir)
    stat = source.stat()
    assert state.metadata["source_root"] == str(source_root.resolve())
    assert run.expected_source_inventory(run_dir) == [
        {"filename": source.name, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    ]
    assert capsys.readouterr().out == f"{run_dir}\n"


def test_run_all_adapter_forwards_every_option_and_serializes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_root = tmp_path / "source"
    output_root = tmp_path / "runs"
    calls: list[dict[str, object]] = []

    class Reporter:
        def __init__(self, *, quiet: bool) -> None:
            self.quiet = quiet
            self.completed: bool | None = None
            created.append(self)

        def close(self, *, completed: bool) -> None:
            self.completed = completed

    created: list[Reporter] = []
    monkeypatch.setattr(run, "ProgressReporter", Reporter)
    monkeypatch.setattr(run, "configured_hf_dataset_repo", lambda repo_id: repo_id)

    def backend(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        # Deliberately put keys out of alphabetical order so this asserts the
        # command's stable JSON ordering rather than insertion order.
        return SimpleNamespace(sources=2, complete=True, run_dir=output_root / "run-1")

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

    [reporter] = created
    assert reporter.quiet is False
    assert reporter.completed is True
    assert calls == [
        {
            "source_root": source_root,
            "output_root": output_root,
            "run_id": "run-1",
            "repo_id": "owner/dataset",
            "apply": True,
            "ensure_repo": True,
            "progress": reporter,
            "area_workers": 2,
            "max_in_flight_areas": 7,
            "fetch_workers": 9,
            "host_policy": HostPolicy(concurrency=3, delay_seconds=0.25),
            "detect_languages": True,
        }
    ]
    expected = {
        "complete": True,
        "run_dir": str(output_root / "run-1"),
        "sources": 2,
    }
    output = capsys.readouterr().out
    assert output == json.dumps(expected, indent=2, sort_keys=True) + "\n"
    assert json.loads(output) == expected


def test_run_all_closes_progress_as_incomplete_when_backend_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class Reporter:
        def __init__(self, *, quiet: bool) -> None:
            assert quiet is False

        def close(self, *, completed: bool) -> None:
            closed.append(completed)

    def fail(**kwargs: object) -> None:
        assert kwargs["apply"] is False
        assert kwargs["detect_languages"] is False
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

    with pytest.raises(ValueError, match=r"^--ensure-repo requires --apply$"):
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

    def require_run_root(path: Path, *, label: str) -> Path:
        assert label == "run directory"
        return path

    monkeypatch.setattr(languages, "require_under_data_root", require_run_root)
    monkeypatch.setattr(languages, "load_run", lambda path: loaded.append(path) or state)
    monkeypatch.setattr(languages, "shard_needs_language_detection", lambda _path: False)
    monkeypatch.setattr(
        languages,
        "load_glotlid_detector",
        lambda _path: pytest.fail("completed shards must not load the model"),
    )

    assert languages.detect_languages_command(run_dir, time_budget_seconds=10.0) == 0

    assert loaded == [run_dir]
    assert capsys.readouterr().out == (
        json.dumps(
            {
                "changed_shards": 0,
                "completed": True,
                "processed_rows": 0,
                "run_dir": str(run_dir),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


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


def test_language_command_reports_actual_bounded_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    shard = run_dir / "polygons" / "site.parquet"
    shard.parent.mkdir(parents=True)
    shard.touch()
    state = SimpleNamespace(metadata={"status": "enriched"}, sources={"site.osm.pbf": {}})
    detector = object()
    monkeypatch.setattr(languages, "require_under_data_root", lambda path, **_kwargs: path)
    monkeypatch.setattr(languages, "load_run", lambda _path: state)
    monkeypatch.setattr(languages, "shard_needs_language_detection", lambda _path: True)
    monkeypatch.setattr(languages, "load_glotlid_detector", lambda _path: detector)
    monkeypatch.setattr(
        languages,
        "_run_language_shards",
        lambda paths, **kwargs: (
            pytest.fail("the detector and shard selection must be forwarded")
            if kwargs["detector"] is not detector or paths != [shard]
            else languages._LanguageRunProgress(2, 7, completed=False)
        ),
    )

    assert languages.detect_languages_command(run_dir, time_budget_seconds=1.0) == 0
    expected = {
        "changed_shards": 2,
        "completed": False,
        "processed_rows": 7,
        "run_dir": str(run_dir),
    }
    assert capsys.readouterr().out == json.dumps(expected, indent=2, sort_keys=True) + "\n"


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

    assert set(validated) == {
        (run_dir, "run directory"),
        (model_dir, "SaT model directory"),
    }
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
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "changed_shards": 1,
                "completed": False,
                "processed_rows": 5,
                "run_dir": str(run_dir),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_sentence_command_selects_sorted_unfinished_shards_and_records_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    polygon_dir = run_dir / "polygons"
    polygon_dir.mkdir(parents=True)
    shards = [polygon_dir / f"{name}.parquet" for name in ("zeta", "alpha", "beta")]
    for shard in shards:
        shard.touch()
    state = SimpleNamespace(
        metadata={"status": "enriched"},
        sources={f"{shard.stem}.osm.pbf": {} for shard in shards},
    )
    model_dir = tmp_path / "data" / "models" / "sat"
    splitter = object()
    calls: list[tuple[list[Path], dict[str, object]]] = []
    records: list[tuple[object, Path, object]] = []

    monkeypatch.setattr(sentences, "require_under_data_root", lambda path, **_kwargs: path)
    monkeypatch.setattr(sentences, "load_run", lambda _path: state)
    monkeypatch.setattr(
        sentences,
        "shard_needs_sentence_segmentation",
        lambda shard: shard.name != "beta.parquet",
    )
    monkeypatch.setattr(
        sentences,
        "load_sat_splitter_from_path",
        lambda path, *, revision: splitter,
    )
    monkeypatch.setattr(
        sentences,
        "_record_completed_language_shard",
        lambda current, shard, result: records.append((current, shard, result)),
    )

    def run_shards(paths: list[Path], **kwargs: object) -> SimpleNamespace:
        calls.append((paths, kwargs))
        return SimpleNamespace(changed_shards=2, completed=True, processed_rows=7)

    monkeypatch.setattr(sentences, "run_sentence_shards", run_shards)

    assert (
        sentences.segment_sentences_command(
            run_dir,
            model_dir=model_dir,
            model_revision="revision-8",
            time_budget_seconds=12.0,
        )
        == 0
    )

    assert len(calls) == 1
    paths, kwargs = calls[0]
    assert paths == [polygon_dir / "alpha.parquet", polygon_dir / "zeta.parquet"]
    assert kwargs["splitter"] is splitter
    assert kwargs["batch_rows"] == sentences.DEFAULT_SENTENCE_BATCH_ROWS
    assert kwargs["time_budget_seconds"] == 12.0
    assert kwargs.keys() == {"splitter", "record", "batch_rows", "time_budget_seconds"}
    callback = cast(Callable[[Path, object], object], kwargs["record"])
    result = object()
    callback(paths[0], result)
    assert records == [(state, paths[0], result)]
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "changed_shards": 2,
                "completed": True,
                "processed_rows": 7,
                "run_dir": str(run_dir),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_sentence_command_rejects_shards_outside_the_source_manifest_before_model_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    shard = run_dir / "polygons" / "outside.parquet"
    shard.parent.mkdir(parents=True)
    shard.touch()
    state = SimpleNamespace(metadata={"status": "enriched"}, sources={})
    monkeypatch.setattr(sentences, "require_under_data_root", lambda path, **_kwargs: path)
    monkeypatch.setattr(sentences, "load_run", lambda _path: state)
    monkeypatch.setattr(
        sentences,
        "load_sat_splitter_from_path",
        lambda *_args, **_kwargs: pytest.fail("unlisted shards must be rejected first"),
    )

    with pytest.raises(ValueError, match="language shard is not in the source manifest"):
        sentences.segment_sentences_command(
            run_dir,
            model_dir=tmp_path / "data" / "models" / "sat",
            model_revision="revision-1",
        )


def test_sentence_command_validates_options_before_reading_the_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="batch_rows must be positive"):
        sentences.segment_sentences_command(
            tmp_path / "missing-run",
            model_dir=tmp_path / "missing-model",
            model_revision="revision-1",
            batch_rows=0,
        )


def test_sentence_command_skips_model_loading_when_all_shards_are_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "data" / "runs" / "run-1"
    shard = run_dir / "polygons" / "site.parquet"
    shard.parent.mkdir(parents=True)
    shard.touch()
    state = SimpleNamespace(metadata={"status": "enriched"}, sources={"site.osm.pbf": {}})
    monkeypatch.setattr(sentences, "require_under_data_root", lambda path, **_kwargs: path)
    monkeypatch.setattr(sentences, "load_run", lambda _path: state)
    monkeypatch.setattr(sentences, "shard_needs_sentence_segmentation", lambda _path: False)
    monkeypatch.setattr(
        sentences,
        "load_sat_splitter_from_path",
        lambda *_args, **_kwargs: pytest.fail("completed shards must not load SaT"),
    )

    assert (
        sentences.segment_sentences_command(
            run_dir,
            model_dir=tmp_path / "sat-model",
            model_revision="revision-1",
            time_budget_seconds=10.0,
        )
        == 0
    )
    expected = {
        "changed_shards": 0,
        "completed": True,
        "processed_rows": 0,
        "run_dir": str(run_dir),
    }
    assert capsys.readouterr().out == json.dumps(expected, indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize(
    "time_budget_seconds",
    [0.0, -1.0, float("nan"), float("inf")],
    ids=["zero", "negative", "nan", "infinity"],
)
def test_sentence_command_validates_time_budget_before_reading_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    time_budget_seconds: float,
) -> None:
    monkeypatch.setattr(
        sentences,
        "require_under_data_root",
        lambda *_args, **_kwargs: pytest.fail("invalid time budget must be rejected first"),
    )

    with pytest.raises(ValueError, match="time_budget_seconds must be positive"):
        sentences.segment_sentences_command(
            tmp_path / "missing-run",
            model_dir=tmp_path / "missing-model",
            model_revision="revision-1",
            time_budget_seconds=time_budget_seconds,
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
        "configured_hf_dataset_repo",
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
    monkeypatch.setattr(publish, "configured_hf_dataset_repo", lambda repo_id: repo_id)
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


@pytest.mark.parametrize("has_readme", [True, False])
def test_publish_plan_reports_resolved_artifact_count_and_readme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    has_readme: bool,
) -> None:
    run_dir = tmp_path / "run"
    readme = run_dir / "README.md"
    readme_path = readme if has_readme else None
    calls: list[tuple[Path, str | None]] = []

    def resolve_repo(repo_id: str | None) -> str:
        return "owner/configured" if repo_id is None else f"resolved/{repo_id}"

    monkeypatch.setattr(publish, "configured_hf_dataset_repo", resolve_repo)
    monkeypatch.setattr(
        publish,
        "build_publish_plan",
        lambda path, *, repo_id: (
            calls.append((path, repo_id))
            or SimpleNamespace(artifact_paths=(Path("one"), Path("two")), readme_path=readme_path)
        ),
    )

    assert publish.publish_plan_command(run_dir, repo_id="owner/requested") == 0

    assert calls == [(run_dir, "resolved/owner/requested")]
    expected = {
        "repo_id": "owner/requested",
        "artifact_count": 2,
        "readme": str(readme) if has_readme else None,
    }
    output = capsys.readouterr().out
    assert output == json.dumps(expected, indent=2) + "\n"
    assert json.loads(output) == expected


def test_create_repo_adapter_keeps_preview_read_only_and_forwards_defaults(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checked: list[str] = []
    created: list[tuple[str, bool]] = []

    def repo_exists(*, repo_id: str) -> bool:
        checked.append(repo_id)
        return True

    def create_repo(*, repo_id: str, exist_ok: bool) -> str:
        created.append((repo_id, exist_ok))
        return "owner/dataset"

    monkeypatch.setattr(publish, "repo_exists", repo_exists)
    monkeypatch.setattr(publish, "create_repo", create_repo)

    assert publish.create_repo_command(repo_id="owner/dataset") == 0

    preview = {"applied": False, "exists": True, "repo_id": "owner/dataset"}
    output = capsys.readouterr().out
    assert output == json.dumps(preview, indent=2) + "\n"
    assert json.loads(output) == preview
    assert checked == ["owner/dataset"]
    assert created == []

    assert publish.create_repo_command(repo_id="owner/dataset", apply=True) == 0

    assert capsys.readouterr().out == "owner/dataset\n"
    assert created == [("owner/dataset", False)]


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
    monkeypatch.setattr(publish, "configured_hf_dataset_repo", lambda repo_id: "configured/dataset")
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

    assert publish.publish_trackio_command(run_dir) == 0

    assert calls == [(run_dir, "configured/dataset")]
    expected = {
        "dry_run": True,
        "manifest_digest": "digest",
        "metrics": {"row_count": 4},
        "project": "osm-polygon-website-tag",
        "remote": None,
        "run_name": "dataset-1",
        "space_id": "NoeFlandre/osm-polygon-website-tag-metrics",
    }
    output = capsys.readouterr().out
    assert output == json.dumps(expected, default=str, indent=2, sort_keys=True) + "\n"
    assert json.loads(output) == expected


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
    builds: list[tuple[Path, str]] = []
    publishes: list[tuple[object, str, str]] = []
    monkeypatch.setattr(publish, "configured_hf_dataset_repo", lambda repo_id: repo_id)
    monkeypatch.setattr(
        publish,
        "build_trackio_snapshot",
        lambda path, *, dataset_repo: builds.append((path, dataset_repo)) or snapshot,
    )
    monkeypatch.setattr(
        publish,
        "publish_trackio_snapshot",
        lambda value, *, space_id, project: (
            publishes.append((value, space_id, project)) or {"revision": "published-revision"}
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

    assert builds == [(run_dir, "owner/dataset")]
    assert publishes == [(snapshot, "owner/metrics", "custom-project")]
    expected = {
        "dry_run": False,
        "space_id": "owner/metrics",
        "project": "custom-project",
        "run_name": "dataset-2",
        "manifest_digest": "digest-2",
        "metrics": {"row_count": 8},
        "remote": {"revision": "published-revision"},
    }
    output = capsys.readouterr().out
    assert output == json.dumps(expected, default=str, indent=2, sort_keys=True) + "\n"
    assert json.loads(output) == expected


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
        )
        == 0
    )

    assert calls == [(tmp_path / "run", "owner/dataset", "owner/dataset", False)]
    expected = {"published": False, "digest": "digest"}
    output = capsys.readouterr().out
    assert output == json.dumps(expected, default=str, indent=2, sort_keys=True) + "\n"
    assert json.loads(output) == expected


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
    expected = {"published": True, "digest": "release-digest"}
    output = capsys.readouterr().out
    assert output == json.dumps(expected, default=str, indent=2, sort_keys=True) + "\n"
    assert json.loads(output) == expected

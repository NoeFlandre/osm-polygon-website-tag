"""Behavioural tests for the per-PBF extraction orchestration helpers."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow.parquet as pq
import pytest

import osm_polygon_website_tag.pipeline.extraction as extraction_module
from osm_polygon_website_tag.pipeline.extraction import extract_pbf
from osm_polygon_website_tag.runtime.run_state import initialise_run, load_run

_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6"><node id="1" lat="0.0" lon="0.0"/><node id="2" lat="0.0" lon="1.0"/>
<node id="3" lat="1.0" lon="1.0"/><node id="4" lat="1.0" lon="0.0"/>
<way id="100" version="2" timestamp="2024-01-01T00:00:00Z">
  <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
  <tag k="building" v="yes"/>
  <tag k="website" v="https://example.com"/>
</way>
<way id="101" version="1" timestamp="2024-01-01T00:00:00Z">
  <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
  <tag k="building" v="yes"/>
  <tag k="wikidata" v="Q42"/>
</way>
</osm>
"""


def _clock(monkeypatch: pytest.MonkeyPatch, *moments: dt.datetime) -> None:
    """Replace the module clock so timestamps carry non-zero microseconds."""
    queue = list(moments)

    class FakeDatetime:
        @staticmethod
        def now(tz: dt.tzinfo | None = None) -> dt.datetime:
            return queue.pop(0)

    monkeypatch.setattr(extraction_module, "dt", SimpleNamespace(datetime=FakeDatetime, UTC=dt.UTC))


def _pbf(make_pbf: Any) -> Path:
    directory = make_pbf(_XML, name="testland-latest.osm.pbf")
    return next(p for p in directory.iterdir() if p.name.endswith(".osm.pbf"))


def test_now_iso_truncates_microseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch, dt.datetime(2024, 5, 6, 7, 8, 9, 987654, tzinfo=dt.UTC))

    assert extraction_module._now_iso() == "2024-05-06T07:08:09+00:00"


def test_extract_pbf_reports_full_result_and_run_state(
    make_pbf: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _pbf(make_pbf)
    run_dir, state = initialise_run(tmp_path, run_id="run")
    _clock(
        monkeypatch,
        dt.datetime(2024, 1, 1, 0, 0, 0, 500000, tzinfo=dt.UTC),
        dt.datetime(2024, 1, 1, 0, 0, 3, 250000, tzinfo=dt.UTC),
    )

    result = extract_pbf(src, run_dir, state)

    obs = pq.read_table(run_dir / "analysis_observations" / "testland-latest.parquet")
    rej = pq.read_table(run_dir / "rejections" / "testland-latest.parquet")
    assert result.source_pbf == "testland-latest.osm.pbf"
    assert result.region == "testland"
    assert result.public_row_count == 1
    assert result.observation_row_count == obs.num_rows >= 1
    assert result.rejection_count == rej.num_rows
    assert result.duration_seconds == 2.75
    assert result.started_at == "2024-01-01T00:00:00+00:00"
    assert result.finished_at == "2024-01-01T00:00:03+00:00"
    entry = load_run(run_dir).sources[src.name]
    assert entry["started_at"] == result.started_at
    assert entry["finished_at"] == result.finished_at
    assert entry["rejection_count"] == result.rejection_count


def test_extract_pbf_removes_a_ledger_that_close_already_removed(
    make_pbf: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _pbf(make_pbf)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    original = extraction_module._extract_and_promote

    def promote_and_drop_ledger(handler: Any, *args: Any, **kwargs: Any) -> Any:
        outcome = original(handler, *args, **kwargs)
        handler.ledger.path.unlink(missing_ok=True)
        return outcome

    monkeypatch.setattr(extraction_module, "_extract_and_promote", promote_and_drop_ledger)

    assert extract_pbf(src, run_dir).public_row_count == 1


def test_extract_pbf_passes_region_stem_and_limits_to_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "testland-latest.osm.pbf"
    src.write_bytes(b"pbf")
    captured: dict[str, Any] = {}

    def fake_handler(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(extraction_module, "_ExtractionHandler", fake_handler)

    _, finals = extraction_module._prepare_extraction(
        src,
        tmp_path / "run",
        region="testland",
        stem="testland-latest",
        area_workers=2,
        max_in_flight_areas=3,
    )

    assert captured["stem"] == "testland-latest"
    assert captured["region"] == "testland"
    assert captured["area_workers"] == 2
    assert captured["max_in_flight_areas"] == 3
    assert finals[0] == tmp_path / "run" / "polygons" / "testland-latest.parquet"


def test_validate_pbf_path_messages_name_the_offending_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="got directory") as directory_error:
        extraction_module._validate_pbf_path(tmp_path)
    assert str(tmp_path) in str(directory_error.value)

    missing = tmp_path / "missing.osm.pbf"
    with pytest.raises(FileNotFoundError) as missing_error:
        extraction_module._validate_pbf_path(missing)
    assert missing_error.value.args == (missing,)


def test_extract_and_promote_rejects_unequal_shard_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.osm.pbf"
    src.write_bytes(b"pbf")
    sinks = [SimpleNamespace(path=tmp_path / name, row_count=0) for name in "abc"]
    handler = SimpleNamespace(
        apply_file=lambda _p: None,
        reconcile_candidates=lambda: None,
        close=lambda: None,
        public_sink=sinks[0],
        obs_sink=sinks[1],
        rej_sink=sinks[2],
    )
    monkeypatch.setattr(extraction_module, "snapshot_source_fingerprint", lambda _p: "same")
    captured: list[Any] = []
    monkeypatch.setattr(
        extraction_module, "atomic_promote_bundle", lambda pairs: captured.extend(pairs)
    )
    finals = (tmp_path / "x", tmp_path / "y")  # one short: strict zip must raise

    with pytest.raises(ValueError, match="zip"):
        extraction_module._extract_and_promote(
            handler,  # ty: ignore[invalid-argument-type]
            src,
            source_before="same",  # ty: ignore[invalid-argument-type]
            final_paths=finals,  # ty: ignore[invalid-argument-type]
        )
    assert captured == []


def test_abort_extraction_tolerates_missing_scratch_and_records_the_error(
    tmp_path: Path,
) -> None:
    run_dir, state = initialise_run(tmp_path, run_id="run")
    aborted: list[bool] = []
    handler = SimpleNamespace(abort=lambda: aborted.append(True))
    present = run_dir / "scratch.tmp"
    present.write_text("x", encoding="utf-8")
    pbf = tmp_path / "boom-latest.osm.pbf"

    extraction_module._abort_extraction(
        handler,  # ty: ignore[invalid-argument-type]
        (present, run_dir / "never-created.tmp"),
        pbf_path=pbf,
        run_dir=run_dir,
        run_state=state,
        error=ValueError(f"bad {pbf}"),
    )

    assert aborted == [True]
    assert not present.exists()
    failure = json.loads((run_dir / "failures.jsonl").read_text(encoding="utf-8"))
    assert failure["kind"] == "ValueError"
    assert failure["message"] == "bad boom-latest.osm.pbf"


def test_abort_extraction_without_run_state_leaves_no_failure_record(tmp_path: Path) -> None:
    handler = SimpleNamespace(abort=lambda: None)

    extraction_module._abort_extraction(
        handler,  # ty: ignore[invalid-argument-type]
        (tmp_path / "gone.tmp",),
        pbf_path=tmp_path / "a.osm.pbf",
        run_dir=tmp_path,
        run_state=None,
        error=ValueError("x"),
    )

    assert not (tmp_path / "failures.jsonl").exists()

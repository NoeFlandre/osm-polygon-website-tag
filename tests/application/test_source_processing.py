from pathlib import Path
from types import SimpleNamespace

from osm_polygon_website_tag.application import source_processing
from osm_polygon_website_tag.application.source_processing import SourceProcessingContext
from osm_polygon_website_tag.publishing.incremental import CheckpointV2
from osm_polygon_website_tag.runtime.run_state import RunState, SourceFingerprint


def test_process_sources_returns_counts_in_order(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "first.osm.pbf"
    second = tmp_path / "second.osm.pbf"
    calls: list[tuple[str, int, int, bool]] = []

    def process_source(**kwargs: object) -> SimpleNamespace:
        source = kwargs["source"]
        index = kwargs["index"]
        total = kwargs["total"]
        allow_extraction = kwargs["allow_extraction"]
        assert isinstance(source, Path)
        assert isinstance(index, int)
        assert isinstance(total, int)
        assert isinstance(allow_extraction, bool)
        calls.append((source.name, index, total, allow_extraction))
        return SimpleNamespace(extracted=index == 1, reused=index == 2, uploaded=True)

    context = SourceProcessingContext(
        run_dir=tmp_path,
        state=RunState(run_dir=tmp_path, run_id="test"),
        repo_id="owner/dataset",
        apply=False,
        progress=None,
        invocation_id="test",
        upload_checkpoint=CheckpointV2(schema_version="v2", global_bundle={}, sources={}),
        area_workers=None,
        max_in_flight_areas=None,
        fetch_workers=None,
        detect_languages=False,
        language_detector=None,
    )
    monkeypatch.setattr(source_processing, "_process_source", process_source, raising=False)
    result = source_processing.process_sources(
        sources=[first, second],
        ordered_sources=[second, first],
        fingerprints_by_name={
            "first.osm.pbf": SourceFingerprint("first.osm.pbf", 0, 0),
            "second.osm.pbf": SourceFingerprint("second.osm.pbf", 0, 0),
        },
        context=context,
        allow_extraction=False,
    )

    assert calls == [
        ("second.osm.pbf", 1, 2, False),
        ("first.osm.pbf", 2, 2, False),
    ]
    assert result.extracted == 1
    assert result.reused == 1
    assert result.uploaded == 2

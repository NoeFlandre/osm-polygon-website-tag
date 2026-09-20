from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from osm_polygon_website_tag.application import source_processing
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA_V1_2,
)


def test_migrate_public_shard_updates_manifest_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    shard = tmp_path / "a.parquet"
    context: Any = type("Context", (), {})()
    context.state = object()
    progress: list[str] = []
    context.progress = progress.append
    schema = object()
    read_calls: list[Path] = []
    schema_calls: list[tuple[object, object]] = []
    migration_calls: list[Path] = []
    metadata: list[tuple[object, dict[str, object]]] = []

    def read_schema(path: Path) -> object:
        read_calls.append(path)
        return schema

    def matches(actual: object, expected: object) -> bool:
        schema_calls.append((actual, expected))
        return expected is POLYGON_PUBLIC_SCHEMA_V1_2

    monkeypatch.setattr(source_processing.pq, "read_schema", read_schema)
    monkeypatch.setattr(source_processing, "schema_matches", matches)
    monkeypatch.setattr(
        source_processing,
        "migrate_public_shard",
        lambda path: (
            migration_calls.append(path)
            or SimpleNamespace(changed=True, row_count=4, shard_sha256="a" * 64)
        ),
    )
    monkeypatch.setattr(
        source_processing,
        "update_public_shard_metadata",
        lambda state, **kwargs: metadata.append((state, kwargs)),
    )

    assert source_processing._migrate_public_shard_if_needed(source, shard, context, 2, 3)
    assert read_calls == [shard]
    assert schema_calls == [(schema, POLYGON_PUBLIC_SCHEMA_V1_2)]
    assert migration_calls == [shard]
    assert progress == ["[2/3] Migrating a.osm.pbf to public schema v1.3"]
    assert metadata == [
        (
            context.state,
            {"filename": source.name, "row_count": 4, "shard_sha256": "a" * 64},
        )
    ]


def test_detect_source_shard_handles_opt_in_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    shard = tmp_path / "a.parquet"
    context: Any = type("Context", (), {})()
    context.progress = None
    context.state = object()
    context.language_detector = None
    context.detect_languages = False
    monkeypatch.setattr(
        source_processing,
        "shard_needs_language_detection",
        lambda _path: pytest.fail("disabled detection must not inspect the shard"),
    )
    assert not source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )

    context.detect_languages = True
    inspected_paths: list[Path] = []
    monkeypatch.setattr(
        source_processing,
        "shard_needs_language_detection",
        lambda path: inspected_paths.append(path) or False,
    )
    assert not source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )
    assert inspected_paths == [shard]

    context.language_detector = object()
    detector_calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(
        source_processing,
        "detect_language_shard",
        lambda path, *, detector: (
            detector_calls.append((path, detector))
            or SimpleNamespace(changed=False, row_count=0, shard_sha256="a" * 64)
        ),
    )
    assert not source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )
    assert detector_calls == [(shard, context.language_detector)]

    context.language_detector = None
    monkeypatch.setattr(source_processing, "shard_needs_language_detection", lambda _path: True)
    with pytest.raises(ValueError) as error:
        source_processing._detect_source_shard_if_needed(
            source=source, shard=shard, context=context, index=1, total=1
        )
    assert str(error.value) == "language detection requested without a detector"


def test_detect_source_shard_persists_changed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    shard = tmp_path / "a.parquet"
    context: Any = type("Context", (), {})()
    progress: list[str] = []
    context.progress = progress.append
    context.state = object()
    context.detect_languages = True
    context.language_detector = object()
    metadata: list[tuple[object, dict[str, object]]] = []
    detector_calls: list[tuple[Path, object]] = []

    monkeypatch.setattr(
        source_processing,
        "detect_language_shard",
        lambda path, *, detector: (
            detector_calls.append((path, detector))
            or SimpleNamespace(changed=True, row_count=4, shard_sha256="b" * 64)
        ),
    )
    monkeypatch.setattr(
        source_processing,
        "update_public_shard_metadata",
        lambda state_value, **kwargs: metadata.append((state_value, kwargs)),
    )

    assert source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )
    assert detector_calls == [(shard, context.language_detector)]
    assert metadata == [
        (
            context.state,
            {"filename": source.name, "row_count": 4, "shard_sha256": "b" * 64},
        )
    ]
    assert progress == ["[1/1] Detecting languages for a.osm.pbf"]


def test_detect_source_shard_delegates_readiness_to_detector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    shard = tmp_path / "a.parquet"
    context: Any = type("Context", (), {})()
    context.progress = None
    context.state = object()
    context.detect_languages = True
    context.language_detector = object()
    monkeypatch.setattr(
        source_processing,
        "shard_needs_language_detection",
        lambda _path: pytest.fail("source processing must delegate readiness to detection"),
    )
    monkeypatch.setattr(
        source_processing,
        "detect_language_shard",
        lambda path, *, detector: SimpleNamespace(
            shard_path=path,
            row_count=4,
            shard_sha256="b" * 64,
            changed=True,
        ),
    )
    monkeypatch.setattr(
        source_processing, "update_public_shard_metadata", lambda *_args, **_kwargs: None
    )

    assert source_processing._detect_source_shard_if_needed(
        source=source, shard=shard, context=context, index=1, total=1
    )


@pytest.mark.parametrize(
    ("marker", "status_summary", "migration_changed", "expected"),
    [
        (None, None, False, True),
        (True, {"success": {"count": 1}}, False, False),
        (False, {"success": {"count": 1}}, False, False),
        (False, {"success": {"count": 1}}, True, True),
        (False, None, False, True),
        (True, None, False, False),
    ],
)
def test_should_recheck_enrichment_is_explicit(
    marker: object,
    status_summary: dict[str, dict[str, int]] | None,
    migration_changed: bool,
    expected: bool,
) -> None:
    assert (
        source_processing._should_recheck_enrichment(
            marker=marker,
            status_summary=status_summary,
            migration_changed=migration_changed,
        )
        is expected
    )


def test_initial_enrichment_decision_preserves_the_cached_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = {"success": {"count": 2}}
    monkeypatch.setattr(
        source_processing,
        "_shard_needs_enrichment",
        lambda _path: pytest.fail("cached marker should avoid a shard scan"),
    )

    decision = source_processing._initial_enrichment_decision(
        tmp_path / "a.parquet",
        marker=False,
        status_summary=summary,
        migration_changed=False,
    )

    assert decision == source_processing._EnrichmentDecision(False, summary)


def test_initial_enrichment_decision_rechecks_after_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shard = tmp_path / "a.parquet"
    monkeypatch.setattr(
        source_processing,
        "_shard_needs_enrichment",
        lambda path: (path == shard) or pytest.fail(f"unexpected shard: {path}"),
    )

    decision = source_processing._initial_enrichment_decision(
        shard,
        marker=False,
        status_summary={"success": {"count": 2}},
        migration_changed=True,
    )

    assert decision == source_processing._EnrichmentDecision(True, {"success": {"count": 2}})


def test_publish_source_forwards_all_change_reasons_and_resume_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.repo_id = "owner/dataset"
    context.apply = False
    callback_messages: list[str] = []
    context.progress = callback_messages.append
    context.state = type("State", (), {"sources": {source.name: {}}})()
    context.upload_checkpoint = {"sources": {}}
    current_calls: list[dict[str, object]] = []
    publication_calls: list[dict[str, object]] = []
    publish_calls: list[dict[str, object]] = []
    record_calls: list[tuple[Path, Any, bool]] = []
    monkeypatch.setattr(
        source_processing,
        "_source_upload_is_current_for_context",
        lambda **kwargs: current_calls.append(kwargs) or False,
    )
    monkeypatch.setattr(
        source_processing,
        "_source_requires_publication",
        lambda **kwargs: publication_calls.append(kwargs) or True,
    )
    monkeypatch.setattr(
        source_processing,
        "_maybe_publish_enriched_shard",
        lambda **kwargs: publish_calls.append(kwargs) or False,
    )
    monkeypatch.setattr(
        source_processing,
        "_record_source_upload",
        lambda source_value, context_value, uploaded: record_calls.append(
            (source_value, context_value, uploaded)
        ),
    )

    result = source_processing._publish_source_if_needed(
        source=source,
        context=context,
        index=2,
        total=3,
        reused=False,
        migration_changed=True,
        needs_enrichment=False,
        language_changed=True,
    )

    assert result is False
    assert current_calls == [
        {
            "source": source,
            "context": context,
            "index": 2,
            "total": 3,
            "migration_changed": True,
            "needs_enrichment": False,
            "language_changed": True,
        }
    ]
    assert publication_calls == [
        {
            "context": context,
            "migration_changed": True,
            "needs_enrichment": False,
            "language_changed": True,
        }
    ]
    assert publish_calls == [
        {
            "run_dir": tmp_path,
            "source": source,
            "repo_id": "owner/dataset",
            "apply": False,
            "progress": context.progress,
            "index": 2,
            "total": 3,
            "allow_bundle_only": True,
            "published_source_names": None,
        }
    ]
    assert record_calls == [(source, context, False)]


def test_publish_source_defaults_language_changed_to_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.run_dir = tmp_path
    context.repo_id = "owner/dataset"
    context.apply = False
    context.progress = None
    current_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_source_upload_is_current_for_context",
        lambda **kwargs: current_calls.append(kwargs) or False,
    )
    monkeypatch.setattr(source_processing, "_source_requires_publication", lambda **_kwargs: False)

    assert not source_processing._publish_source_if_needed(
        source=source,
        context=context,
        index=1,
        total=1,
        reused=False,
        migration_changed=False,
        needs_enrichment=False,
    )
    assert current_calls[0]["language_changed"] is False


def test_source_upload_current_for_context_rejects_changed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    manifest = {"public_shard_sha256": "a" * 64}
    context: Any = type("Context", (), {})()
    context.apply = True
    context.state = type("State", (), {"sources": {source.name: manifest}})()
    context.upload_checkpoint = {"sources": {source.name: {"polygon_sha256": "a" * 64}}}
    context.progress = None
    context.run_dir = tmp_path

    assert source_processing._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=1,
        migration_changed=False,
        needs_enrichment=False,
        language_changed=False,
    )
    for changed in ("migration_changed", "needs_enrichment", "language_changed"):
        flags = {
            "migration_changed": False,
            "needs_enrichment": False,
            "language_changed": False,
        }
        flags[changed] = True
        assert not source_processing._source_upload_is_current_for_context(
            source=source,
            context=context,
            index=1,
            total=1,
            **flags,
        )

    context.apply = False
    assert not source_processing._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=1,
        migration_changed=False,
        needs_enrichment=False,
        language_changed=False,
    )

    context.apply = True
    context.upload_checkpoint = {"sources": {source.name: {"polygon_sha256": "b" * 64}}}
    assert not source_processing._source_upload_is_current_for_context(
        source=source,
        context=context,
        index=1,
        total=1,
        migration_changed=False,
        needs_enrichment=False,
        language_changed=False,
    )


@pytest.mark.parametrize(
    ("apply", "migration_changed", "needs_enrichment", "language_changed", "expected"),
    [
        (False, False, False, False, False),
        (True, False, False, False, True),
        (False, True, False, False, True),
        (False, False, True, False, True),
        (False, False, False, True, True),
    ],
)
def test_source_requires_publication_accounts_for_every_change(
    apply: bool,
    migration_changed: bool,
    needs_enrichment: bool,
    language_changed: bool,
    expected: bool,
) -> None:
    context: Any = type("Context", (), {"apply": apply})()
    assert (
        source_processing._source_requires_publication(
            context=context,
            migration_changed=migration_changed,
            needs_enrichment=needs_enrichment,
            language_changed=language_changed,
        )
        is expected
    )


def test_publication_helpers_keep_previous_sources_and_ignore_false_uploads(
    tmp_path: Path,
) -> None:
    source = Path("a.osm.pbf")
    context: Any = type("Context", (), {})()
    context.apply = True
    context.upload_checkpoint = {"sources": {"previous.osm.pbf": {}}}
    assert source_processing._published_source_names(context, source) == {
        "previous.osm.pbf",
        source.name,
    }

    checkpoint: Any = {"sources": {}}
    state: Any = type("State", (), {"sources": {source.name: {}}})()
    context.state = state
    context.upload_checkpoint = checkpoint
    source_processing._record_source_upload(source, context, uploaded=False)
    assert checkpoint["sources"] == {}

    state.sources[source.name] = {"public_shard_sha256": "a" * 64}
    source_processing._record_source_upload(source, context, uploaded=True)
    assert checkpoint["sources"] == {
        source.name: {"polygon_sha256": "a" * 64},
    }

    context.progress = None
    source_processing._progress(context.progress, "ignored")
    messages: list[str] = []
    source_processing._progress(messages.append, "kept")
    assert messages == ["kept"]
    assert source_processing._public_shard_path(tmp_path, source) == (
        tmp_path / "polygons" / "a.parquet"
    )

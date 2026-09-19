from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from osm_polygon_website_tag.application import source_processing
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.publishing.incremental import IncrementalPublishPlan


@pytest.mark.parametrize(
    ("manifest", "filename", "checkpoint", "expected"),
    [
        ({"public_shard_sha256": "a" * 64}, "a.osm.pbf", {"sources": {}}, False),
        ({"public_shard_sha256": "a" * 64}, "a.osm.pbf", {"sources": []}, False),
        ({"public_shard_sha256": "a" * 64}, "a.osm.pbf", {"sources": {"a.osm.pbf": []}}, False),
        (
            {"public_shard_sha256": "a" * 64},
            "a.osm.pbf",
            {"sources": {"a.osm.pbf": {"polygon_sha256": "b" * 64}}},
            False,
        ),
        (
            {"public_shard_sha256": "a" * 64},
            "a.osm.pbf",
            {"sources": {"a.osm.pbf": {"polygon_sha256": "a" * 64}}},
            True,
        ),
    ],
)
def test_source_upload_checkpoint_match_is_strict(
    manifest: Mapping[str, object],
    filename: str,
    checkpoint: Any,
    expected: bool,
) -> None:
    assert source_processing._source_upload_is_current(manifest, filename, checkpoint) is expected


@pytest.mark.parametrize(
    ("shard_changed", "upload_paths", "allow_bundle_only", "expected"),
    [
        (False, [Path("a.parquet")], False, False),
        (False, [Path("a.parquet")], True, True),
        (True, [], True, False),
        (True, [Path("a.parquet")], True, True),
    ],
)
def test_maybe_publish_enriched_shard_respects_upload_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shard_changed: bool,
    upload_paths: list[Path],
    allow_bundle_only: bool,
    expected: bool,
) -> None:
    source = Path("a.osm.pbf")
    plan = SimpleNamespace(
        shard_changed=shard_changed,
        upload_paths=upload_paths,
        shard_sha256="a" * 64,
        bundle_state={},
    )
    published_source_names = {"previous.osm.pbf", source.name}
    card_calls: list[tuple[Path, object]] = []
    preview_calls: list[tuple[Path, Path, bool]] = []
    upload_calls: list[tuple[Path, Path, str, object]] = []
    persist_calls: list[tuple[Path, Path, str, object]] = []
    progress_messages: list[str] = []

    def fake_build_card(run_dir: Path, *, source_names: object = None) -> None:
        card_calls.append((run_dir, source_names))

    def fake_preview(run_dir_value: Path, source_value: Path, *, dry_run: bool) -> Any:
        preview_calls.append((run_dir_value, source_value, dry_run))
        return plan

    def fake_upload(
        run_dir_value: Path,
        source_value: Path,
        repo_id_value: str,
        plan_value: object,
    ) -> None:
        upload_calls.append((run_dir_value, source_value, repo_id_value, plan_value))

    def fake_persist(
        run_dir_value: Path,
        source_value: Path,
        *,
        shard_sha256: str,
        bundle_state: object,
    ) -> None:
        persist_calls.append((run_dir_value, source_value, shard_sha256, bundle_state))

    monkeypatch.setattr(
        source_processing,
        "build_card",
        fake_build_card,
    )
    monkeypatch.setattr(
        source_processing,
        "incremental_publish_changed_shard",
        fake_preview,
    )
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        fake_upload,
    )
    monkeypatch.setattr(
        source_processing,
        "persist_successful_upload",
        fake_persist,
    )

    result = source_processing._maybe_publish_enriched_shard(
        run_dir=tmp_path,
        source=source,
        repo_id="owner/dataset",
        apply=True,
        progress=progress_messages.append,
        index=1,
        total=1,
        allow_bundle_only=allow_bundle_only,
        published_source_names=published_source_names,
    )

    assert result is expected
    assert card_calls == [(tmp_path, published_source_names)]
    assert preview_calls == [(tmp_path, source, True)]
    if expected:
        assert upload_calls == [(tmp_path, source, "owner/dataset", plan)]
        assert persist_calls == [(tmp_path, source, "a" * 64, {})]
        assert progress_messages == ["[1/1] Uploading enriched shard and recomputed card"]
    else:
        assert upload_calls == []
        assert persist_calls == []
        assert progress_messages == []


def test_maybe_publish_enriched_shard_defaults_to_bundle_only_uploads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = SimpleNamespace(
        shard_changed=False,
        upload_paths=[tmp_path / "README.md"],
        shard_sha256="a" * 64,
        bundle_state={},
    )
    uploads: list[object] = []
    monkeypatch.setattr(source_processing, "build_card", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        source_processing,
        "incremental_publish_changed_shard",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        source_processing,
        "_upload_public_shard",
        lambda *args, **_kwargs: uploads.append(args),
    )
    monkeypatch.setattr(
        source_processing,
        "persist_successful_upload",
        lambda *_args, **_kwargs: None,
    )

    assert source_processing._maybe_publish_enriched_shard(
        run_dir=tmp_path,
        source=Path("a.osm.pbf"),
        repo_id="owner/dataset",
        apply=True,
        progress=None,
        index=1,
        total=1,
    )
    assert uploads


def test_upload_public_shard_uses_supplied_incremental_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    plan = IncrementalPublishPlan(
        source_filename=source.name,
        upload_paths=[tmp_path / "a.parquet"],
        shard_changed=True,
        bundle_changed=False,
        shard_sha256="a" * 64,
        bundle_state={},
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_upload_folder",
        lambda run_dir, **kwargs: calls.append({"run_dir": run_dir, **kwargs}),
    )

    source_processing._upload_public_shard(tmp_path, source, "owner/dataset", plan)

    assert calls == [
        {
            "run_dir": tmp_path,
            "repo_id": "owner/dataset",
            "repo_kind": "dataset",
            "artifact_paths": plan.upload_paths,
        }
    ]
    with pytest.raises(ValueError) as error:
        source_processing._upload_public_shard(tmp_path, Path("b.osm.pbf"), "owner/dataset", plan)
    assert str(error.value) == "incremental publish plan does not match source"


def test_upload_public_shard_uploads_the_complete_bundle_without_a_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "_upload_folder",
        lambda run_dir, **kwargs: calls.append({"run_dir": run_dir, **kwargs}),
    )

    source_processing._upload_public_shard(tmp_path, source, "owner/dataset")

    assert calls == [
        {
            "run_dir": tmp_path,
            "repo_id": "owner/dataset",
            "repo_kind": "dataset",
            "artifact_paths": [
                tmp_path / "polygons" / "a.parquet",
                tmp_path / "README.md",
                tmp_path / "dataset.yaml",
            ],
        }
    ]


def test_upload_public_shard_delegates_to_incremental_uploader_when_map_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("a.osm.pbf")
    map_path = tmp_path / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir(parents=True)
    map_path.write_bytes(b"map")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        source_processing,
        "incremental_publish_changed_shard",
        lambda *args, **kwargs: calls.append({"args": args, **kwargs}) or object(),
    )

    source_processing._upload_public_shard(tmp_path, source, "owner/dataset")

    assert calls == [
        {
            "args": (tmp_path, source),
            "repo_id": "owner/dataset",
            "repo_kind": "dataset",
            "dry_run": False,
            "uploader": source_processing._upload_folder,
        }
    ]


def test_schema_enrichment_checks_legacy_and_current_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parquet: Any = type("Parquet", (), {"schema_arrow": object()})()
    calls: list[tuple[object, object]] = []

    def matches(schema: object, expected: object) -> bool:
        calls.append((schema, expected))
        return expected is POLYGON_PUBLIC_SCHEMA_V1_1

    monkeypatch.setattr(source_processing, "schema_matches", matches)
    assert source_processing._schema_needs_enrichment(parquet)
    assert calls == [(parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_1)]

    calls.clear()

    def current_matches(schema: object, expected: object) -> bool:
        calls.append((schema, expected))
        return expected is POLYGON_PUBLIC_SCHEMA

    monkeypatch.setattr(source_processing, "schema_matches", current_matches)
    assert not source_processing._schema_needs_enrichment(parquet)
    assert calls == [
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_1),
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA),
    ]

    calls.clear()

    def unknown_matches(schema: object, expected: object) -> bool:
        calls.append((schema, expected))
        return False

    monkeypatch.setattr(source_processing, "schema_matches", unknown_matches)
    assert source_processing._schema_needs_enrichment(parquet)
    assert calls == [
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_1),
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA),
        (parquet.schema_arrow, POLYGON_PUBLIC_SCHEMA_V1_4),
    ]


def test_status_columns_stop_at_the_first_retryable_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Batch:
        def column(self, name: str) -> object:
            return name

    class Parquet:
        def iter_batches(self, **kwargs: object):  # type: ignore[no-untyped-def]
            assert kwargs == {
                "columns": ["website_text_status", "contact_website_text_status"],
                "batch_size": 8_192,
            }
            yield Batch()

    values = {"website_text_status": False, "contact_website_text_status": True}
    calls: list[object] = []
    monkeypatch.setattr(
        source_processing,
        "status_has_retryable_value",
        lambda value: calls.append(value) or values[value],
    )

    assert source_processing._status_columns_need_enrichment(cast(Any, Parquet()))
    assert calls == ["website_text_status", "contact_website_text_status"]


def test_status_columns_report_complete_when_no_column_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Batch:
        def column(self, name: str) -> str:
            return name

    class Parquet:
        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield Batch()

    monkeypatch.setattr(source_processing, "status_has_retryable_value", lambda _value: False)

    assert not source_processing._status_columns_need_enrichment(cast(Any, Parquet()))


def test_shard_needs_enrichment_scans_schema_then_status_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Parquet:
        schema_arrow = object()

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            raise AssertionError("schema requiring enrichment must stop before status scan")

    opened: list[Path] = []

    def open_parquet(path: Path) -> Parquet:
        opened.append(path)
        return Parquet()

    monkeypatch.setattr(source_processing.pq, "ParquetFile", open_parquet)
    monkeypatch.setattr(source_processing, "_schema_needs_enrichment", lambda _parquet: True)
    monkeypatch.setattr(
        source_processing,
        "_status_columns_need_enrichment",
        lambda _parquet: pytest.fail("status scan should be skipped"),
    )
    assert source_processing._shard_needs_enrichment(tmp_path / "a.parquet")
    assert opened == [tmp_path / "a.parquet"]


def test_run_needs_enrichment_scans_sorted_public_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    first = polygons / "a.parquet"
    second = polygons / "b.parquet"
    first.touch()
    second.touch()
    (polygons / "ignored.PARQUET").touch()
    seen: list[Path] = []

    def needs_enrichment(shard: Path) -> bool:
        seen.append(shard)
        return shard == second

    monkeypatch.setattr(source_processing, "_shard_needs_enrichment", needs_enrichment)

    assert source_processing._run_needs_enrichment(tmp_path)
    assert seen == [first, second]


def test_run_needs_language_detection_scans_sorted_public_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    first = polygons / "a.parquet"
    second = polygons / "b.parquet"
    first.touch()
    second.touch()
    (polygons / "ignored.PARQUET").touch()
    seen: list[Path] = []

    def needs_detection(shard: Path) -> bool:
        seen.append(shard)
        return shard == second

    monkeypatch.setattr(source_processing, "shard_needs_language_detection", needs_detection)

    assert source_processing._run_needs_language_detection(tmp_path)
    assert seen == [first, second]


def test_run_needs_language_detection_returns_false_when_every_shard_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polygons = tmp_path / "polygons"
    polygons.mkdir()
    shard = polygons / "a.parquet"
    shard.touch()
    seen: list[Path] = []

    def needs_detection(path: Path) -> bool:
        seen.append(path)
        return False

    monkeypatch.setattr(source_processing, "shard_needs_language_detection", needs_detection)

    assert not source_processing._run_needs_language_detection(tmp_path)
    assert seen == [shard]

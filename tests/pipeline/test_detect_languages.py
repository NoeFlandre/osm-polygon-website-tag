"""Tests for bounded, resumable per-shard language detection."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.fixtures.polygon_shards import legacy_polygon_row

import osm_polygon_website_tag.pipeline.detect_languages as detection
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_4,
)
from osm_polygon_website_tag.contracts.text_schema import initial_text_fields
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity
from osm_polygon_website_tag.pipeline.language_detection_checkpoint import (
    language_checkpoint_store,
)
from osm_polygon_website_tag.runtime.run_state import hash_shard


class RecordingDetector:
    identity = ModelIdentity("repo", "file", "revision", "d" * 64)

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        self.calls.append(list(texts))
        return [
            LanguagePrediction("eng_Latn" if "English" in text else "fra_Latn", 0.91)
            for text in texts
        ]

    @property
    def seen(self) -> list[str]:
        return [text for call in self.calls for text in call]


class InterruptingDetector(RecordingDetector):
    def __init__(self, *, interrupt_on_call: int) -> None:
        super().__init__()
        self.interrupt_on_call = interrupt_on_call

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        result = super().predict(texts)
        if len(self.calls) == self.interrupt_on_call:
            raise KeyboardInterrupt
        return result


class SequenceClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.index = 0

    def __call__(self) -> float:
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


def _v1_3_text_row(
    index: int, *, website_text: str | None, contact_text: str | None = None
) -> dict[str, object]:
    row = legacy_polygon_row(
        polygon_id=f"source:way/{index}",
        website="https://example.org" if website_text is not None else None,
        contact="https://contact.example.org" if contact_text is not None else None,
    )
    for name in (
        "preferred_website",
        "preferred_website_source",
        "wikidata",
        "wikidata_qid",
        "wikidata_class",
        "area_km2",
    ):
        row.pop(name)
    row.update(
        initial_text_fields(
            website_present=website_text is not None,
            contact_website_present=contact_text is not None,
        )
    )
    row.update(
        {
            "website_text": website_text,
            "website_word_count": 2 if website_text is not None else None,
            "website_text_status": "success" if website_text is not None else "absent",
            "contact_website_text": contact_text,
            "contact_website_word_count": 2 if contact_text is not None else None,
            "contact_website_text_status": "success" if contact_text is not None else "absent",
            "schema_version": "v1.3",
        }
    )
    return {name: row[name] for name in POLYGON_PUBLIC_SCHEMA.names}


def _write_v1_3_shard(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    shard = tmp_path / "polygons" / "source.parquet"
    shard.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=POLYGON_PUBLIC_SCHEMA), shard)
    return shard


def _write_v1_4_pair_shard(tmp_path: Path, *, label: object, probability: object) -> Path:
    row = _v1_3_text_row(0, website_text="English text")
    row.update(
        {
            "website_language": label,
            "website_language_probability": probability,
            "contact_website_language": None,
            "contact_website_language_probability": None,
        }
    )
    shard = tmp_path / "polygons" / "source.parquet"
    shard.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA_V1_4), shard)
    return shard


def test_detect_language_shard_populates_website_and_contact_independently(
    tmp_path: Path,
) -> None:
    shard = _write_v1_3_shard(
        tmp_path,
        [
            _v1_3_text_row(0, website_text="English text", contact_text="Texte français"),
            _v1_3_text_row(1, website_text="English only"),
        ],
    )
    detector = RecordingDetector()

    result = detection.detect_language_shard(shard, detector=detector, batch_rows=1)

    table = pq.read_table(shard)
    assert table.schema.equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
    assert [row["polygon_id"] for row in table.to_pylist()] == [
        "source:way/0",
        "source:way/1",
    ]
    assert table["website_language"].to_pylist() == ["eng_Latn", "eng_Latn"]
    assert table["contact_website_language"].to_pylist() == ["fra_Latn", None]
    assert table["website_language_probability"].to_pylist() == [0.91, 0.91]
    assert table["contact_website_language_probability"].to_pylist() == [0.91, None]
    assert detector.calls == [["English text"], ["Texte français"], ["English only"]]
    assert result.changed is True
    assert result.shard_path == shard
    assert result.row_count == 2
    assert result.processed_rows == 2
    assert result.max_batch_rows == 1
    assert result.shard_sha256 == hash_shard(shard)


def test_detect_language_shard_leaves_absent_language_fields_null(tmp_path: Path) -> None:
    shard = _write_v1_3_shard(tmp_path, [_v1_3_text_row(0, website_text="English text")])

    detection.detect_language_shard(shard, detector=RecordingDetector())

    row = pq.read_table(shard).to_pylist()[0]
    assert row["website_language"] == "eng_Latn"
    assert row["contact_website_language"] is None
    assert row["contact_website_language_probability"] is None


def test_interrupt_leaves_original_and_resumes_only_after_durable_prefix(tmp_path: Path) -> None:
    shard = _write_v1_3_shard(
        tmp_path,
        [
            _v1_3_text_row(0, website_text="English 0"),
            _v1_3_text_row(1, website_text="English 1"),
            _v1_3_text_row(2, website_text="English 2"),
            _v1_3_text_row(3, website_text="English 3"),
        ],
    )
    detector = InterruptingDetector(interrupt_on_call=3)

    with pytest.raises(KeyboardInterrupt):
        detection.detect_language_shard(shard, detector=detector, batch_rows=1)

    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    checkpoint_dir = shard.parent / f".{shard.name}.language.parts"
    assert len(list(checkpoint_dir.glob("part-*.parquet"))) == 2
    metadata = json.loads((checkpoint_dir / "checkpoint.json").read_text())
    assert metadata["source_shard_sha256"] == hash_shard(shard)

    resumed = RecordingDetector()
    result = detection.detect_language_shard(shard, detector=resumed, batch_rows=1)

    assert result.changed is True
    assert resumed.seen == ["English 2", "English 3"]
    assert pq.read_table(shard)["website_language"].to_pylist() == [
        "eng_Latn",
        "eng_Latn",
        "eng_Latn",
        "eng_Latn",
    ]
    assert not checkpoint_dir.exists()


def test_time_budget_pauses_between_batches(tmp_path: Path) -> None:
    shard = _write_v1_3_shard(
        tmp_path,
        [_v1_3_text_row(index, website_text=f"English {index}") for index in range(2)],
    )
    clock = SequenceClock([0.0, 0.5, 1.1])

    result = detection.detect_language_shard(
        shard,
        detector=RecordingDetector(),
        batch_rows=1,
        time_budget_seconds=1.0,
        clock=clock,
    )

    assert result.completed is False
    assert result.changed is False
    assert result.shard_path == shard
    assert result.processed_rows == 1
    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    checkpoint_dir = shard.parent / ".source.parquet.language.parts"
    assert len(list(checkpoint_dir.glob("part-*.parquet"))) == 1


def test_completed_v1_4_shard_is_not_reprocessed(tmp_path: Path) -> None:
    shard = _write_v1_3_shard(tmp_path, [_v1_3_text_row(0, website_text="English text")])
    detection.detect_language_shard(shard, detector=RecordingDetector())
    detector = RecordingDetector()

    result = detection.detect_language_shard(shard, detector=detector)

    assert result.changed is False
    assert detector.calls == []
    assert result.shard_path == shard
    assert result.row_count == 1
    assert result.processed_rows == 1
    assert result.max_batch_rows == 0
    assert result.shard_sha256 == hash_shard(shard)


def test_v1_4_shard_with_missing_language_pair_is_completed_in_place(tmp_path: Path) -> None:
    shard = _write_v1_4_pair_shard(tmp_path, label=None, probability=None)

    result = detection.detect_language_shard(shard, detector=RecordingDetector(), batch_rows=1)

    assert result.changed is True
    assert pq.read_table(shard)["website_language"].to_pylist() == ["eng_Latn"]


def test_shard_needs_language_detection_detects_an_incomplete_pair(tmp_path: Path) -> None:
    shard = _write_v1_4_pair_shard(tmp_path, label=None, probability=None)

    assert detection.shard_needs_language_detection(shard) is True


def test_invalid_batch_size_is_rejected_before_opening_the_shard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"^batch_rows must be positive$"):
        detection.detect_language_shard(
            tmp_path / "missing.parquet", detector=RecordingDetector(), batch_rows=0
        )


def test_invalid_time_budget_is_rejected_before_opening_the_shard(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"^time_budget_seconds must be positive$"):
        detection.detect_language_shard(
            tmp_path / "missing.parquet",
            detector=RecordingDetector(),
            time_budget_seconds=0,
        )


@pytest.mark.parametrize("value", [True, None, float("nan"), float("inf"), 0, -1])
def test_language_budget_validation_rejects_non_positive_or_non_finite_values(
    value: object,
) -> None:
    with pytest.raises(ValueError, match=r"^time_budget_seconds must be positive$"):
        detection._validate_positive_time_value(value)


def test_language_detection_rejects_an_unsupported_schema(tmp_path: Path) -> None:
    shard = tmp_path / "wrong.parquet"
    pq.write_table(pa.table({"value": [1]}), shard)

    with pytest.raises(ValueError, match="unsupported polygon schema"):
        detection.shard_needs_language_detection(shard)
    with pytest.raises(ValueError, match="unsupported polygon schema"):
        detection.detect_language_shard(shard, detector=RecordingDetector())


def test_language_detection_rejects_missing_text_status_columns(tmp_path: Path) -> None:
    class MissingStatusParquet:
        schema_arrow = pa.schema([pa.field("website_text_status", pa.string())])

    with pytest.raises(ValueError, match="missing text status columns"):
        detection._validate_text_statuses(
            cast(pq.ParquetFile, MissingStatusParquet()), tmp_path / "shard.parquet"
        )
    with pytest.raises(ValueError, match="missing text status columns"):
        detection._inspect_language_readiness(
            cast(pq.ParquetFile, MissingStatusParquet()),
            tmp_path / "shard.parquet",
            validate_statuses=False,
        )


def test_status_validation_accepts_resolved_values_and_rejects_unfinished_values(
    tmp_path: Path,
) -> None:
    shard = tmp_path / "shard.parquet"
    detection._validate_status_values(
        ["absent", "success", "empty", "invalid_url", "unsafe_url", "fetch_error", "extract_error"],
        shard,
    )

    for status in ("pending", None, "unknown"):
        with pytest.raises(ValueError, match="must be resolved"):
            detection._validate_status_values([status], shard)


def test_status_batch_validates_both_website_columns(tmp_path: Path) -> None:
    batch = pa.RecordBatch.from_arrays(
        [pa.array(["success"]), pa.array(["absent"])],
        ["website_text_status", "contact_website_text_status"],
    )

    detection._validate_status_batch(batch, tmp_path / "shard.parquet")

    invalid_batch = pa.RecordBatch.from_arrays(
        [pa.array(["pending"]), pa.array(["absent"])],
        ["website_text_status", "contact_website_text_status"],
    )
    with pytest.raises(ValueError, match=r"shard\.parquet text statuses"):
        detection._validate_status_batch(invalid_batch, tmp_path / "shard.parquet")


def test_status_validation_requests_only_status_columns_in_bounded_batches() -> None:
    calls: list[dict[str, object]] = []

    class CompleteStatusParquet:
        schema_arrow = pa.schema(
            [pa.field(name, pa.string()) for name in detection._TEXT_STATUS_COLUMNS]
        )

        def iter_batches(self, **kwargs: object) -> list[object]:
            calls.append(kwargs)
            return []

    detection._validate_text_statuses(
        cast(pq.ParquetFile, CompleteStatusParquet()), Path("shard.parquet")
    )

    assert calls == [{"columns": list(detection._TEXT_STATUS_COLUMNS), "batch_size": 8_192}]


def test_status_validation_checks_values_from_each_batch(tmp_path: Path) -> None:
    class InvalidStatusParquet:
        schema_arrow = pa.schema(
            [pa.field(name, pa.string()) for name in detection._TEXT_STATUS_COLUMNS]
        )

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield pa.RecordBatch.from_arrays(
                [pa.array(["pending"]), pa.array(["absent"])],
                list(detection._TEXT_STATUS_COLUMNS),
            )

    with pytest.raises(
        ValueError,
        match=r"^shard\.parquet text statuses must be resolved before language detection$",
    ):
        detection._validate_text_statuses(
            cast(pq.ParquetFile, InvalidStatusParquet()), tmp_path / "shard.parquet"
        )


def test_shard_needs_detection_projects_language_columns_in_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    parquet_paths: list[object] = []

    class CompleteLanguageParquet:
        schema_arrow = POLYGON_PUBLIC_SCHEMA_V1_4

        def iter_batches(self, **kwargs: object) -> list[object]:
            calls.append(kwargs)
            return []

    monkeypatch.setattr(
        detection.pq,
        "ParquetFile",
        lambda path: parquet_paths.append(path) or CompleteLanguageParquet(),
    )

    assert detection.shard_needs_language_detection("shard.parquet") is False
    assert calls == [
        {
            "columns": [
                "website_text_status",
                "contact_website_text_status",
                "website_language",
                "website_language_probability",
                "contact_website_language",
                "contact_website_language_probability",
            ],
            "batch_size": 8_192,
        }
    ]
    assert parquet_paths == [Path("shard.parquet")]


def test_shard_needs_detection_passes_path_and_skips_status_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = Path("shard.parquet")
    observed: dict[str, object] = {}

    class CompleteLanguageParquet:
        schema_arrow = POLYGON_PUBLIC_SCHEMA_V1_4

    monkeypatch.setattr(detection.pq, "ParquetFile", lambda _path: CompleteLanguageParquet())
    monkeypatch.setattr(
        detection,
        "_validate_source_schema",
        lambda schema, shard: observed.update(schema=schema, shard=shard),
    )
    monkeypatch.setattr(
        detection,
        "_inspect_language_readiness",
        lambda parquet, shard, *, validate_statuses: (
            observed.update(parquet=parquet, shard=shard, validate_statuses=validate_statuses)
            or False
        ),
    )

    assert not detection.shard_needs_language_detection(source_path)
    assert observed["shard"] == source_path
    assert observed["validate_statuses"] is False


def test_language_readiness_inspection_is_one_bounded_arrow_scan() -> None:
    calls: list[dict[str, object]] = []

    class CompleteShard:
        schema_arrow = POLYGON_PUBLIC_SCHEMA_V1_4

        def iter_batches(self, **kwargs: object):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            yield pa.RecordBatch.from_arrays(
                [
                    pa.array(["success"]),
                    pa.array(["absent"]),
                    pa.array(["eng_Latn"]),
                    pa.array([0.9]),
                    pa.array([None], type=pa.string()),
                    pa.array([None], type=pa.float64()),
                ],
                [
                    "website_text_status",
                    "contact_website_text_status",
                    "website_language",
                    "website_language_probability",
                    "contact_website_language",
                    "contact_website_language_probability",
                ],
            )

    assert (
        detection._inspect_language_readiness(
            cast(pq.ParquetFile, CompleteShard()),
            Path("shard.parquet"),
            validate_statuses=False,
        )
        is False
    )
    assert calls == [
        {
            "columns": [*detection._TEXT_STATUS_COLUMNS, *detection.LANGUAGE_COLUMN_NAMES],
            "batch_size": 8_192,
        }
    ]


def test_language_readiness_inspection_detects_missing_success_pair() -> None:
    class IncompleteShard:
        schema_arrow = POLYGON_PUBLIC_SCHEMA_V1_4

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield pa.RecordBatch.from_arrays(
                [
                    pa.array(["success"]),
                    pa.array(["absent"]),
                    pa.array([None], type=pa.string()),
                    pa.array([None], type=pa.float64()),
                    pa.array([None], type=pa.string()),
                    pa.array([None], type=pa.float64()),
                ],
                [
                    "website_text_status",
                    "contact_website_text_status",
                    "website_language",
                    "website_language_probability",
                    "contact_website_language",
                    "contact_website_language_probability",
                ],
            )

    assert (
        detection._inspect_language_readiness(
            cast(pq.ParquetFile, IncompleteShard()),
            Path("shard.parquet"),
            validate_statuses=True,
        )
        is True
    )


def test_legacy_readiness_passes_shard_to_status_validation(tmp_path: Path) -> None:
    row = _v1_3_text_row(0, website_text="English text")
    row["website_text_status"] = "pending"
    shard = _write_v1_3_shard(tmp_path, [row])

    with pytest.raises(
        ValueError,
        match=r"^source\.parquet text statuses must be resolved before language detection$",
    ):
        detection._inspect_language_readiness(
            pq.ParquetFile(shard),
            shard,
            validate_statuses=True,
        )


def test_prepare_detection_context_forwards_shard_and_status_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shard = _write_v1_3_shard(tmp_path, [_v1_3_text_row(0, website_text="English text")])
    detector = RecordingDetector()
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        detection,
        "_inspect_language_readiness",
        lambda parquet, actual_shard, *, validate_statuses: (
            observed.update(
                parquet=parquet,
                shard=actual_shard,
                validate_statuses=validate_statuses,
            )
            or True
        ),
    )

    context = detection._prepare_detection_context(shard, detector)

    assert context is not None
    assert observed["shard"] == shard
    assert observed["validate_statuses"] is True
    shutil.rmtree(context.checkpoint.directory)


def test_language_readiness_inspection_validates_later_batches() -> None:
    class InvalidLaterBatchShard:
        schema_arrow = POLYGON_PUBLIC_SCHEMA_V1_4

        def iter_batches(self, **_kwargs: object):  # type: ignore[no-untyped-def]
            for status in ("success", "pending"):
                yield pa.RecordBatch.from_arrays(
                    [
                        pa.array([status]),
                        pa.array(["absent"]),
                        pa.array([None], type=pa.string()),
                        pa.array([None], type=pa.float64()),
                        pa.array([None], type=pa.string()),
                        pa.array([None], type=pa.float64()),
                    ],
                    [
                        "website_text_status",
                        "contact_website_text_status",
                        "website_language",
                        "website_language_probability",
                        "contact_website_language",
                        "contact_website_language_probability",
                    ],
                )

    with pytest.raises(ValueError, match="must be resolved"):
        detection._inspect_language_readiness(
            cast(pq.ParquetFile, InvalidLaterBatchShard()),
            Path("shard.parquet"),
            validate_statuses=True,
        )


@pytest.mark.parametrize(
    ("status", "label", "probability", "expected"),
    [
        ("success", "eng_Latn", 0.9, False),
        ("success", None, None, True),
        ("success", "", 0.9, True),
        ("success", "eng_Latn", None, True),
        ("success", "eng_Latn", -0.1, True),
        ("success", "eng_Latn", 1.1, True),
        ("absent", None, None, False),
        ("absent", "eng_Latn", None, True),
        ("failed", None, 0.5, True),
        (None, "eng_Latn", None, True),
    ],
)
def test_arrow_language_pair_readiness_is_status_aware(
    status: object,
    label: object,
    probability: object,
    expected: bool,
) -> None:
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array([status]),
            pa.array([status]),
            pa.array([label], type=pa.string()),
            pa.array([probability], type=pa.float64()),
            pa.array([None], type=pa.string()),
            pa.array([None], type=pa.float64()),
        ],
        [
            "website_text_status",
            "contact_website_text_status",
            "website_language",
            "website_language_probability",
            "contact_website_language",
            "contact_website_language_probability",
        ],
    )

    assert detection._language_pair_needs_detection_arrow(batch, "website") is expected


def test_batch_language_readiness_checks_contact_pair() -> None:
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array(["absent"]),
            pa.array(["success"]),
            pa.array([None], type=pa.string()),
            pa.array([None], type=pa.float64()),
            pa.array([None], type=pa.string()),
            pa.array([None], type=pa.float64()),
        ],
        [
            "website_text_status",
            "contact_website_text_status",
            "website_language",
            "website_language_probability",
            "contact_website_language",
            "contact_website_language_probability",
        ],
    )

    assert detection._batch_needs_language_detection(batch)


def test_skip_checkpointed_rows_preserves_remaining_skip_count() -> None:
    originals: list[dict[str, object]] = [{"id": 1}, {"id": 2}]

    assert detection._skip_checkpointed_rows(originals, 3) == ([], 1)
    assert detection._skip_checkpointed_rows(originals, 2) == ([], 0)
    assert detection._skip_checkpointed_rows(originals, 1) == ([{"id": 2}], 0)
    assert detection._skip_checkpointed_rows(originals, 0) == (originals, 0)


def test_completed_process_progress_retains_rows_and_completion() -> None:
    class EmptyParquet:
        def iter_batches(self, *, batch_size: int) -> list[object]:
            assert batch_size == 2
            return []

    progress = detection._process_detection_batches_with_progress(
        cast(pq.ParquetFile, EmptyParquet()),
        2,
        Checkpoint(Path("parts"), (), 2),
        store=language_checkpoint_store(),
        next_part_index=0,
        detector=RecordingDetector(),
        batch_rows=2,
        deadline=None,
        clock=lambda: 0.0,
    )

    assert progress == detection._DetectionProgress(2, 0, completed=True)


def test_deadline_is_reached_at_the_exact_boundary() -> None:
    assert detection._deadline_reached(1.0, lambda: 1.0) is True


def test_process_batches_reports_a_paused_progress_state_at_the_deadline() -> None:
    class OneBatchParquet:
        def iter_batches(self, *, batch_size: int) -> list[object]:
            assert batch_size == 1
            return [
                pa.RecordBatch.from_pylist(
                    [
                        {
                            "website_text_status": "absent",
                            "contact_website_text_status": "absent",
                        }
                    ]
                )
            ]

    progress = detection._process_detection_batches_with_progress(
        cast(pq.ParquetFile, OneBatchParquet()),
        1,
        Checkpoint(Path("parts"), (), 0),
        store=language_checkpoint_store(),
        next_part_index=0,
        detector=RecordingDetector(),
        batch_rows=1,
        deadline=1.0,
        clock=lambda: 1.0,
    )

    assert progress.completed is False


def test_prepare_row_resets_language_fields_for_absent_text() -> None:
    row = _v1_3_text_row(0, website_text=None)
    row.update(
        {
            "website_language": "eng_Latn",
            "website_language_probability": 0.9,
            "contact_website_language": "fra_Latn",
            "contact_website_language_probability": 0.8,
        }
    )

    prepared = detection._prepare_row(row)

    assert prepared["website_language"] is None
    assert prepared["website_language_probability"] is None
    assert prepared["schema_version"] == "v1.4"


def test_prepare_row_adds_missing_language_fields() -> None:
    prepared = detection._prepare_row(_v1_3_text_row(0, website_text="English text"))

    assert {name: prepared[name] for name in detection.LANGUAGE_COLUMN_NAMES} == {
        "website_language": None,
        "website_language_probability": None,
        "contact_website_language": None,
        "contact_website_language_probability": None,
    }


def test_prepare_detection_batch_collects_only_pending_successful_texts() -> None:
    pending = _v1_3_text_row(0, website_text="English text", contact_text=None)
    complete = _v1_3_text_row(1, website_text="Already English", contact_text=None)
    complete.update({"website_language": "eng_Latn", "website_language_probability": 0.9})

    rows, pending_by_prefix = detection._prepare_detection_batch([pending, complete])

    assert len(rows) == 2
    assert pending_by_prefix["website"] == [(rows[0], "English text")]
    assert pending_by_prefix["contact_website"] == []


def test_successful_text_must_be_a_string() -> None:
    row = _v1_3_text_row(0, website_text="English text")
    row["website_text"] = None

    with pytest.raises(ValueError, match="successful website text is not a string"):
        detection._queue_successful_text(row, "website", [])


def test_pending_prediction_count_mismatch_fails_closed() -> None:
    row = _v1_3_text_row(0, website_text="English text")
    pending = [(row, "English text")]

    class MissingPredictionDetector(RecordingDetector):
        def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
            del texts
            return []

    with pytest.raises(ValueError, match="prediction count"):
        detection._apply_pending_predictions("website", pending, MissingPredictionDetector())


def test_pending_predictions_are_applied_to_the_corresponding_rows() -> None:
    first = _v1_3_text_row(0, website_text="English text")
    second = _v1_3_text_row(1, website_text="French text")
    pending = [(first, "English text"), (second, "French text")]

    detection._apply_pending_predictions("website", pending, RecordingDetector())

    assert first["website_language"] == "eng_Latn"
    assert second["website_language"] == "fra_Latn"


def test_existing_complete_language_pair_is_not_queued() -> None:
    row = _v1_3_text_row(0, website_text="English text")
    row.update({"website_language": "eng_Latn", "website_language_probability": 0.9})
    pending: list[tuple[dict[str, object], str]] = []

    detection._queue_language_text(row, "website", "English text", pending)

    assert pending == []


def test_empty_language_pair_is_queued_with_its_row_and_text() -> None:
    row = _v1_3_text_row(0, website_text="English text")
    pending: list[tuple[dict[str, object], str]] = []

    detection._queue_language_text(row, "website", "English text", pending)

    assert pending == [(row, "English text")]


def test_empty_language_pair_requires_both_values_to_be_null() -> None:
    assert detection._empty_language_pair(None, None) is True
    assert detection._empty_language_pair("eng_Latn", None) is False
    assert detection._empty_language_pair(None, 0.9) is False
    assert detection._empty_language_pair("eng_Latn", 0.9) is False


@pytest.mark.parametrize(
    ("label", "probability", "message"),
    [
        ("eng_Latn", None, "incomplete website language pair"),
        (None, 0.9, "incomplete website language pair"),
        ("", 0.9, "invalid existing website language pair"),
        ("eng_Latn", 1.1, "invalid existing website language pair"),
    ],
)
def test_existing_partial_or_invalid_language_pair_fails_closed(
    label: object, probability: object, message: str
) -> None:
    row = _v1_3_text_row(0, website_text="English text")
    row.update({"website_language": label, "website_language_probability": probability})

    with pytest.raises(ValueError, match=message):
        detection._queue_language_text(row, "website", "English text", [])


def test_invalid_language_pair_error_helper_has_specific_messages() -> None:
    with pytest.raises(ValueError, match="incomplete website language pair"):
        detection._raise_invalid_language_pair("website", None, 0.9)
    with pytest.raises(ValueError, match="incomplete website language pair"):
        detection._raise_invalid_language_pair("website", "eng_Latn", None)
    with pytest.raises(ValueError, match="invalid existing website language pair"):
        detection._raise_invalid_language_pair("website", "eng_Latn", 1.1)


@pytest.mark.parametrize(
    ("label", "probability", "expected"),
    [
        ("eng_Latn", 0.9, True),
        ("", 0.9, False),
        (None, 0.9, False),
        ("eng_Latn", None, False),
    ],
)
def test_complete_language_pair_requires_label_and_valid_probability(
    label: object, probability: object, expected: bool
) -> None:
    assert detection._complete_language_pair(label, probability) is expected


def test_invalid_language_prediction_and_probability_fail_closed() -> None:
    row: dict[str, object] = {}

    with pytest.raises(ValueError, match="invalid website language prediction"):
        detection._apply_prediction(row, "website", cast(LanguagePrediction, object()))
    with pytest.raises(ValueError, match="invalid website language prediction"):
        detection._apply_prediction(row, "website", LanguagePrediction("", 0.9))
    with pytest.raises(ValueError, match="invalid website language probability"):
        detection._apply_prediction(row, "website", LanguagePrediction("eng_Latn", 1.1))
    detection._apply_prediction(row, "website", LanguagePrediction("eng_Latn", 0.9))
    assert row == {"website_language": "eng_Latn", "website_language_probability": 0.9}


@pytest.mark.parametrize("value", [0, 1, 0.5])
def test_valid_probability_accepts_unit_interval(value: float) -> None:
    assert detection._valid_probability(value) is True


@pytest.mark.parametrize("value", [True, None, -0.1, 1.1])
def test_valid_probability_rejects_invalid_values(value: object) -> None:
    assert detection._valid_probability(value) is False


def test_resolved_non_success_text_status_is_preserved_without_language(tmp_path: Path) -> None:
    shard = _write_v1_3_shard(tmp_path, [_v1_3_text_row(0, website_text="English text")])
    row = pq.read_table(shard).to_pylist()[0]
    row["website_text_status"] = "fetch_error"
    row["website_text"] = None
    row["website_word_count"] = None
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA), shard)

    detector = RecordingDetector()
    result = detection.detect_language_shard(shard, detector=detector)

    assert result.completed is True
    assert result.changed is True
    assert detector.calls == []
    output = pq.read_table(shard)
    assert output.schema.equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)
    assert output["website_language"].to_pylist() == [None]
    assert output["website_language_probability"].to_pylist() == [None]


@pytest.mark.parametrize(
    ("label", "probability", "error"),
    [
        ("eng_Latn", None, "incomplete"),
        ("eng_Latn", 1.1, "invalid"),
    ],
)
def test_existing_incomplete_or_invalid_language_pair_fails_closed(
    tmp_path: Path,
    label: object,
    probability: object,
    error: str,
) -> None:
    shard = _write_v1_4_pair_shard(tmp_path, label=label, probability=probability)

    with pytest.raises(ValueError, match=error):
        detection.detect_language_shard(shard, detector=RecordingDetector())

    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA_V1_4, check_metadata=True)

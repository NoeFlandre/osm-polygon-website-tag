"""Tests for bounded, resumable per-shard language detection."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

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
from osm_polygon_website_tag.pipeline.glotlid import LanguagePrediction, ModelIdentity


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
        [_v1_3_text_row(0, website_text="English 0"), _v1_3_text_row(1, website_text="English 1")],
    )
    detector = InterruptingDetector(interrupt_on_call=2)

    with pytest.raises(KeyboardInterrupt):
        detection.detect_language_shard(shard, detector=detector, batch_rows=1)

    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    checkpoint_dir = shard.parent / f".{shard.name}.language.parts"
    assert len(list(checkpoint_dir.glob("part-*.parquet"))) == 1

    resumed = RecordingDetector()
    result = detection.detect_language_shard(shard, detector=resumed, batch_rows=1)

    assert result.changed is True
    assert resumed.seen == ["English 1"]
    assert pq.read_table(shard)["website_language"].to_pylist() == [
        "eng_Latn",
        "eng_Latn",
    ]
    assert not checkpoint_dir.exists()


def test_completed_v1_4_shard_is_not_reprocessed(tmp_path: Path) -> None:
    shard = _write_v1_3_shard(tmp_path, [_v1_3_text_row(0, website_text="English text")])
    detection.detect_language_shard(shard, detector=RecordingDetector())
    detector = RecordingDetector()

    result = detection.detect_language_shard(shard, detector=detector)

    assert result.changed is False
    assert detector.calls == []


def test_nonterminal_text_status_fails_before_promotion(tmp_path: Path) -> None:
    shard = _write_v1_3_shard(tmp_path, [_v1_3_text_row(0, website_text="English text")])
    row = pq.read_table(shard).to_pylist()[0]
    row["website_text_status"] = "fetch_error"
    pq.write_table(pa.Table.from_pylist([row], schema=POLYGON_PUBLIC_SCHEMA), shard)

    with pytest.raises(ValueError, match="terminal"):
        detection.detect_language_shard(shard, detector=RecordingDetector())

    assert pq.read_schema(shard).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)

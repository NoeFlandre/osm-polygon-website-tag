"""Bounded transactional website-text enrichment for one polygon shard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.text_schema import TEXT_COLUMN_NAMES, initial_text_fields
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink
from osm_polygon_website_tag.web.text_cache import CachedText, TextCache
from osm_polygon_website_tag.web.text_extract import TextExtraction, extract_main_text
from osm_polygon_website_tag.web.web_fetch import FetchResult, fetch_html, normalize_http_url

DEFAULT_BATCH_ROWS = 512
Fetcher = Callable[[str], FetchResult]
Extractor = Callable[..., TextExtraction]


@dataclass(frozen=True)
class EnrichmentResult:
    """Outcome of enriching one polygon shard."""

    shard_path: Path
    row_count: int
    changed: bool
    shard_sha256: str
    max_batch_rows: int


def enrich_polygon_shard(
    shard_path: Path | str,
    *,
    cache_path: Path | str,
    invocation_id: str,
    fetcher: Fetcher = fetch_html,
    extractor: Extractor = extract_main_text,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> EnrichmentResult:
    """Migrate/enrich one shard without reading its source PBF."""
    shard = Path(shard_path)
    cache = TextCache(Path(cache_path))
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    if not (
        source_schema.equals(POLYGON_PUBLIC_SCHEMA_V1_1, check_metadata=True)
        or source_schema.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    ):
        cache.close()
        raise ValueError(f"unsupported polygon schema for enrichment: {shard.name}")

    staged = shard.with_name(f".{shard.name}.enriching")
    staged.unlink(missing_ok=True)
    sink = BatchParquetSink(staged, POLYGON_PUBLIC_SCHEMA, batch_rows=batch_rows)
    changed = source_schema.equals(POLYGON_PUBLIC_SCHEMA_V1_1, check_metadata=True)
    try:
        for batch in parquet.iter_batches(batch_size=batch_rows):
            for original in batch.to_pylist():
                row = dict(original)
                if source_schema.equals(POLYGON_PUBLIC_SCHEMA_V1_1, check_metadata=True):
                    row.update(
                        initial_text_fields(
                            website_present=row["website"] is not None,
                            contact_website_present=row["contact_website"] is not None,
                        )
                    )
                before = tuple(row.get(name) for name in TEXT_COLUMN_NAMES)
                _enrich_tag(
                    row,
                    value_column="website",
                    field_prefix="website",
                    cache=cache,
                    invocation_id=invocation_id,
                    fetcher=fetcher,
                    extractor=extractor,
                )
                _enrich_tag(
                    row,
                    value_column="contact_website",
                    field_prefix="contact_website",
                    cache=cache,
                    invocation_id=invocation_id,
                    fetcher=fetcher,
                    extractor=extractor,
                )
                row["schema_version"] = SCHEMA_VERSION
                after = tuple(row.get(name) for name in TEXT_COLUMN_NAMES)
                changed = (
                    changed or before != after or original.get("schema_version") != SCHEMA_VERSION
                )
                sink.add(row)
        sink.close()
        if sink.row_count != parquet.metadata.num_rows:
            raise ValueError("enrichment row count changed")
        if changed:
            if not pq.read_schema(staged).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True):
                raise ValueError("enriched shard schema mismatch")
            atomic_promote_bundle([(staged, shard)])
        else:
            staged.unlink(missing_ok=True)
    except BaseException:
        sink.close()
        staged.unlink(missing_ok=True)
        raise
    finally:
        cache.close()
    return EnrichmentResult(
        shard_path=shard,
        row_count=parquet.metadata.num_rows,
        changed=changed,
        shard_sha256=hash_shard(shard),
        max_batch_rows=sink.max_pending_rows,
    )


def _enrich_tag(
    row: dict[str, object],
    *,
    value_column: str,
    field_prefix: str,
    cache: TextCache,
    invocation_id: str,
    fetcher: Fetcher,
    extractor: Extractor,
) -> None:
    value = row.get(value_column)
    text_column = f"{field_prefix}_text"
    count_column = f"{field_prefix}_word_count"
    status_column = f"{field_prefix}_text_status"
    if not isinstance(value, str) or not value:
        row[text_column] = None
        row[count_column] = None
        row[status_column] = "absent"
        return
    if (
        row.get(status_column) == "success"
        and isinstance(row.get(text_column), str)
        and isinstance(row.get(count_column), int)
    ):
        return
    try:
        normalized = normalize_http_url(value)
    except ValueError:
        _apply_result(
            row,
            field_prefix,
            CachedText(
                value,
                "invalid_url",
                None,
                None,
                None,
                "invalid_url",
                0,
                "",
                None,
                invocation_id,
            ),
        )
        return
    cached = cache.get_reusable(normalized, invocation_id=invocation_id)
    if cached is None:
        cached = _fetch_and_extract(
            normalized,
            invocation_id=invocation_id,
            fetcher=fetcher,
            extractor=extractor,
        )
        cached = cache.record(cached, invocation_id=invocation_id)
    _apply_result(row, field_prefix, cached)


def _fetch_and_extract(
    url: str,
    *,
    invocation_id: str,
    fetcher: Fetcher,
    extractor: Extractor,
) -> CachedText:
    fetched = fetcher(url)
    if fetched.status != "ok" or fetched.body is None:
        return CachedText(
            url,
            fetched.status,
            None,
            None,
            fetched.final_url,
            fetched.message,
            0,
            "",
            None,
            invocation_id,
        )
    final_url = fetched.final_url or url
    extracted = extractor(fetched.body, url=final_url)
    return CachedText(
        url,
        extracted.status,
        extracted.text,
        extracted.word_count,
        final_url,
        extracted.message,
        0,
        "",
        extracted.trafilatura_version,
        invocation_id,
    )


def _apply_result(row: dict[str, object], prefix: str, value: CachedText) -> None:
    row[f"{prefix}_text"] = value.text
    row[f"{prefix}_word_count"] = value.word_count
    row[f"{prefix}_text_status"] = value.status


__all__ = ["EnrichmentResult", "enrich_polygon_shard"]

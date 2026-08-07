"""Bounded transactional website-text enrichment for one polygon shard."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_2,
    SCHEMA_VERSION,
)
from osm_polygon_website_tag.contracts.text_schema import TEXT_COLUMN_NAMES, initial_text_fields
from osm_polygon_website_tag.runtime.run_state import atomic_write_json, hash_shard
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.storage.batch_sink import BatchParquetSink
from osm_polygon_website_tag.web.text_cache import CachedText, TextCache
from osm_polygon_website_tag.web.text_extract import TextExtraction, extract_main_text
from osm_polygon_website_tag.web.web_fetch import FetchResult, fetch_html, normalize_http_url

DEFAULT_BATCH_ROWS = 512
# Fetching is I/O-bound; keep the pool bounded so one shard cannot create an
# unbounded number of sockets or put avoidable pressure on public websites.
DEFAULT_FETCH_WORKERS = 8
MAX_FETCH_WORKERS = 32
CHECKPOINT_VERSION = 1
CHECKPOINT_DIRECTORY_SUFFIX = ".enriching.parts"
CHECKPOINT_METADATA_NAME = "checkpoint.json"
Fetcher = Callable[[str], FetchResult]
Extractor = Callable[..., TextExtraction]


@dataclass(frozen=True)
class _RowState:
    """One input row plus its pre-enrichment values for change detection."""

    original: dict[str, object]
    row: dict[str, object]
    before: tuple[object, ...]


@dataclass(frozen=True)
class _Checkpoint:
    """Durable prefix of one shard's enriched rows."""

    directory: Path
    parts: tuple[Path, ...]
    completed_rows: int


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
    fetch_workers: int | None = None,
) -> EnrichmentResult:
    """Migrate/enrich one shard without reading its source PBF.

    Completed batches are source-hash-bound checkpoint parts. They remain until
    the final shard promotion succeeds, allowing a graceful interruption to
    resume without refetching or reprocessing the completed prefix.
    """
    shard = Path(shard_path)
    workers = DEFAULT_FETCH_WORKERS if fetch_workers is None else fetch_workers
    if not 1 <= workers <= MAX_FETCH_WORKERS:
        raise ValueError(f"fetch_workers must be between 1 and {MAX_FETCH_WORKERS}")
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    source_row_count = parquet.metadata.num_rows
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")
    if not (
        source_schema.equals(POLYGON_PUBLIC_SCHEMA_V1_1, check_metadata=True)
        or source_schema.equals(POLYGON_PUBLIC_SCHEMA_V1_2, check_metadata=True)
        or source_schema.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True)
    ):
        raise ValueError(f"unsupported polygon schema for enrichment: {shard.name}")

    cache = TextCache(Path(cache_path))
    try:
        checkpoint = _load_checkpoint(
            shard,
            source_row_count=source_row_count,
            source_shard_sha256=hash_shard(shard),
        )
    except BaseException:
        cache.close()
        raise
    staged = shard.with_name(f".{shard.name}.enriching")
    staged.unlink(missing_ok=True)
    changed = not source_schema.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True) or bool(
        checkpoint.parts
    )
    processed_rows = checkpoint.completed_rows
    next_part_index = len(checkpoint.parts)
    max_batch_rows = 0
    try:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="website-fetch",
        ) as fetch_pool:
            rows_to_skip = checkpoint.completed_rows
            for batch in parquet.iter_batches(batch_size=batch_rows):
                originals = batch.to_pylist()
                if rows_to_skip >= len(originals):
                    rows_to_skip -= len(originals)
                    continue
                if rows_to_skip:
                    originals = originals[rows_to_skip:]
                    rows_to_skip = 0
                states: list[_RowState] = []
                pending: dict[str, list[tuple[dict[str, object], str]]] = {}
                lookup_urls: set[str] = set()
                for original in originals:
                    row = dict(original)
                    if source_schema.equals(POLYGON_PUBLIC_SCHEMA_V1_1, check_metadata=True):
                        row.update(
                            initial_text_fields(
                                website_present=row["website"] is not None,
                                contact_website_present=row["contact_website"] is not None,
                            )
                        )
                    before = tuple(row.get(name) for name in TEXT_COLUMN_NAMES)
                    _queue_tag(
                        row,
                        value_column="website",
                        field_prefix="website",
                        invocation_id=invocation_id,
                        pending=pending,
                        lookup_urls=lookup_urls,
                    )
                    _queue_tag(
                        row,
                        value_column="contact_website",
                        field_prefix="contact_website",
                        invocation_id=invocation_id,
                        pending=pending,
                        lookup_urls=lookup_urls,
                    )
                    states.append(_RowState(original, row, before))
                unresolved_pending: dict[str, list[tuple[dict[str, object], str]]] = {}
                if lookup_urls:
                    cached_by_url = cache.get_reusable_many(
                        lookup_urls,
                        invocation_id=invocation_id,
                    )
                    for url, references in pending.items():
                        cached = cached_by_url.get(url)
                        if cached is None:
                            unresolved_pending[url] = references
                            continue
                        for row, field_prefix in references:
                            _apply_result(row, field_prefix, cached)
                _resolve_pending(
                    unresolved_pending,
                    cache=cache,
                    invocation_id=invocation_id,
                    fetcher=fetcher,
                    extractor=extractor,
                    fetch_pool=fetch_pool,
                )
                enriched_rows: list[dict[str, object]] = []
                for state in states:
                    state.row["schema_version"] = SCHEMA_VERSION
                    after = tuple(state.row.get(name) for name in TEXT_COLUMN_NAMES)
                    changed = (
                        changed
                        or state.before != after
                        or state.original.get("schema_version") != SCHEMA_VERSION
                    )
                    enriched_rows.append(state.row)
                cache.flush()
                _write_checkpoint_part(
                    checkpoint.directory,
                    next_part_index,
                    enriched_rows,
                    batch_rows=batch_rows,
                )
                next_part_index += 1
                processed_rows += len(enriched_rows)
                max_batch_rows = max(max_batch_rows, len(enriched_rows))
        if processed_rows != source_row_count:
            raise ValueError("enrichment row count changed")
        parts = _checkpoint_parts(checkpoint.directory)
        assembled_max_batch_rows = _assemble_checkpoint(
            parts,
            staged,
            batch_rows=batch_rows,
            row_count=source_row_count,
        )
        max_batch_rows = max(max_batch_rows, assembled_max_batch_rows)
        if changed:
            if not pq.read_schema(staged).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True):
                raise ValueError("enriched shard schema mismatch")
            atomic_promote_bundle([(staged, shard)])
        else:
            staged.unlink(missing_ok=True)
        shutil.rmtree(checkpoint.directory)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    finally:
        cache.close()
    return EnrichmentResult(
        shard_path=shard,
        row_count=source_row_count,
        changed=changed,
        shard_sha256=hash_shard(shard),
        max_batch_rows=max_batch_rows,
    )


def _checkpoint_directory(shard: Path) -> Path:
    return shard.with_name(f".{shard.name}{CHECKPOINT_DIRECTORY_SUFFIX}")


def _checkpoint_part_path(directory: Path, index: int) -> Path:
    return directory / f"part-{index:08d}.parquet"


def _write_checkpoint_metadata(
    directory: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
) -> None:
    atomic_write_json(
        directory / CHECKPOINT_METADATA_NAME,
        {
            "checkpoint_version": CHECKPOINT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source_row_count": source_row_count,
            "source_shard_sha256": source_shard_sha256,
        },
    )


def _checkpoint_parts(directory: Path) -> tuple[Path, ...]:
    """Validate and return sequential durable checkpoint parts."""
    parts = sorted(directory.glob("part-*.parquet"), key=lambda path: path.name)
    total_rows = 0
    for index, part in enumerate(parts):
        if part.name != _checkpoint_part_path(directory, index).name:
            raise ValueError(f"non-sequential enrichment checkpoint part: {part.name}")
        parquet = pq.ParquetFile(part)
        if not parquet.schema_arrow.equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True):
            raise ValueError(f"invalid enrichment checkpoint schema: {part.name}")
        if parquet.metadata.num_rows < 1:
            raise ValueError(f"empty enrichment checkpoint part: {part.name}")
        total_rows += parquet.metadata.num_rows
    return tuple(parts)


def _load_checkpoint(
    shard: Path,
    *,
    source_row_count: int,
    source_shard_sha256: str,
) -> _Checkpoint:
    """Load a source-bound checkpoint or create its empty durable directory."""
    directory = _checkpoint_directory(shard)
    directory.mkdir(parents=True, exist_ok=True)
    for temporary in directory.glob(".*.writing"):
        temporary.unlink(missing_ok=True)
    metadata_path = directory / CHECKPOINT_METADATA_NAME
    metadata_path.with_suffix(metadata_path.suffix + ".tmp").unlink(missing_ok=True)
    if metadata_path.exists():
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "source_row_count": source_row_count,
            "source_shard_sha256": source_shard_sha256,
        }
        if payload != expected:
            raise ValueError(f"enrichment checkpoint does not match source shard: {shard.name}")
    else:
        if any(directory.iterdir()):
            raise ValueError(f"unrecognized enrichment checkpoint contents: {directory}")
        _write_checkpoint_metadata(
            directory,
            source_row_count=source_row_count,
            source_shard_sha256=source_shard_sha256,
        )
    parts = _checkpoint_parts(directory)
    allowed = {CHECKPOINT_METADATA_NAME, *(part.name for part in parts)}
    unknown = sorted(child.name for child in directory.iterdir() if child.name not in allowed)
    if unknown:
        raise ValueError(f"unrecognized enrichment checkpoint contents: {unknown}")
    completed_rows = sum(pq.ParquetFile(part).metadata.num_rows for part in parts)
    if completed_rows > source_row_count:
        raise ValueError(f"enrichment checkpoint exceeds source row count: {shard.name}")
    return _Checkpoint(directory, parts, completed_rows)


def _write_checkpoint_part(
    directory: Path,
    index: int,
    rows: list[dict[str, object]],
    *,
    batch_rows: int,
) -> None:
    """Write one completed enrichment batch and publish it atomically."""
    if not rows:
        return
    target = _checkpoint_part_path(directory, index)
    if target.exists():
        raise ValueError(f"enrichment checkpoint part already exists: {target.name}")
    temporary = directory / f".{target.name}.writing"
    sink = BatchParquetSink(temporary, POLYGON_PUBLIC_SCHEMA, batch_rows=batch_rows)
    try:
        for row in rows:
            sink.add(row)
        sink.close()
        if sink.row_count != len(rows):
            raise ValueError("enrichment checkpoint row count changed")
        if not pq.read_schema(temporary).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True):
            raise ValueError("enrichment checkpoint schema mismatch")
        atomic_promote_bundle([(temporary, target)])
    finally:
        sink.close()
        temporary.unlink(missing_ok=True)


def _assemble_checkpoint(
    parts: tuple[Path, ...],
    staged: Path,
    *,
    batch_rows: int,
    row_count: int,
) -> int:
    """Stream durable parts into one final staged Parquet.

    Checkpoint parts already contain Arrow record batches in the target
    schema. Writing those batches directly avoids converting every row to a
    Python dictionary and back during final assembly while retaining the
    existing bounded row-group size and deterministic part order.
    """
    staged.unlink(missing_ok=True)
    assembled_rows = 0
    max_batch_rows = 0
    try:
        with pq.ParquetWriter(staged, POLYGON_PUBLIC_SCHEMA, compression="snappy") as writer:
            for part in parts:
                parquet = pq.ParquetFile(part)
                for batch in parquet.iter_batches(batch_size=batch_rows):
                    writer.write_batch(batch)
                    assembled_rows += batch.num_rows
                    max_batch_rows = max(max_batch_rows, batch.num_rows)
        if assembled_rows != row_count:
            raise ValueError("enrichment row count changed while assembling checkpoint")
        if not pq.read_schema(staged).equals(POLYGON_PUBLIC_SCHEMA, check_metadata=True):
            raise ValueError("assembled enrichment schema mismatch")
        return max_batch_rows
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _queue_tag(
    row: dict[str, object],
    *,
    value_column: str,
    field_prefix: str,
    invocation_id: str,
    pending: dict[str, list[tuple[dict[str, object], str]]],
    lookup_urls: set[str],
) -> None:
    """Resolve local cases and collect one cache lookup per normalized URL."""
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
    lookup_urls.add(normalized)
    pending.setdefault(normalized, []).append((row, field_prefix))


def _resolve_pending(
    pending: dict[str, list[tuple[dict[str, object], str]]],
    *,
    cache: TextCache,
    invocation_id: str,
    fetcher: Fetcher,
    extractor: Extractor,
    fetch_pool: ThreadPoolExecutor,
) -> None:
    """Fetch cache misses concurrently, then extract and write results serially.

    ``TextCache`` deliberately remains confined to the caller thread because it
    owns one SQLite connection. Network retrieval can safely fan out, but the
    Trafilatura/lxml parser is kept on the caller thread because its native
    parser state is not safe to run concurrently on this platform. Cache
    writes, extraction, and row application stay ordered and deterministic.
    """
    futures = {
        url: fetch_pool.submit(
            _fetch,
            url,
            fetcher=fetcher,
        )
        for url in pending
    }
    recorded: set[str] = set()
    try:
        for url, future in futures.items():
            recorded.add(url)
            fetched = future.result()
            cached = cache.record(
                _extract_fetched(
                    url,
                    fetched,
                    invocation_id=invocation_id,
                    extractor=extractor,
                ),
                invocation_id=invocation_id,
            )
            for row, field_prefix in pending[url]:
                _apply_result(row, field_prefix, cached)
    except KeyboardInterrupt:
        # Preserve every result that already completed while Ctrl-C was
        # delivered. Cancel queued work; running requests finish under the
        # executor's bounded shutdown, then their results are checkpointed.
        for future in futures.values():
            future.cancel()
        for url, future in futures.items():
            if url in recorded or future.cancelled():
                continue
            try:
                fetched = future.result()
            except BaseException:
                fetched = None
            if fetched is not None:
                cached = cache.record(
                    _extract_fetched(
                        url,
                        fetched,
                        invocation_id=invocation_id,
                        extractor=extractor,
                    ),
                    invocation_id=invocation_id,
                )
                for row, field_prefix in pending[url]:
                    _apply_result(row, field_prefix, cached)
        cache.flush()
        raise


def _fetch(url: str, *, fetcher: Fetcher) -> FetchResult:
    """Retrieve one URL; native text parsing happens on the caller thread."""
    return fetcher(url)


def _extract_fetched(
    url: str,
    fetched: FetchResult,
    *,
    invocation_id: str,
    extractor: Extractor,
) -> CachedText:
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


__all__ = [
    "DEFAULT_FETCH_WORKERS",
    "MAX_FETCH_WORKERS",
    "EnrichmentResult",
    "enrich_polygon_shard",
]

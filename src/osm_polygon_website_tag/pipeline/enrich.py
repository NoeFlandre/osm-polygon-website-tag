"""Bounded website-text enrichment orchestration for one polygon shard."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from osm_polygon_website_tag.contracts.language_schema import LANGUAGE_SCHEMA_VERSION
from osm_polygon_website_tag.contracts.polygon_schema import (
    POLYGON_PUBLIC_SCHEMA,
    POLYGON_PUBLIC_SCHEMA_V1_1,
    POLYGON_PUBLIC_SCHEMA_V1_4,
    SCHEMA_VERSION,
    is_supported_public_polygon_schema,
    schema_matches,
)
from osm_polygon_website_tag.contracts.text_schema import TEXT_COLUMN_NAMES, initial_text_fields
from osm_polygon_website_tag.pipeline.checkpoint_storage import Checkpoint, CheckpointStore
from osm_polygon_website_tag.pipeline.enrichment_checkpoint import enrichment_checkpoint_store
from osm_polygon_website_tag.runtime.run_state import hash_shard
from osm_polygon_website_tag.storage.atomic import atomic_promote_bundle
from osm_polygon_website_tag.web.text_cache import CachedText, TextCache
from osm_polygon_website_tag.web.text_extract import TextExtraction, extract_main_text
from osm_polygon_website_tag.web.web_fetch import FetchResult, fetch_html, normalize_http_url

DEFAULT_BATCH_ROWS = 512
# Fetching is I/O-bound; keep the pool bounded so one shard cannot create an
# unbounded number of sockets or put avoidable pressure on public websites.
DEFAULT_FETCH_WORKERS = 8
MAX_FETCH_WORKERS = 32
Fetcher = Callable[[str], FetchResult]
Extractor = Callable[..., TextExtraction]
_Reference = tuple[dict[str, object], str]
_PendingReferences = dict[str, list[_Reference]]


@dataclass(frozen=True)
class _RowState:
    """One input row plus its pre-enrichment values for change detection."""

    original: dict[str, object]
    row: dict[str, object]
    before: tuple[object, ...]


@dataclass(frozen=True)
class EnrichmentResult:
    """Outcome of enriching one polygon shard."""

    shard_path: Path
    row_count: int
    changed: bool
    shard_sha256: str
    max_batch_rows: int


@dataclass
class _EnrichmentContext:
    """Validated resources shared by one enrichment invocation."""

    shard: Path
    parquet: pq.ParquetFile
    source_schema: pa.Schema
    target_schema_version: str
    source_row_count: int
    store: CheckpointStore
    checkpoint: Checkpoint
    cache: TextCache
    staged: Path
    changed: bool
    next_part_index: int


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
    context = _prepare_enrichment_context(shard, cache_path, workers, batch_rows)
    try:
        changed_by_batches, max_batch_rows = _process_enrichment_batches(
            parquet=context.parquet,
            source_schema=context.source_schema,
            target_schema_version=context.target_schema_version,
            source_row_count=context.source_row_count,
            batch_rows=batch_rows,
            next_part_index=context.next_part_index,
            store=context.store,
            checkpoint=context.checkpoint,
            cache=context.cache,
            invocation_id=invocation_id,
            fetcher=fetcher,
            extractor=extractor,
            workers=workers,
        )
        context.changed = context.changed or changed_by_batches
        _promote_enriched_shard(
            shard=context.shard,
            staged=context.staged,
            store=context.store,
            checkpoint_directory=context.checkpoint.directory,
            changed=context.changed,
            batch_rows=batch_rows,
            source_row_count=context.source_row_count,
            max_batch_rows=max_batch_rows,
        )
        shutil.rmtree(context.checkpoint.directory)
    except BaseException:
        context.staged.unlink(missing_ok=True)
        raise
    finally:
        context.cache.close()
    return EnrichmentResult(
        shard_path=context.shard,
        row_count=context.source_row_count,
        changed=context.changed,
        shard_sha256=hash_shard(context.shard),
        max_batch_rows=max_batch_rows,
    )


def _validate_enrichment_settings(workers: int, batch_rows: int) -> None:
    """Validate bounded worker and batch settings before opening files."""
    if not 1 <= workers <= MAX_FETCH_WORKERS:
        raise ValueError(f"fetch_workers must be between 1 and {MAX_FETCH_WORKERS}")
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")


def _prepare_enrichment_context(
    shard: Path,
    cache_path: Path | str,
    workers: int,
    batch_rows: int,
) -> _EnrichmentContext:
    """Validate inputs and open the source-bound checkpoint/cache."""
    _validate_enrichment_settings(workers, batch_rows)
    parquet = pq.ParquetFile(shard)
    source_schema = parquet.schema_arrow
    source_row_count = parquet.metadata.num_rows
    if not is_supported_public_polygon_schema(source_schema):
        raise ValueError(f"unsupported polygon schema for enrichment: {shard.name}")
    target_schema, target_schema_version = _enrichment_contract(source_schema)
    store = enrichment_checkpoint_store(target_schema, target_schema_version)
    cache = TextCache(Path(cache_path))
    try:
        checkpoint = store.load(
            shard,
            source_row_count=source_row_count,
            source_shard_sha256=hash_shard(shard),
        )
    except BaseException:
        cache.close()
        raise
    staged = shard.with_name(f".{shard.name}.enriching")
    staged.unlink(missing_ok=True)
    return _EnrichmentContext(
        shard=shard,
        parquet=parquet,
        source_schema=source_schema,
        target_schema_version=target_schema_version,
        source_row_count=source_row_count,
        store=store,
        checkpoint=checkpoint,
        cache=cache,
        staged=staged,
        changed=not schema_matches(source_schema, target_schema) or bool(checkpoint.parts),
        next_part_index=len(checkpoint.parts),
    )


def _process_enrichment_batches(
    *,
    parquet: pq.ParquetFile,
    source_schema: pa.Schema,
    target_schema_version: str,
    source_row_count: int,
    batch_rows: int,
    next_part_index: int,
    store: CheckpointStore,
    checkpoint: Checkpoint,
    cache: TextCache,
    invocation_id: str,
    fetcher: Fetcher,
    extractor: Extractor,
    workers: int,
) -> tuple[bool, int]:
    """Process and checkpoint every unprocessed batch in source order."""
    changed = False
    processed_rows = checkpoint.completed_rows
    max_batch_rows = 0
    rows_to_skip = checkpoint.completed_rows
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="website-fetch") as fetch_pool:
        for batch in parquet.iter_batches(batch_size=batch_rows):
            originals, rows_to_skip = _skip_checkpointed_rows(batch.to_pylist(), rows_to_skip)
            if not originals:
                continue
            enriched_rows, batch_changed = _enrich_batch(
                originals,
                source_schema=source_schema,
                target_schema_version=target_schema_version,
                cache=cache,
                invocation_id=invocation_id,
                fetcher=fetcher,
                extractor=extractor,
                fetch_pool=fetch_pool,
            )
            changed = changed or batch_changed
            cache.flush()
            store.write_part(
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
    return changed, max_batch_rows


def _skip_checkpointed_rows(
    originals: list[dict[str, object]],
    rows_to_skip: int,
) -> tuple[list[dict[str, object]], int]:
    """Drop the durable prefix from one Arrow batch."""
    if rows_to_skip >= len(originals):
        return [], rows_to_skip - len(originals)
    if rows_to_skip:
        return originals[rows_to_skip:], 0
    return originals, 0


def _enrich_batch(
    originals: list[dict[str, object]],
    *,
    source_schema: pa.Schema,
    target_schema_version: str,
    cache: TextCache,
    invocation_id: str,
    fetcher: Fetcher,
    extractor: Extractor,
    fetch_pool: ThreadPoolExecutor,
) -> tuple[list[dict[str, object]], bool]:
    """Enrich one batch and return rows plus whether any values changed."""
    states, pending, lookup_urls = _prepare_batch(
        originals,
        source_schema=source_schema,
        invocation_id=invocation_id,
    )
    unresolved = _apply_cached_results(
        pending,
        lookup_urls,
        cache=cache,
        invocation_id=invocation_id,
    )
    _resolve_pending(
        unresolved,
        cache=cache,
        invocation_id=invocation_id,
        fetcher=fetcher,
        extractor=extractor,
        fetch_pool=fetch_pool,
    )
    return _finalize_batch(states, schema_version=target_schema_version), any(
        state.before != tuple(state.row.get(name) for name in TEXT_COLUMN_NAMES)
        or state.original.get("schema_version") != target_schema_version
        for state in states
    )


def _prepare_batch(
    originals: list[dict[str, object]],
    *,
    source_schema: pa.Schema,
    invocation_id: str,
) -> tuple[list[_RowState], _PendingReferences, set[str]]:
    """Prepare row states and normalized URL references for one batch."""
    states: list[_RowState] = []
    pending: _PendingReferences = {}
    lookup_urls: set[str] = set()
    is_legacy = schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA_V1_1)
    for original in originals:
        row = dict(original)
        if is_legacy:
            row.update(
                initial_text_fields(
                    website_present=row["website"] is not None,
                    contact_website_present=row["contact_website"] is not None,
                )
            )
        before = tuple(row.get(name) for name in TEXT_COLUMN_NAMES)
        for value_column, field_prefix in (
            ("website", "website"),
            ("contact_website", "contact_website"),
        ):
            _queue_tag(
                row,
                value_column=value_column,
                field_prefix=field_prefix,
                invocation_id=invocation_id,
                pending=pending,
                lookup_urls=lookup_urls,
            )
        states.append(_RowState(original, row, before))
    return states, pending, lookup_urls


def _apply_cached_results(
    pending: _PendingReferences,
    lookup_urls: set[str],
    *,
    cache: TextCache,
    invocation_id: str,
) -> _PendingReferences:
    """Apply reusable cache entries and return the remaining misses."""
    if not lookup_urls:
        return {}
    cached_by_url = cache.get_reusable_many(lookup_urls, invocation_id=invocation_id)
    unresolved: _PendingReferences = {}
    for url, references in pending.items():
        cached = cached_by_url.get(url)
        if cached is None:
            unresolved[url] = references
            continue
        for row, field_prefix in references:
            _apply_result(row, field_prefix, cached)
    return unresolved


def _finalize_batch(
    states: list[_RowState], *, schema_version: str = SCHEMA_VERSION
) -> list[dict[str, object]]:
    """Set the current schema marker and return mutable rows."""
    rows: list[dict[str, object]] = []
    for state in states:
        state.row["schema_version"] = schema_version
        rows.append(state.row)
    return rows


def _promote_enriched_shard(
    *,
    shard: Path,
    staged: Path,
    store: CheckpointStore,
    checkpoint_directory: Path,
    changed: bool,
    batch_rows: int,
    source_row_count: int,
    max_batch_rows: int,
) -> int:
    """Assemble checkpoint parts and atomically promote the enriched shard."""
    assembled_max_batch_rows = store.assemble(
        store.parts(checkpoint_directory),
        staged,
        batch_rows=batch_rows,
        row_count=source_row_count,
    )
    max_batch_rows = max(max_batch_rows, assembled_max_batch_rows)
    if not changed:
        staged.unlink(missing_ok=True)
        return max_batch_rows
    if not schema_matches(pq.read_schema(staged), store.schema):
        raise ValueError("enriched shard schema mismatch")
    atomic_promote_bundle([(staged, shard)])
    return max_batch_rows


def _enrichment_contract(source_schema: pa.Schema) -> tuple[pa.Schema, str]:
    """Keep language columns while migrating older public schemas to v1.3."""
    if schema_matches(source_schema, POLYGON_PUBLIC_SCHEMA_V1_4):
        return POLYGON_PUBLIC_SCHEMA_V1_4, LANGUAGE_SCHEMA_VERSION
    return POLYGON_PUBLIC_SCHEMA, SCHEMA_VERSION


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
    if not isinstance(value, str) or not value:
        _mark_absent(row, field_prefix)
        return
    if _has_complete_text(row, field_prefix):
        return
    try:
        normalized = normalize_http_url(value)
    except ValueError:
        _mark_invalid_url(row, field_prefix, value, invocation_id)
        return
    lookup_urls.add(normalized)
    pending.setdefault(normalized, []).append((row, field_prefix))


def _mark_absent(row: dict[str, object], field_prefix: str) -> None:
    """Mark a missing website field as absent."""
    row[f"{field_prefix}_text"] = None
    row[f"{field_prefix}_word_count"] = None
    row[f"{field_prefix}_text_status"] = "absent"


def _has_complete_text(row: dict[str, object], field_prefix: str) -> bool:
    """Return whether a row already has a successful text extraction."""
    return (
        row.get(f"{field_prefix}_text_status") == "success"
        and isinstance(row.get(f"{field_prefix}_text"), str)
        and isinstance(row.get(f"{field_prefix}_word_count"), int)
    )


def _mark_invalid_url(
    row: dict[str, object],
    field_prefix: str,
    value: str,
    invocation_id: str,
) -> None:
    """Apply the deterministic invalid-URL result without a network call."""
    _apply_result(
        row,
        field_prefix,
        CachedText(
            value, "invalid_url", None, None, None, "invalid_url", 0, "", None, invocation_id
        ),
    )


def _resolve_pending(
    pending: _PendingReferences,
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
    futures = _submit_fetches(pending, fetch_pool=fetch_pool, fetcher=fetcher)
    try:
        _record_fetches(
            pending,
            futures,
            cache=cache,
            invocation_id=invocation_id,
            extractor=extractor,
        )
    except KeyboardInterrupt:
        # Preserve every result that already completed while Ctrl-C was
        # delivered. Cancel queued work; running requests finish under the
        # executor's bounded shutdown, then their results are checkpointed.
        _drain_interrupted_fetches(
            pending,
            futures,
            cache=cache,
            invocation_id=invocation_id,
            extractor=extractor,
        )
        cache.flush()
        raise


def _submit_fetches(
    pending: _PendingReferences,
    *,
    fetch_pool: ThreadPoolExecutor,
    fetcher: Fetcher,
) -> dict[str, Future[FetchResult]]:
    """Submit normalized URL fetches in deterministic insertion order."""
    return {url: fetch_pool.submit(_fetch, url, fetcher=fetcher) for url in pending}


def _record_fetches(
    pending: _PendingReferences,
    futures: dict[str, Future[FetchResult]],
    *,
    cache: TextCache,
    invocation_id: str,
    extractor: Extractor,
) -> None:
    """Record completed fetches and apply them to all referencing rows."""
    for url, future in futures.items():
        _record_one_fetch(
            url,
            future,
            pending[url],
            cache=cache,
            invocation_id=invocation_id,
            extractor=extractor,
        )


def _record_one_fetch(
    url: str,
    future: Future[FetchResult],
    references: list[_Reference],
    *,
    cache: TextCache,
    invocation_id: str,
    extractor: Extractor,
) -> None:
    """Convert one future result to a cache entry and apply it."""
    cached = cache.record(
        _extract_fetched(url, future.result(), invocation_id=invocation_id, extractor=extractor),
        invocation_id=invocation_id,
    )
    for row, field_prefix in references:
        _apply_result(row, field_prefix, cached)


def _drain_interrupted_fetches(
    pending: _PendingReferences,
    futures: dict[str, Future[FetchResult]],
    *,
    cache: TextCache,
    invocation_id: str,
    extractor: Extractor,
) -> None:
    """Cancel queued futures and preserve any result already available."""
    for future in futures.values():
        future.cancel()
    for url, future in futures.items():
        if future.cancelled():
            continue
        fetched = _completed_fetch(future)
        if fetched is None:
            continue
        _record_fetched(
            url,
            fetched,
            pending[url],
            cache=cache,
            invocation_id=invocation_id,
            extractor=extractor,
        )


def _completed_fetch(future: Future[FetchResult]) -> FetchResult | None:
    """Return a completed fetch result, swallowing interrupted-worker errors."""
    try:
        return future.result()
    except BaseException:
        return None


def _record_fetched(
    url: str,
    fetched: FetchResult,
    references: list[_Reference],
    *,
    cache: TextCache,
    invocation_id: str,
    extractor: Extractor,
) -> None:
    """Record one fetched result and apply it to all referencing rows."""
    cached = cache.record(
        _extract_fetched(url, fetched, invocation_id=invocation_id, extractor=extractor),
        invocation_id=invocation_id,
    )
    for row, field_prefix in references:
        _apply_result(row, field_prefix, cached)


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

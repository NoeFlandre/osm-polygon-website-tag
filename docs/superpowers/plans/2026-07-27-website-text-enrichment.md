# Website Text Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add resumable, full-text Trafilatura enrichment for both website tags, incremental factual dataset-card updates, and minimum-work migration of already extracted/uploaded PBF shards.

**Architecture:** Keep extraction and HTTP enrichment separate. Extraction writes schema-v1.2 rows in `pending` state; a bounded enrichment engine resolves URLs through a persistent SQLite success cache, transactionally rewrites one shard, verifies it, recomputes the cumulative card from Parquets, and uploads the shard plus card. Legacy v1.1 shards migrate without reopening their PBF.

**Tech Stack:** Python 3.12, uv, PyArrow/Parquet, SQLite, Trafilatura 2.x, urllib3, DuckDB, pytest, Ruff, strict mypy.

---

## File map

- Create `text_schema.py`: enrichment columns, statuses, word-count contract, v1.1/v1.2 schema recognition.
- Create `web_fetch.py`: URL normalization, DNS/IP safety, bounded HTTP fetch and redirects.
- Create `text_extract.py`: tiny Trafilatura adapter operating on downloaded HTML.
- Create `text_cache.py`: persistent SQLite cache with per-invocation retry semantics.
- Create `enrich.py`: bounded per-shard migration/enrichment and atomic promotion.
- Modify `polygon_schema.py`, `extraction.py`: emit v1.2 pending fields for new shards.
- Modify `card_stats.py`, `card.py`: artifact-derived enrichment totals and in-progress card.
- Modify `run_state.py`, `workflow.py`, `publish.py`, `verify.py`, `finalize.py`: migration lifecycle, exact resume, upload checkpoint, verification.
- Modify `pyproject.toml`, `uv.lock`: locked Trafilatura runtime dependency.
- Add focused tests plus a legacy-run end-to-end acceptance test.

### Task 1: Freeze schema v1.2 and word-count semantics

**Files:**
- Create: `src/osm_polygon_website_tag/text_schema.py`
- Modify: `src/osm_polygon_website_tag/polygon_schema.py`
- Modify: `src/osm_polygon_website_tag/extraction.py`
- Test: `tests/test_text_schema.py`
- Test: `tests/test_polygon_schema.py`
- Test: `tests/test_extraction.py`

- [ ] Write RED tests asserting six appended fields, exact statuses, independent tag values, full untruncated text, and Unicode `\w+` counts.
- [ ] Run `uv run pytest tests/test_text_schema.py tests/test_polygon_schema.py tests/test_extraction.py -q`; confirm missing v1.2 API failures.
- [ ] Define:

```python
TEXT_STATUSES = frozenset(
    {
        "absent",
        "pending",
        "success",
        "empty",
        "invalid_url",
        "unsafe_url",
        "fetch_error",
        "extract_error",
    }
)


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))
```

- [ ] Preserve `POLYGON_PUBLIC_SCHEMA_V1_1`; define v1.2 by appending the six approved fields and set `SCHEMA_VERSION = "v1.2"`.
- [ ] Make extraction emit `absent` for absent tags and `pending` with null text/count for present tags.
- [ ] Run the focused tests and confirm GREEN.

### Task 2: Safe bounded downloader

**Files:**
- Create: `src/osm_polygon_website_tag/web_fetch.py`
- Test: `tests/test_web_fetch.py`

- [ ] Write RED tests for absolute, scheme-relative, and bare-host normalization; reject non-HTTP schemes, credentials, localhost, and non-global IPv4/IPv6; validate every redirect; enforce timeouts, redirect limit, and maximum response bytes.
- [ ] Run `uv run pytest tests/test_web_fetch.py -q`; confirm import/API failures.
- [ ] Implement immutable results:

```python
@dataclass(frozen=True)
class FetchResult:
    status: Literal["ok", "invalid_url", "unsafe_url", "fetch_error"]
    requested_url: str
    final_url: str | None
    body: bytes | None
    message: str | None
```

- [ ] Resolve hostnames before each request and accept only addresses for which `ipaddress.ip_address(value).is_global` is true. Disable automatic redirects and validate the next `Location` before following it.
- [ ] Stream at most 20 MB, use finite connect/read timeouts, accept HTML-like responses, and return sanitized structured failures.
- [ ] Run focused tests and confirm GREEN.

### Task 3: Trafilatura adapter and locked dependency

**Files:**
- Create: `src/osm_polygon_website_tag/text_extract.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_text_extract.py`

- [ ] Wait until the currently active production process has stopped before changing the environment or lockfile.
- [ ] Add RED tests using static HTML: main text extracted, comments excluded, source URL passed, empty result classified, extractor exceptions sanitized, and long text retained completely.
- [ ] Add `trafilatura>=2.1,<3` with `uv add 'trafilatura>=2.1,<3'`.
- [ ] Implement:

```python
def extract_main_text(html: bytes, *, url: str) -> TextExtraction:
    decoded = html.decode("utf-8", errors="replace")
    try:
        value = trafilatura.extract(
            decoded,
            url=url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
        )
    except Exception as exc:
        return TextExtraction("extract_error", None, None, sanitize(exc))
    if value is None or not value.strip():
        return TextExtraction("empty", "", 0, None)
    return TextExtraction("success", value, count_words(value), None)
```

- [ ] Run focused tests and confirm GREEN.

### Task 4: Persistent URL cache with retry-on-next-run

**Files:**
- Create: `src/osm_polygon_website_tag/text_cache.py`
- Test: `tests/test_text_cache.py`

- [ ] Write RED tests for success reuse, cross-tag URL deduplication, one failed attempt per invocation, failure retry under a new invocation ID, full text persistence, and rollback-safe transactions.
- [ ] Implement SQLite rows keyed by normalized URL with status, text, count, final URL, attempt count, timestamp, Trafilatura version, and invocation ID.
- [ ] Expose only `get_reusable(url, invocation_id)` and `record(result, invocation_id)`; successful rows are always reusable, failures only within their attempt invocation.
- [ ] Run focused tests and confirm GREEN.

### Task 5: Atomic bounded shard enrichment and legacy migration

**Files:**
- Create: `src/osm_polygon_website_tag/enrich.py`
- Modify: `src/osm_polygon_website_tag/run_state.py`
- Test: `tests/test_enrich.py`

- [ ] Write RED tests proving a v1.1 shard is enriched without calling `extract_pbf`, both tag URLs are processed, duplicate URLs fetch once, existing successes are reused, failures retry next invocation, batches remain bounded, and interruption preserves the prior shard.
- [ ] Stream Parquet record batches, resolve unique URLs through the cache, write v1.2 to a sibling staged file, validate schema/count, then atomically replace the shard.
- [ ] Update only `public_shard_sha256` and unchanged row count in `sources.json` after promotion; never mutate source fingerprints or other shard hashes.
- [ ] If no value/status changes, retain the existing shard bytes and hash.
- [ ] Run focused tests and confirm GREEN.

### Task 6: Artifact-derived incremental card

**Files:**
- Modify: `src/osm_polygon_website_tag/card_stats.py`
- Modify: `src/osm_polygon_website_tag/card.py`
- Test: `tests/test_card.py`

- [ ] Write RED tests for completed/expected source progress and every enrichment statistic, including independent website/contact totals, total words, at-least-one-success count, zero rows, and corrupt Parquet failure.
- [ ] Compute statistics by projecting only required columns from every current polygon shard; never use workflow counters or cache rows.
- [ ] Render `dataset_status: in_progress` until all expected shards are v1.2 and `complete` only after finalization. Document Trafilatura, status vocabulary, full-text policy, and exact word definition.
- [ ] Make `build_card` atomically promote both README and dataset YAML as a pair.
- [ ] Run focused tests and confirm GREEN.

### Task 7: Resumable lifecycle and incremental upload

**Files:**
- Modify: `src/osm_polygon_website_tag/run_state.py`
- Modify: `src/osm_polygon_website_tag/workflow.py`
- Modify: `src/osm_polygon_website_tag/publish.py`
- Test: `tests/test_run_state.py`
- Test: `tests/test_workflow.py`
- Test: `tests/test_publish.py`

- [ ] Write RED tests for new `enriching`/`enriched` transitions, reopening a verified v1.1 complete run solely for migration, skipping exact acknowledged v1.2 shards, enriching legacy shards without PBF reads, and re-uploading retry-improved shards only.
- [ ] Move per-source upload after enrichment. Build the partial card, verify shard/card, and call the HF uploader once with exactly:

```python
[polygon_shard, run_dir / "README.md", run_dir / "dataset.yaml"]
```

- [ ] Extend `uploaded_polygons.json` entries to bind polygon, README, and dataset-YAML hashes. Write the checkpoint only after the HF call returns.
- [ ] On completion, rebuild analysis/card, finalize, and run the existing receipt-bound upload.
- [ ] Run focused tests and confirm GREEN.

### Task 8: Verification, finalization, and acceptance

**Files:**
- Modify: `src/osm_polygon_website_tag/verify.py`
- Modify: `src/osm_polygon_website_tag/finalize.py`
- Modify: `tests/test_verify.py`
- Modify: `tests/test_finalize.py`
- Create: `tests/test_acceptance_text_enrichment.py`

- [ ] Write RED tests for exact v1.2 schema, tag/status/text/count invariants, word-count recomputation, missing enrichment, tampering, stale card, receipt mutation, legacy migration, interruption/resume, failure retry, and final remote allow-list.
- [ ] Add bounded DuckDB/PyArrow verification:
  - absent tag implies absent status and null text/count;
  - success implies non-null text and exact recomputed count;
  - empty implies empty text and zero;
  - failure/pending implies null text/count;
  - no `pending` remains in enriched/complete states.
- [ ] Ensure a complete receipt binds the cache metadata needed for reproducibility but never publishes raw HTML or secrets.
- [ ] Run acceptance and full verification.

### Task 9: Public documentation and final gates

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/data-and-remotes.md`
- Modify: `docs/setup.md`
- Modify: `AGENTS.md`

- [ ] Document full-text columns, two-tag behavior, retry semantics, safe-fetch constraints, cumulative card updates, storage on Seagate, and exact same-command resume.
- [ ] Search for stale v1.1-only claims and old upload ordering.
- [ ] Run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv build
git diff --check
```

- [ ] Stop for review. Do not crawl production URLs, rerun the production pipeline, commit/push, or publish during implementation verification.

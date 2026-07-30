# Website Text Enrichment Design

## Scope

Enrich every public polygon row with full main text extracted independently
from both non-empty `website` and `contact:website` values. Preserve the
existing one-Parquet-per-source layout, incremental Hugging Face uploads,
read-only PBF contract, and resumable `run-all` command.

No link crawling, sitemap discovery, page recursion, browser rendering,
language detection, or text truncation is included.

## Public schema

Schema `v1.2` appends these columns to the existing polygon schema:

- `website_text`: full Trafilatura plain-text output, nullable.
- `website_word_count`: exact word count for `website_text`, nullable.
- `website_text_status`: documented status, non-null.
- `contact_website_text`: full Trafilatura plain-text output, nullable.
- `contact_website_word_count`: exact word count, nullable.
- `contact_website_text_status`: documented status, non-null.

Word count is the number of Python Unicode `\w+` matches in the stored text.
It is zero for a successful extraction that returns an empty string and null
when no text result exists. The generated card documents this definition and
reports artifact-derived totals for each tag independently:

- URLs present;
- successful text extractions;
- failed or empty extractions;
- total extracted words;
- polygons with at least one successful extraction.

The status vocabulary is frozen and documented. At minimum it distinguishes
`absent`, `pending`, `success`, `empty`, `invalid_url`, `unsafe_url`,
`fetch_error`, and `extract_error`.

## Extraction and network safety

Trafilatura receives already downloaded HTML and extracts plain text with
comments disabled and the source URL supplied. Downloading is isolated behind
a small injectable fetch interface and has:

- HTTP/HTTPS-only URLs;
- normalization of scheme-relative and bare host values;
- bounded connect/read timeouts and response bytes;
- bounded redirects;
- validation of every redirect target;
- rejection of localhost, credentials in URLs, and private, loopback,
  link-local, multicast, reserved, or otherwise non-global resolved IPs;
- a fixed public user agent;
- no cookies, authentication, JavaScript, or proxy configuration.

OSM tag values are untrusted. Unsafe URLs are never requested.

## Persistent URL cache and retry semantics

One run-owned SQLite cache maps normalized URL to:

- status;
- extracted text and word count for successes;
- final URL;
- attempt count and last-attempt timestamp;
- Trafilatura version;
- invocation identifier.

The cache is bounded on disk, not in source-sized Python collections. The same
URL appearing in either tag or multiple polygons is downloaded once after a
success. A failed URL is attempted at most once per `run-all` invocation and
is retried on a later invocation. Successful cached text is never refetched
unless a future explicit refresh feature is designed.

## Per-source migration and transactional writes

The enrichment stage runs after polygon extraction and before per-source
publication:

1. Inspect the local polygon shard.
2. If it is legacy `v1.1`, stream it in bounded batches and append v1.2
   enrichment columns without rereading its PBF.
3. For v1.2 rows, reuse every successful result and process only `pending` or
   retryable failed values.
4. Write a complete staged Parquet and atomically replace the shard only after
   every row is materialized.
5. Update the source manifest count and output-shard SHA-256 atomically.
6. Verify the enriched schema, counts, invariants, and hash.

An interruption leaves the previous complete shard intact. A resume never
re-extracts a PBF merely because its polygon shard predates text enrichment.

## Incremental card and Hugging Face publication

After each source shard is enriched:

1. Recompute cumulative card statistics from all currently enriched polygon
   Parquets. No displayed number is sourced from mutable counters.
2. Render deterministic `README.md` and `dataset.yaml` with an explicit
   `in_progress` marker and `completed_sources / expected_sources`.
3. Upload the verified enriched polygon shard, README, and dataset metadata in
   the same Hugging Face commit.
4. Atomically checkpoint the acknowledged shard hash and card hashes.

The final analysis still runs once after all sources finish. It regenerates
the definitive complete card, verifies all artifacts, creates the completion
receipt, and performs the final receipt-bound upload.

If a previously acknowledged remote shard lacks v1.2 columns, its local v1.1
shard is enriched and only that shard plus regenerated card files are
re-uploaded. Completed PBF extraction is not repeated.

## Lifecycle and compatibility

`run-all` remains the single operational command. Its local source manifest,
remote-upload checkpoint, and public schema determine the minimum necessary
work:

- missing source bundle: extract, enrich, upload;
- valid v1.1 bundle: enrich, upload;
- v1.2 with retryable failures: retry failed URLs, rewrite only if results
  change, upload only if the shard hash changes;
- verified v1.2 with acknowledged matching hash: skip;
- final aggregate artifacts absent or stale: rebuild card/analysis/finalize.

Existing completed runs may be reopened only through a narrowly defined
enrichment migration transition; arbitrary mutation of complete artifacts
remains forbidden.

## Testing

All behavior is developed RED-GREEN with hermetic tests:

- Trafilatura extraction from static HTML;
- independent website/contact fields and word counts;
- full text is not truncated;
- URL normalization and SSRF/redirect rejection;
- timeouts, oversized responses, empty extraction, and structured failures;
- URL-cache deduplication and retry-on-next-invocation;
- bounded batch migration from v1.1 without calling PBF extraction;
- transactional rollback on enrichment failure;
- manifest hash update and mutation detection;
- partial card statistics derived from Parquets;
- per-PBF upload includes shard plus recomputed card;
- interruption and exact resume;
- end-to-end legacy-run migration and final verification.

All HTTP and Hugging Face calls are mocked in tests. Production crawling and
publication remain separate reviewed actions.

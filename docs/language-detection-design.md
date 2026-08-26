# GlotLID language detection

**Status:** Approved for implementation

## Goal

Add a resumable, stoppable language-detection stage for every successfully
extracted `website_text` and `contact_website_text` value. The stage uses the
GlotLID V3 FastText model and stores the result in the same public polygon row,
while preserving the existing text-fetching workflow when language detection
is not requested.

## Public data contract

Language detection introduces public schema version `v1.4`, extending the
current `v1.3` polygon row with four nullable fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `website_language` | nullable string | The exact GlotLID label, such as `eng_Latn`. |
| `website_language_probability` | nullable float64 | GlotLID's top-1 probability for that label. |
| `contact_website_language` | nullable string | The exact GlotLID label for `contact_website_text`. |
| `contact_website_language_probability` | nullable float64 | GlotLID's top-1 probability for that label. |

The raw script-aware model label is retained; the pipeline does not collapse
it to a two-letter language code. Both fields for a text source are null when
that source has no successful extracted text. Successful text receives one
top-1 label and one probability; no arbitrary confidence threshold is applied.

Existing `v1.1`, `v1.2`, and `v1.3` shards remain readable and accepted as
inputs for migration. A language run upgrades public shards atomically to
`v1.4`; the normal `run-all` workflow remains unchanged unless its explicit
language-detection option is used.

## Execution model

The implementation has one focused model adapter and one focused shard
pipeline:

1. `detect-languages --run-dir <run>` discovers public polygon shards and
   validates that their text statuses are terminal before starting a shard.
2. `run-all --detect-languages` invokes the same stage after text extraction
   and before incremental publication.
3. A single process loads one GlotLID model and predicts a bounded list of
   text values at a time. Language prediction is serial and deterministic;
   there is no model copy per worker.
4. Text sources are detected independently, so a row with both website fields
   produces two predictions and a row with one field produces one.
5. A completed shard updates its source manifest hash and remains ready for the
   existing analysis, card, verification, and publication phases.

The default `run-all` path does not download or load a language model. This
keeps existing extraction-only runs compatible and makes the new model cost an
explicit operator choice.

## Model and storage boundary

GlotLID is loaded through `fasttext` and `huggingface_hub`, using only the
versioned `model_v3.bin` file from `cis-lmu/glotlid` at Hub revision
`85cd671`. The resolved model file is hashed once and that hash, repository,
filename, and revision are recorded in the language checkpoint metadata.

Production model-cache and run paths are on the mounted Seagate volume:

```text
/Volumes/Seagate M3/projects/osm-polygon-website-tag-data/models/glotlid/
/Volumes/Seagate M3/projects/osm-polygon-website-tag-data/runs/<run-id>/
```

The CLI supplies the Seagate model-cache default explicitly to
`hf_hub_download`, so Hugging Face's default Mac home cache is not used for
the production model. Production language commands reject a model cache or
run path outside the mounted Seagate volume. Tests may use `tmp_path` and an
injected fake detector; they never download the model.

The model binary is local operational state. It is not copied into Git, the
run receipt, or the public dataset.

## Resume and interruption contract

Each public shard has a language checkpoint directory containing:

- source row count and source-shard SHA-256;
- target schema version;
- model repository, filename, Hub revision, and model SHA-256;
- sequential, atomically promoted Parquet parts containing completed rows.

The pipeline reads source rows in bounded batches. After each batch it flushes
the checkpoint part before continuing. The source shard is not replaced until
all source rows have been detected, the assembled output has the expected row
count and exact `v1.4` schema, and the staged file is atomically promoted.

`Ctrl-C` and ordinary exceptions leave the original shard valid and retain
completed checkpoint parts. A rerun verifies the source and model identity,
skips the durable prefix, and continues at the first unfinished batch. A
changed source or model fails closed instead of mixing predictions from
different inputs. Known temporary files are cleaned without deleting unrelated
run artifacts.

## Testing and quality gates

Implementation follows a visible RED → GREEN → REFACTOR cycle for each
behavior:

- exact schema and nullability contract;
- GlotLID label/probability conversion;
- independent detection of both website text fields;
- model-path and Seagate-path safety;
- source/model-bound checkpoint validation;
- atomic completion and row-order preservation;
- interruption and resume without reprocessing completed batches;
- CLI/workflow integration without changing the default path.

The tests inject a small fake detector and use `tmp_path`; no network, model
download, or production disk is used. Before handoff, `just check`,
`just pre-commit`, `just pre-push`, `just crap`, and `just mutation` must pass,
with the CRAP report below 6 and no surviving, unverified, timed-out, or
interrupted mutants.

## Non-goals

- No language translation or normalization to a single ISO-639 representation.
- No HTML `<html lang>` parsing or domain/region heuristic fallback.
- No model fine-tuning or remote inference endpoint.
- No changes to URL safety, fetching, Trafilatura extraction, or text-cache
  semantics.

# GlotLID language detection

**Status:** Implemented (public schema v1.4). This page is the design record. The README and `docs/operations.md` describe the later sentence stage (v1.5).

## Goal

Add a language-detection stage that you can stop and resume. The stage processes each successfully extracted `website_text` and `contact_website_text` value. It uses the GlotLID V3 FastText model. It stores the result in the same public polygon row. When you do not request language detection, the existing text-fetching workflow does not change.

## Public data contract

Language detection introduces public schema version `v1.4`. It extends the current `v1.3` polygon row with four nullable fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `website_language` | nullable string | The exact GlotLID label, such as `eng_Latn`. |
| `website_language_probability` | nullable float64 | The top-1 probability of GlotLID for that label. |
| `contact_website_language` | nullable string | The exact GlotLID label for `contact_website_text`. |
| `contact_website_language_probability` | nullable float64 | The top-1 probability of GlotLID for that label. |

The pipeline keeps the raw script-aware model label. It does not reduce it to a two-letter language code. Both fields for a text source are null when that source has no successful extracted text. Successful text gets one top-1 label and one probability. The pipeline applies no arbitrary confidence threshold.

The pipeline still reads and accepts the existing `v1.1`, `v1.2`, and `v1.3` shards as inputs for migration. A language run upgrades the public shards to `v1.4` in an atomic step. The normal `run-all` workflow does not change, unless you use its explicit language-detection option.

## Execution model

The implementation has one focused model adapter and one focused shard pipeline:

1. `detect-languages --run-dir <run>` discovers the public polygon shards. Before it starts a shard, it validates that the text statuses are known and resolved. It detects the language of successful text. A resolved outcome that is not a success stays null, because there is no text to detect.
2. `run-all --detect-languages` calls the same stage after text extraction and before incremental publication.
3. A single process loads one GlotLID model. It predicts a bounded list of text values at one time. The language prediction is serial and deterministic. There is no model copy for each worker.
4. The stage detects the text sources independently. A row with both website fields produces two predictions. A row with one field produces one prediction.
5. A completed shard updates its source manifest hash. It stays ready for the existing analysis, card, verification, and publication phases.

The default `run-all` path does not download or load a language model. This keeps existing extraction-only runs compatible. The operator decides explicitly to pay the cost of the new model.

## Model and storage boundary

The stage loads GlotLID through `fasttext` and `huggingface_hub`. It uses only the versioned `model_v3.bin` file from `cis-lmu/glotlid` at Hub revision `85cd671`. The stage hashes the resolved model file one time. It records that hash, the repository, the filename, and the revision in the metadata of the language checkpoint.

The production model-cache path and the run paths are on the mounted Seagate volume:

```text
/Volumes/Seagate M3/projects/osm-polygon-website-tag/models/glotlid/
/Volumes/Seagate M3/projects/osm-polygon-website-tag/runs/<run-id>/
```

The CLI supplies the Seagate model-cache default explicitly to `hf_hub_download`. So the production model does not use the default Mac home cache of Hugging Face. The production language commands reject a model cache or a run path outside the mounted Seagate volume. The tests can use `tmp_path` and an injected fake detector. They never download the model.

The model binary is local operational state. Nobody copies it into Git, into the run receipt, or into the public dataset.

## Resume and interruption contract

Each public shard has a language checkpoint directory. It contains these items:

- The source row count and the source-shard SHA-256.
- The target schema version.
- The model repository, the filename, the Hub revision, and the model SHA-256.
- Sequential Parquet parts that contain completed rows. The pipeline promotes each part in an atomic step.

The pipeline reads the source rows in bounded batches. After each batch, it flushes the checkpoint part before it continues. The pipeline does not replace the source shard until these conditions are true: it detected all source rows, the assembled output has the expected row count and the exact `v1.4` schema, and it promoted the staged file in an atomic step.

`Ctrl-C` and ordinary exceptions leave the original shard valid. They keep the completed checkpoint parts. When you run the command again, it verifies the identity of the source and of the model. It skips the durable prefix. It continues at the first unfinished batch. A changed source or a changed model fails closed. The pipeline does not mix predictions from different inputs. The pipeline cleans known temporary files. It does not delete unrelated run artifacts.

## Tests and quality gates

The implementation follows a visible RED, GREEN, REFACTOR cycle for each behavior:

- The exact schema and nullability contract.
- The conversion of the GlotLID label and probability.
- The independent detection of both website text fields.
- The safety of the model path and the Seagate path.
- The validation of checkpoints that are bound to the source and to the model.
- The atomic completion and the preservation of the row order.
- The interruption and the resume without reprocessing of completed batches.
- The CLI and workflow integration without a change to the default path.

The tests inject a small fake detector and use `tmp_path`. They use no network, no model download, and no production disk. Before the handoff, `just check`, `just pre-commit`, `just pre-push`, `just crap`, and `just mutation` must pass. The CRAP report must be below 6. No mutant can be surviving, unverified, timed out, or interrupted.

## Non-goals

- No language translation. No normalization to a single ISO-639 representation.
- No parsing of the HTML `<html lang>` attribute. No fallback with domain or region heuristics.
- No model fine-tuning. No remote inference endpoint.
- No change to URL safety, fetching, Trafilatura extraction, or the semantics of the text cache.

# G1 Safety Corrections Design

## Goal

Make the synthetic-only pipeline safe to review before any canary, production
PBF access, Git operation, network call, or Hugging Face publication.

## Architecture

Extraction uses bounded Arrow batches and a run-owned SQLite candidate ledger.
All three per-source shards are built in a transaction directory, source
identity is checked before and after reading, and a recoverable bundle
promotion updates final paths only after every output is complete.

Analysis is performed by DuckDB under explicit memory and spill limits. Large
results are written directly to temporary Parquet files and promoted as a
complete analysis bundle; Python only receives bounded scalar and top-K
results.

Run state owns the exact expected-source inventory and legal phase transitions.
The CLI delegates to phase functions and never reconstructs or overwrites their
results. Credentials come only from the Hugging Face token provider and never
from command-line arguments.

Verification checks exact schemas, counts, hashes, source inventory, row
invariants, analysis reconciliation, generated metadata, and the deterministic
completion receipt. Publication planning is local and receipt-driven; remote
creation and upload remain explicit approval-gated operations.

## Data Contract

The public dataset contains one row per successfully assembled closed way or
supported polygon relation having a non-empty `website` or
`contact:website`. `wikidata` is comparison-only. Each original PBF produces
exactly one public Parquet, including a schema-valid zero-row file.

Analysis uses all eight boolean cells formed by website presence,
contact:website presence, and Wikidata presence. Observation and canonical
levels are named explicitly and all cells are emitted even when zero.

## Failure Safety

Source mutation, writer failure, promotion failure, invalid lifecycle state,
schema mismatch, corrupt input artifact, or arithmetic disagreement fails
closed. Prior finalized artifacts remain unchanged. Cleanup is restricted to
known run-owned transaction paths and does not recursively erase diagnostic
state.

## Test Strategy

Every correction begins with a focused regression test observed failing for
the intended reason. Synthetic PBF fixtures exercise exact cells, duplicates,
conflicts, relations with holes, geometry rejection, empty sources, hostname
normalization, and artifact mutation. Full lint, type, test, coverage, and
build gates are rerun before readiness is claimed.

## Operational Gates

This phase does not access production PBFs, run a canary, stage, commit, push,
create a remote repository, use the network, or publish. Those actions require
fresh review after implementation and verification evidence.

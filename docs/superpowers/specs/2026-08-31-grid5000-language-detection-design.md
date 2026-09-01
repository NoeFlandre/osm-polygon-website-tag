# Grid5000 Language Detection Design

**Date:** 2026-08-31

**Status:** Approved for implementation

## Goal

Run the existing GlotLID language stage on Grid'5000 in short, policy-aware
OAR jobs while keeping Seagate as the canonical store for the model, dataset,
checkpoints, provenance, and final artifacts.

## Constraints and policy boundaries

- Grid'5000 frontends are used only for checkout, file transfer, submission,
  and monitoring. Detection runs only on a reserved node.
- Each job requests one host, one GPU, and two CPU cores for `0:30`; GlotLID is
  a FastText CPU model. The operational wrapper requests one GPU per short job
  to support isolated staged workers; this is a resource contract, not a claim
  that the FastText inference itself uses the GPU.
- Detection receives a 25-minute compute budget. It stops between complete
  Parquet batches, leaving five minutes of walltime margin for job cleanup and
  result synchronization.
- A job never downloads Hugging Face weights or fetches website URLs. The
  pinned model binary and the selected Parquet shard are staged into the job
  bundle before submission.
- The job wrapper runs `usagepolicycheck -t` before and after submission,
  refuses a previously active job marker, and records the OAR job ID.
- Grid'5000 working storage is temporary operational state. The Seagate run
  directory is the source of truth; synchronization verifies source/model
  identities before installing a completed shard or checkpoint prefix.
- Credentials are never put in scripts, manifests, command-line arguments,
  or logs. The public model can be downloaded with the local Hugging Face
  credential store or an environment variable without printing its value.

## Architecture

The existing `detect_language_shard` stage gains an optional monotonic time
budget. A budget exhaustion is a normal paused outcome, not an error: the
original shard remains valid, the last atomically written checkpoint part is
durable, and a later invocation resumes from that exact prefix. Unlimited
local runs retain their current behavior.

The new `pipeline/grid5000.py` module owns a small, validated bundle
contract. `prepare` selects one unfinished public shard, copies it and its
existing language checkpoint parts plus the verified `model_v3.bin` into a
Seagate bundle, and records source/model hashes, row count, repository commit,
and job configuration. The reserved-node runner loads the staged binary
without network access and writes a result receipt. `sync` verifies the
receipt and copies either the checkpoint prefix or the completed v1.4 shard
back to the Seagate run, updating the ordinary run manifest only after
validation.

Thin shell scripts handle Grid'5000-specific OAR submission and environment
setup. They do not implement data semantics. Site, queue, remote paths, and
resource properties remain explicit environment variables because they vary
across Grid'5000 sites and user groups.

## Data flow

```text
Seagate run + Seagate model
        │ prepare one shard and write manifest
        ▼
Seagate job bundle ── rsync ──> Grid'5000 job directory
                                      │ OAR reserved node, offline
                                      ▼
                             checkpoint or v1.4 shard
                                      │ rsync + identity verification
                                      ▼
                              Seagate run and history
```

Only one bundle is active at a time. A completed bundle is not submitted
again. A paused bundle can be prepared again from the synchronized Seagate
checkpoint and receives a new OAR job ID.

## Testing and quality

Pure functions and bundle boundaries are tested with `tmp_path` and fake
detectors/clocks; no test contacts Grid'5000, Hugging Face, or a real website.
Tests cover time-budget red/green behavior, atomic pause semantics, model and
source identity mismatches, malformed manifests/results, checkpoint sync,
completed-shard sync, duplicate-job refusal, and policy-command ordering.
The existing public CLI and unlimited local detection path remain covered by
their current tests. The final validation runs the repository's check,
pre-commit, pre-push, CRAP, and mutation gates; generated reports remain
outside Git unless they are explicitly part of the code change.

## External references

- Grid'5000 Usage Policy:
  https://www.grid5000.fr/w/Grid5000:UsagePolicy
- Grid'5000 Getting Started:
  https://www.grid5000.fr/w/Getting_Started
- Grid'5000 Storage:
  https://www.grid5000.fr/w/Storage
- GlotLID:
  https://huggingface.co/cis-lmu/glotlid

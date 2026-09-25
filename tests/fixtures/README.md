# Test fixtures

This package contains small, deterministic fixtures shared across test suites.
The polygon-shard helpers model the pre-v1.3 public schemas used by migration
and enrichment tests; they never read production PBFs or network resources.

`wtpsplit_languages.txt` snapshots the language codes the pinned wtpsplit
segmenter supports; regenerate it with `scripts/quality/wtpsplit_languages.py`
when the pin changes.

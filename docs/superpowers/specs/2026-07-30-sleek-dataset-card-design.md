# Sleek Public Dataset Card Design

## Goal

Generate a concise, polished Hugging Face dataset card whose metadata validates
without warnings and whose factual statistics are recomputed exclusively from
the current published Parquets.

## Hugging Face metadata

Remove `task_categories` from YAML front matter. The dataset is geographic
source data rather than an official Hugging Face machine-learning task, so
omitting the optional field is more accurate than using the generic `other`
category. Retain `license: odbl`, the dataset configuration, size category,
geographic tags, progress fields, and automatically derived text totals.

## Card structure

The generated README contains:

1. A one-paragraph purpose and exact inclusion rule.
2. A compact status table with:
   - dataset state;
   - processed/expected PBFs;
   - public polygon count;
   - canonical polygon count;
   - duplicate, conflict, and rejection counts.
3. A compact website-text table with separate `website` and
   `contact:website` rows:
   - URLs present;
   - successful, empty, and failed extractions;
   - extracted word count.
   It also reports polygons with at least one extracted text and the combined
   word total.
4. The top ten hostnames for each website key.
5. A concise public-column schema table generated from the current Arrow
   schema and column documentation.
6. Short methodology, data-quality, provenance, license, and attribution
   sections.

The verbose eight-cell cube and per-source coverage tables are omitted from the
README because their complete factual results remain published in
`analysis/*.parquet`.

## Factuality and update behavior

`build_card` continues to call `compute_card_stats`; rendering receives only
that immutable result. No numbers are hard-coded or copied from run metadata.
Every incremental per-PBF upload rebuilds `README.md` and `dataset.yaml` from
the current Parquets before upload. Final publication does the same through the
existing workflow.

## Testing and safety

RED-to-GREEN tests assert:

- generated YAML contains no `task_categories`;
- core progress, polygon, URL, extraction, failure, and word statistics render
  from synthetic Parquets;
- combined words equal the two automatically computed totals;
- hostname tables are capped at ten entries;
- the detailed analysis tables are absent from the README but remain unchanged
  on disk;
- schema, ODbL license, OSM attribution, Geofabrik attribution, and
  reproducibility language remain present;
- card generation remains deterministic and idempotent.

Ruff, Ruff formatting, ty, pytest, pre-commit, package build, fresh-wheel smoke,
and GitHub Actions must pass. The currently running process uses the old code
and must exit before the user resumes with the new card generator.

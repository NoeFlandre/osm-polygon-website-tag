# Glossary

This page defines the technical names that the documentation uses. Each name has one meaning.

| Term | Definition |
| --- | --- |
| OSM | OpenStreetMap, the source of the map data. |
| PBF | A compressed file of OSM data. The pipeline reads PBF files and does not change them. |
| Polygon | A closed area from a closed way or a supported polygon relation. |
| Shard | One Parquet file. It holds the polygons of one source PBF. |
| Run | One execution of the pipeline. A run writes to `<output-root>/<run-id>/`. |
| Run ID | The name of a run (`--run-id`). |
| Source root | The read-only directory that holds the PBF files (`--source-root`). |
| Output root | The writable directory that holds the runs (`--output-root`). |
| Data root | The directory that `OSM_POLY_DATA_DIR` names. It holds runs, model caches and Grid'5000 bundles. |
| Resume | To start a stopped run again with the same command. The run continues from the verified work. |
| Dataset card | The generated README of the published dataset. It is the source of truth for the totals. |
| Schema | The versioned list of columns of the polygon shards (`v1.3`, `v1.4`, `v1.5`). |
| Language detection | An opt-in stage. It adds GlotLID labels and probabilities (schema `v1.4`). |
| Sentence segmentation | An opt-in stage. It adds SaT sentences, counts and status (schema `v1.5`). |
| Publish | To upload a verified run to the Hugging Face dataset repository. |
| Gate | A check that must pass before a change is accepted. |

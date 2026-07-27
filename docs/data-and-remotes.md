# Data and remotes

Where data lives on disk, and how it gets to Hugging Face.

## Local data root

The default data root is `/Volumes/Seagate M3/projects/osm-polygon-website-tag`,
an external Seagate M3 drive. Override with `OSM_POLY_DATA_DIR`.

Under the root, three sub-directories are managed by `osm_polygon_website_tag.paths`:

| Sub-directory | Owner                                  | Contents                              |
| ------------- | -------------------------------------- | ------------------------------------- |
| `raw/`        | `paths.raw_dir()`                      | Immutable OSM extracts (PBF, Overpass JSON dumps). Never modified after write. |
| `processed/`  | `paths.processed_dir()`                | Cleaned, normalized intermediate artifacts. |
| `exports/`    | `paths.exports_dir()`                  | Final artifacts ready for HF upload. |

Nothing in `data/` is committed to git (see `.gitignore`).

## GitHub remote

```
https://github.com/NoeFlandre/osm-polygon-website-tag.git
```

Push flow (typical):

```bash
git status
git diff --staged   # review before committing
git add <files>
git commit -m "<message>"
git push origin main
```

## Hugging Face dataset remote

```
https://huggingface.co/datasets/NoeFlandre/osm-polygon-website-tag
```

Use the `hf` CLI (not the Python SDK) for uploads, per the project's chosen tooling:

```bash
# One-time
brew install hf
hf auth login                                # paste a write token from
                                             # https://huggingface.co/settings/tokens

# After artifacts are produced in $OSM_POLY_DATA_DIR/exports
hf upload NoeFlandre/osm-polygon-website-tag . --repo-type=dataset \
    $OSM_POLY_DATA_DIR/exports
```

A convenience wrapper lives at `scripts/upload_to_hf.sh` and reads the
destination from the same environment variables documented in `.env.example`.

## Why split code and data

- **Code in git, data on disk** keeps the repo cloneable and lightweight.
- The external drive provides the storage needed for planet-scale OSM data,
  which would otherwise bloat history.
- Treating `raw/` as immutable mirrors the way OSM providers serve data: any
  transformation produces a new artifact under `processed/` or `exports/`.

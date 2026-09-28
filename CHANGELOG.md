# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is
defined once, in `pyproject.toml`; `CITATION.cff` and the README's BibTeX must
match it.

## [Unreleased]

### Added

- `--version`, `-v`/`-vv` and `-q` global options (#69).
- Distinct exit codes (3 invalid input, 4 remote, 130 Ctrl-C), one-line
  errors, and `--debug` / `OSM_PWT_DEBUG=1` for tracebacks (#68).
- `CONTRIBUTING.md`, `SECURITY.md`, issue forms and a pull request template (#88).
- This changelog and a tag-driven release workflow (#87).

### Changed

- Pages are decoded with the HTTP charset, then the HTML `<meta>` prescan, as
  browsers do, instead of always as UTF-8 (#83).
- The data root comes from `OSM_POLY_DATA_DIR` (environment or `.env`) and
  defaults to `./data`; no machine-specific paths remain (#81, #79, #80).
- The User-Agent carries the full package version.
- Trafilatura loads on first extraction, not at import: every command starts
  about 2 s faster (#95).
- `publish-trackio` without the `trackio` package exits 5 with one clear line.

## [0.1.0]

First published snapshot of the dataset and pipeline.

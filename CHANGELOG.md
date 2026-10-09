# Changelog

This file records all notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version is defined one time, in `pyproject.toml`. The version in `CITATION.cff` and in the BibTeX of the README must be the same.

## [Unreleased]

- Read SaT model capabilities from a versioned, digest-checked offline reference.
  Preserve sentence-routing policy, runtime model pins, and historical fingerprints.

### Added

- The global options `--version`, `-v`/`-vv` and `-q` (#69).
- Different exit codes (3 for invalid input, 4 for remote, 130 for Ctrl-C), one-line errors, and `--debug` / `OSM_PWT_DEBUG=1` to show tracebacks (#68).
- `CONTRIBUTING.md`, `SECURITY.md`, issue forms and a pull request template (#88).
- This changelog and a release workflow that a tag starts (#87).
- CI: `ci-ok` is the one check to require. Pull requests build the docs and audit `uv.lock` with `pip-audit` (`just audit`). Each job has a timeout. The mutation shards start in parallel with the gate and run on pull requests only (#74, #75).

### Changed

- The pipeline decodes pages with the HTTP charset, then with the HTML `<meta>` prescan, as browsers do. Before, it always used UTF-8 (#83).
- The data root comes from `OSM_POLY_DATA_DIR` (environment or `.env`). The default is `./data`. No machine-specific paths remain (#81, #79, #80).
- The User-Agent contains the full package version.
- Trafilatura loads at the first extraction, not at import. Each command starts about 2 s faster (#95).
- The fetcher refuses these responses from the headers alone, and it does not download the body: a redirect, an error page, an unsupported media type, or a declared `Content-Length` above the limit. The fetcher matches media types exactly (#84).
- Politeness for each host: `--host-concurrency` (default 2) and `--host-delay-seconds` (default 0.2) limit and space the requests to one website. The fetcher obeys a short `Retry-After` on 429/503 and retries one time (#85).
- A charset label with a NUL byte does not stop the extraction. The pipeline rejects a host that has only dots as `missing_hostname`. The new property tests found both problems (#89).
- `publish-trackio` without the `trackio` package exits with code 5 and one clear line.
- `--time-budget-seconds` and `--batch-rows` have one declaration per type. Every command that takes one shows the same help text. The accepted types do not change.

## [0.1.0]

This is the first published snapshot of the dataset and the pipeline.

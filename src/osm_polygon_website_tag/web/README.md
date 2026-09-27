# Web

Owns safe HTTP retrieval, Trafilatura adaptation, and persistent text caching.

- Modules: `web_fetch`, `content_type`, `encoding_labels`, `text_extract`, `text_cache`.
- Dependencies: `contracts` only.
- Entry points: URL normalization, bounded fetch, main-text extraction, `TextCache`.
- Excludes: OSM classification, reporting, publication, and orchestration.

`text_extract` resolves the installed Trafilatura version lazily and caches it
for the process. Every extraction result still records the exact installed
version, but repeated URLs do not rescan package metadata. It also reuses a
thread-local Trafilatura options object while updating the current URL, so
configuration setup is amortized without sharing mutable parser state across
threads.

`content_type` parses `Content-Type` values (HTTP headers and `<meta content>`)
one parameter at a time, consuming quoted values whole. `encoding_labels`
holds the WHATWG label table, so `text_extract` reads a declared charset the
way browsers do.

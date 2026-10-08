# Web

Owns safe HTTP retrieval, Trafilatura adaptation, and persistent text caching.

`web_fetch` checks and caches each origin's `robots.txt` before requesting a
page, applies its `Crawl-delay`, and uses the same SSRF-safe transport for both
requests. The cache is a `RobotsCache` object. `fetch_html` takes one through
`robots_cache=`; without it, the module's shared instance is used. A cache can
bound its origins (`max_entries`) and expire failed robots fetches
(`error_ttl_seconds`). Previously cached successful text remains reusable and is
not retroactively removed.

- Modules: `web_fetch`, `content_type`, `encoding_labels`, `text_extract`, `text_cache`.
- Dependencies: `contracts` only.
- Entry points: URL normalization, bounded fetch, `RobotsCache`, main-text extraction, `TextCache`.
- Excludes: OSM classification, reporting, publication, and orchestration.

`web_fetch` is this repository's reference implementation for safe website
retrieval. A reusable shared package remains a separate follow-up because it
would need coordinated changes in sibling repositories.

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

# Geographic reporting

This package derives a deterministic H3 density summary from public polygon
centroids and renders the dataset-card map. The text-only dataset-card map
counts one globally unique OSM polygon per ``(osm_type, osm_id)`` with a
successful, trimmed non-empty ``website_text`` or ``contact_website_text``.
It reads only finalized Parquet
artifacts; it does not read PBFs, fetch websites, or publish remotely.

The default summary uses ``aggregation_mode="regional_rows"`` and includes
every public polygon observation. Use
``aggregation_mode="global_unique_text"`` (or the compatibility alias
``extracted_text_only=True``) for the unique, non-empty-text definition;
shards without the required identity, text, or status columns are excluded
because they cannot prove that a qualifying polygon exists. The dataset-card
map uses global mode. Regional duplicate rows remain separately reportable,
but never inflate the global map total or its caption, which explicitly says
that regional overlap duplicates were removed globally.

The public entry point is `build_polygon_density_map`. H3 resolution 3 and the
canonical `assets/geographic_polygon_density.png` path are defined in `layout`.
The renderer uses the bundled Natural Earth 1:110m Admin-0 country GeoJSON for
the same neutral land backdrop as the wikidata-only reference. Rendering is
offline and does not download basemap data.

# Geographic reporting

This package derives a deterministic H3 density summary from public polygon
centroids and renders the dataset-card map. It reads only finalized Parquet
artifacts; it does not read PBFs, fetch websites, or publish remotely.

The public entry point is `build_polygon_density_map`. H3 resolution 3 and the
canonical `assets/geographic_polygon_density.png` path are defined in `layout`.
The renderer uses the bundled Natural Earth 1:110m Admin-0 country GeoJSON for
the same neutral land backdrop as the wikidata-only reference. Rendering is
offline and does not download basemap data.

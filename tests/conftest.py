"""Pytest fixtures for synthetic OSM fixtures."""

from __future__ import annotations

from pathlib import Path

import osmium
import osmium.osm
import pytest


@pytest.fixture
def make_pbf(tmp_path: Path):
    """Write synthetic OSM XML as a ``.osm.pbf`` file.

    Returns a function that creates a new directory containing the
    trans-coded PBF.
    """
    counter = {"i": 0}

    class _ForwardHandler(osmium.SimpleHandler):
        def __init__(self, writer: osmium.SimpleWriter) -> None:
            super().__init__()
            self._writer = writer

        def node(self, n: osmium.osm.Node) -> None:
            self._writer.add_node(n)

        def way(self, w: osmium.osm.Way) -> None:
            self._writer.add_way(w)

        def relation(self, r: osmium.osm.Relation) -> None:
            self._writer.add_relation(r)

    def _make(xml: str, *, name: str = "monaco-latest.osm.pbf") -> Path:
        counter["i"] += 1
        src_dir = tmp_path / f"src_{counter['i']:03d}"
        src_dir.mkdir()
        osm_path = src_dir / "intermediate.osm"
        osm_path.write_text(xml, encoding="utf-8")
        pbf_path = src_dir / name
        writer = osmium.SimpleWriter(str(pbf_path), overwrite=True)
        try:
            _ForwardHandler(writer).apply_file(str(osm_path))
        finally:
            writer.close()
        osm_path.unlink()
        return src_dir

    return _make

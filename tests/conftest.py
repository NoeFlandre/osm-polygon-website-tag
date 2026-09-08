"""Pytest fixtures for synthetic OSM fixtures."""

from __future__ import annotations

from pathlib import Path

import osmium
import osmium.osm
import pytest

from osm_polygon_website_tag.application import source_processing as source_processing_module
from osm_polygon_website_tag.publishing import incremental as incremental_module
from osm_polygon_website_tag.publishing import publish as publish_module

_UPLOAD_ATTEMPT = (
    "a test tried to reach Hugging Face; publication is an explicit, "
    "human-approved operation and must be stubbed in tests"
)


@pytest.fixture(autouse=True)
def _never_reach_hugging_face(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed instead of touching the real dataset repository.

    An apply-mode publish test once relied on no credential being resolvable;
    when one became resolvable it uploaded fixture artifacts over the published
    dataset. A test that means to exercise upload must stub these itself.
    """
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(_UPLOAD_ATTEMPT)

    # Each module binds the uploader at import time, so every binding is closed.
    for module in (publish_module, incremental_module, source_processing_module):
        monkeypatch.setattr(module, "_upload_folder", refuse)


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

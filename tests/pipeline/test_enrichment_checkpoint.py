"""Architecture contract for durable enrichment checkpoints."""

from __future__ import annotations

import importlib


def test_enrichment_checkpoint_module_exposes_focused_boundary() -> None:
    """Checkpoint persistence is isolated from URL-enrichment orchestration."""
    module = importlib.import_module("osm_polygon_website_tag.pipeline.enrichment_checkpoint")

    assert set(module.__all__) == {
        "EnrichmentCheckpoint",
        "assemble_checkpoint",
        "checkpoint_parts",
        "load_checkpoint",
        "write_checkpoint_part",
    }

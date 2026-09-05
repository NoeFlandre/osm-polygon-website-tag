"""Pinned SaT ("Segment any Text") model loading and output normalization.

The segmenter is a Hugging Face model directory rather than a single binary, so
its pinned identity is a digest over every file it contains.  The revision is
supplied by the caller instead of hard-coded: nothing here may claim a commit
that has not actually been staged.

``wtpsplit`` is imported lazily inside the loader so that importing this module
-- which every sentence test does -- never drags in a deep-learning backend.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from osm_polygon_website_tag.pipeline.model_identity import (
    ModelIdentity,
    sha256_directory,
)

MODEL_REPOSITORY = "segment-any-text/sat-3l-sm"


# Deliberately a plain class rather than a dataclass: mutmut skips decorated
# class definitions, which would drop every method below out of the gate.
class SaTSplitter:
    """SaT-backed splitter with deterministic, validated output conversion."""

    def __init__(self, model: Any, identity: ModelIdentity) -> None:
        self._model = model
        self._identity = identity

    @property
    def identity(self) -> ModelIdentity:
        """Return the pinned identity of the loaded segmentation model."""
        return self._identity

    def split(self, texts: Sequence[str]) -> list[list[str]]:
        """Segment a batch of texts, one sentence list per input, in order."""
        batch = list(texts)
        if not batch:
            return []
        results = [list(result) for result in self._model.split(batch)]
        if len(results) != len(batch):
            raise ValueError("sentence result count does not match input count")
        return [_validated_sentences(result) for result in results]


def sat_model_identity(model_dir: Path | str, *, revision: str) -> ModelIdentity:
    """Return the pinned identity of a locally staged segmentation model."""
    directory = Path(model_dir)
    return ModelIdentity(
        repository=MODEL_REPOSITORY,
        filename=directory.name,
        revision=revision,
        sha256=sha256_directory(directory),
    )


def load_sat_splitter_from_path(model_dir: Path | str, *, revision: str) -> SaTSplitter:
    """Load the pinned segmentation model from a locally staged directory."""
    from wtpsplit import SaT

    identity = sat_model_identity(model_dir, revision=revision)
    return SaTSplitter(SaT(str(model_dir)), identity)


def _validated_sentences(result: list[Any]) -> list[str]:
    """Reject model output that is not a list of strings."""
    for sentence in result:
        if not isinstance(sentence, str):
            raise ValueError("model returned a non-string sentence")
    return list(result)


__all__ = [
    "MODEL_REPOSITORY",
    "SaTSplitter",
    "load_sat_splitter_from_path",
    "sat_model_identity",
]

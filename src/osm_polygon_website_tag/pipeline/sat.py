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

# The reserved node segments with the model on its GPU. 256 texts per forward
# batch keeps a single job's throughput high without holding more device memory
# than one 3-layer encoder needs.
DEFAULT_MODEL_BATCH_SIZE = 256
_CUDA = "cuda"
_CPU = "cpu"
_SUPPORTED_DEVICES = (_CUDA, _CPU)


# Deliberately a plain class rather than a dataclass: mutmut skips decorated
# class definitions, which would drop every method below out of the gate.
class SaTSplitter:
    """SaT-backed splitter with deterministic, validated output conversion."""

    def __init__(
        self,
        model: Any,
        identity: ModelIdentity,
        *,
        model_batch_size: int = DEFAULT_MODEL_BATCH_SIZE,
    ) -> None:
        self._model = model
        self._identity = identity
        self._model_batch_size = model_batch_size

    @property
    def identity(self) -> ModelIdentity:
        """Return the pinned identity of the loaded segmentation model."""
        return self._identity

    def split(self, texts: Sequence[str]) -> list[list[str]]:
        """Segment a batch of texts, one sentence list per input, in order."""
        batch = list(texts)
        if not batch:
            return []
        segmented = self._model.split(batch, batch_size=self._model_batch_size)
        results = [list(result) for result in segmented]
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


def select_device(*, requested: str | None, cuda_available: bool) -> str:
    """Resolve the device one job segments on, refusing an impossible request.

    Without a request the accelerator is used when the node exposes one, so a
    reserved GPU is never held idle while the encoder runs on two cores.
    """
    if requested is None:
        return _CUDA if cuda_available else _CPU
    return _validated_device(requested, cuda_available=cuda_available)


def _validated_device(requested: str, *, cuda_available: bool) -> str:
    """Return an explicitly requested device this host can actually provide."""
    if requested not in _SUPPORTED_DEVICES:
        raise ValueError(f"unsupported segmentation device: {requested!r}")
    if requested == _CUDA and not cuda_available:
        raise ValueError("requested CUDA device is not available")
    return requested


def prepare_model(model: Any, *, device: str) -> Any:
    """Move a loaded model onto its device, halving precision on an accelerator."""
    if device == _CUDA:
        model.half().to(_CUDA)
    return model


def load_sat_splitter_from_path(
    model_dir: Path | str,
    *,
    revision: str,
    device: str | None = None,
    model_batch_size: int = DEFAULT_MODEL_BATCH_SIZE,
) -> SaTSplitter:
    """Load the pinned segmentation model from a locally staged directory."""
    from wtpsplit import SaT

    identity = sat_model_identity(model_dir, revision=revision)
    resolved = select_device(requested=device, cuda_available=_cuda_available())
    model = prepare_model(SaT(str(model_dir)), device=resolved)
    return SaTSplitter(model, identity, model_batch_size=model_batch_size)


def _cuda_available() -> bool:
    """Report whether this host exposes a CUDA device, importing torch lazily."""
    import torch

    return bool(torch.cuda.is_available())


def _validated_sentences(result: list[Any]) -> list[str]:
    """Reject model output that is not a list of strings."""
    for sentence in result:
        if not isinstance(sentence, str):
            raise ValueError("model returned a non-string sentence")
    return list(result)


__all__ = [
    "DEFAULT_MODEL_BATCH_SIZE",
    "MODEL_REPOSITORY",
    "SaTSplitter",
    "load_sat_splitter_from_path",
    "prepare_model",
    "sat_model_identity",
    "select_device",
]

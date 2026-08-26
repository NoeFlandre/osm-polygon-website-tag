"""Pinned GlotLID model loading and FastText prediction normalization."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import fasttext
from huggingface_hub import hf_hub_download

MODEL_REPOSITORY = "cis-lmu/glotlid"
MODEL_FILENAME = "model_v3.bin"
MODEL_REVISION = "85cd671"
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ModelIdentity:
    """Immutable identity of the model binary used for one detection run."""

    repository: str
    filename: str
    revision: str
    sha256: str


@dataclass(frozen=True)
class LanguagePrediction:
    """One top-1 language label and its model probability."""

    label: str
    probability: float


class LanguageDetector(Protocol):
    """Small prediction boundary consumed by the shard pipeline."""

    identity: ModelIdentity

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]: ...


@dataclass(frozen=True)
class GlotLIDDetector:
    """FastText-backed GlotLID detector with deterministic output conversion."""

    model: Any
    identity: ModelIdentity

    def predict(self, texts: Sequence[str]) -> list[LanguagePrediction]:
        """Predict one normalized GlotLID result for each input text."""
        normalized = [_normalize_text(text) for text in texts]
        if not normalized:
            return []
        labels, probabilities = self.model.predict(normalized, k=1)
        _validate_prediction_counts(labels, probabilities, expected=len(normalized))
        return _normalise_predictions(labels, probabilities)


def load_glotlid_detector(cache_dir: Path) -> GlotLIDDetector:
    """Download/load the pinned GlotLID binary using the explicit cache directory."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = Path(
        hf_hub_download(
            repo_id=MODEL_REPOSITORY,
            filename=MODEL_FILENAME,
            revision=MODEL_REVISION,
            cache_dir=str(cache_dir),
        )
    )
    identity = ModelIdentity(
        repository=MODEL_REPOSITORY,
        filename=MODEL_FILENAME,
        revision=MODEL_REVISION,
        sha256=_sha256_file(model_path),
    )
    return GlotLIDDetector(fasttext.load_model(str(model_path)), identity)


def _normalize_text(text: str) -> str:
    """Remove line terminators unsupported by the FastText Python binding."""
    return text.replace("\r", " ").replace("\n", " ")


def _validate_prediction_counts(labels: Any, probabilities: Any, *, expected: int) -> None:
    """Reject model output that does not contain one result per input."""
    if len(labels) != expected or len(probabilities) != expected:
        raise ValueError("model prediction count does not match input count")


def _normalise_predictions(labels: Any, probabilities: Any) -> list[LanguagePrediction]:
    """Convert paired FastText top-1 results in input order."""
    return [
        _prediction_for_item(label_values, probability_values)
        for label_values, probability_values in zip(labels, probabilities, strict=True)
    ]


def _prediction_for_item(label_values: Any, probability_values: Any) -> LanguagePrediction:
    """Validate and convert one FastText top-1 result."""
    if not _one_item_sequence(label_values) or not _one_item_sequence(probability_values):
        raise ValueError("model returned an invalid top-1 prediction")
    label = label_values[0]
    if not isinstance(label, str) or not label:
        raise ValueError("model returned an invalid language label")
    return LanguagePrediction(
        label.removeprefix("__label__"), _validated_probability(probability_values[0])
    )


def _validated_probability(value: Any) -> float:
    """Validate and convert one model probability."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("model returned an invalid language probability")
    converted = float(value)
    if not math.isfinite(converted) or not 0 <= converted <= 1:
        raise ValueError("model returned an invalid language probability")
    return converted


def _one_item_sequence(value: Any) -> bool:
    """Return whether a model result contains exactly one item."""
    try:
        return len(value) == 1
    except TypeError:
        return False


def _sha256_file(path: Path) -> str:
    """Hash a model file in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "MODEL_FILENAME",
    "MODEL_REPOSITORY",
    "MODEL_REVISION",
    "GlotLIDDetector",
    "LanguageDetector",
    "LanguagePrediction",
    "ModelIdentity",
    "load_glotlid_detector",
]

"""Tests for the GlotLID/FastText adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import osm_polygon_website_tag.pipeline.glotlid as glotlid


class FakeFastText:
    def predict(self, texts: list[str], *, k: int) -> tuple[list[list[str]], list[list[float]]]:
        assert k == 1
        return (
            [["__label__eng_Latn"] if "hello" in text else ["__label__fra_Latn"] for text in texts],
            [[0.9] if "hello" in text else [0.8] for text in texts],
        )


def test_loader_downloads_only_the_pinned_model_into_the_requested_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_file = tmp_path / "model_v3.bin"
    model_file.write_bytes(b"model")
    calls: list[dict[str, object]] = []

    def download(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(model_file)

    monkeypatch.setattr(glotlid, "hf_hub_download", download)
    monkeypatch.setattr(glotlid.fasttext, "load_model", lambda path: FakeFastText())

    detector = glotlid.load_glotlid_detector(tmp_path / "cache")

    assert calls == [
        {
            "repo_id": "cis-lmu/glotlid",
            "filename": "model_v3.bin",
            "revision": "85cd671",
            "cache_dir": str(tmp_path / "cache"),
        }
    ]
    assert detector.identity.filename == "model_v3.bin"
    assert detector.identity.sha256 == hashlib.sha256(b"model").hexdigest()


def test_detector_returns_one_prediction_per_input_in_order() -> None:
    identity = glotlid.ModelIdentity("r", "f", "v", "h")
    detector = glotlid.GlotLIDDetector(FakeFastText(), identity)

    assert detector.predict(["hello\nworld", "bonjour"]) == [
        glotlid.LanguagePrediction("eng_Latn", 0.9),
        glotlid.LanguagePrediction("fra_Latn", 0.8),
    ]


@pytest.mark.parametrize("probability", [float("nan"), -0.1, 1.1])
def test_detector_rejects_invalid_probabilities(probability: float) -> None:
    class InvalidProbabilityModel:
        def predict(
            self, _texts: list[str], *, k: int
        ) -> tuple[list[list[str]], list[list[float]]]:
            assert k == 1
            return [["__label__eng_Latn"]], [[probability]]

    detector = glotlid.GlotLIDDetector(
        InvalidProbabilityModel(), glotlid.ModelIdentity("r", "f", "v", "h")
    )
    with pytest.raises(ValueError, match="probability"):
        detector.predict(["hello"])


def test_detector_rejects_mismatched_model_output() -> None:
    class MissingPredictionModel:
        def predict(
            self, _texts: list[str], *, k: int
        ) -> tuple[list[list[str]], list[list[float]]]:
            assert k == 1
            return [], []

    detector = glotlid.GlotLIDDetector(
        MissingPredictionModel(), glotlid.ModelIdentity("r", "f", "v", "h")
    )
    with pytest.raises(ValueError, match="prediction count"):
        detector.predict(["hello"])

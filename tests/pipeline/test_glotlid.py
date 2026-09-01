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

    loaded_paths: list[object] = []

    def load_model(path: object) -> FakeFastText:
        loaded_paths.append(path)
        return FakeFastText()

    monkeypatch.setattr(glotlid, "hf_hub_download", download)
    monkeypatch.setattr(glotlid.fasttext, "load_model", load_model)

    cache_dir = tmp_path / "nested" / "cache"
    detector = glotlid.load_glotlid_detector(cache_dir)
    rerun_detector = glotlid.load_glotlid_detector(cache_dir)

    expected_call = {
        "repo_id": "cis-lmu/glotlid",
        "filename": "model_v3.bin",
        "revision": "85cd671",
        "cache_dir": str(cache_dir),
    }
    assert calls == [expected_call, expected_call]
    assert loaded_paths == [str(model_file), str(model_file)]
    assert detector.model.__class__ is FakeFastText
    assert rerun_detector.identity == glotlid.ModelIdentity(
        "cis-lmu/glotlid",
        "model_v3.bin",
        "85cd671",
        hashlib.sha256(b"model").hexdigest(),
    )


def test_loader_can_load_a_staged_model_without_hugging_face(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_file = tmp_path / "model_v3.bin"
    model_file.write_bytes(b"staged model")
    loaded_paths: list[object] = []

    def fail_download(**_kwargs: object) -> str:
        raise AssertionError("staged loading must not contact Hugging Face")

    def load_model(path: object) -> FakeFastText:
        loaded_paths.append(path)
        return FakeFastText()

    monkeypatch.setattr(glotlid, "hf_hub_download", fail_download)
    monkeypatch.setattr(glotlid.fasttext, "load_model", load_model)

    detector = glotlid.load_glotlid_detector_from_path(model_file)

    assert loaded_paths == [str(model_file)]
    assert detector.identity == glotlid.ModelIdentity(
        "cis-lmu/glotlid",
        "model_v3.bin",
        "85cd671",
        hashlib.sha256(b"staged model").hexdigest(),
    )


def test_staged_loader_rejects_a_missing_model(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as missing:
        glotlid.load_glotlid_detector_from_path(tmp_path / "missing.bin")
    assert missing.value.args == (tmp_path / "missing.bin",)


def test_detector_returns_one_prediction_per_input_in_order() -> None:
    identity = glotlid.ModelIdentity("r", "f", "v", "h")
    detector = glotlid.GlotLIDDetector(FakeFastText(), identity)

    assert detector.predict(["hello\nworld", "bonjour"]) == [
        glotlid.LanguagePrediction("eng_Latn", 0.9),
        glotlid.LanguagePrediction("fra_Latn", 0.8),
    ]


def test_detector_normalizes_both_fasttext_line_terminators() -> None:
    class RecordingFastText:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def predict(self, texts: list[str], *, k: int) -> tuple[list[list[str]], list[list[float]]]:
            assert k == 1
            self.texts = texts
            return [["__label__eng_Latn"] for _text in texts], [[0.9] for _text in texts]

    model = RecordingFastText()
    detector = glotlid.GlotLIDDetector(model, glotlid.ModelIdentity("r", "f", "v", "h"))

    detector.predict(["before\rbetween\nafter"])

    assert model.texts == ["before between after"]


def test_detector_returns_empty_without_calling_the_model() -> None:
    model = FakeFastText()
    detector = glotlid.GlotLIDDetector(model, glotlid.ModelIdentity("r", "f", "v", "h"))

    assert detector.predict([]) == []


@pytest.mark.parametrize(
    ("labels", "probabilities"),
    [
        ([], [[0.9]]),
        ([["__label__eng_Latn"]], []),
        ([["__label__eng_Latn", "__label__fra_Latn"]], [[0.9]]),
        ([["__label__eng_Latn"]], [[0.9, 0.1]]),
        ([[None]], [[0.9]]),
        ([[""]], [[0.9]]),
        ([[1]], [[0.9]]),
    ],
)
def test_detector_rejects_invalid_top_one_shapes_and_labels(
    labels: object, probabilities: object
) -> None:
    class InvalidOutputModel:
        def predict(self, _texts: list[str], *, k: int) -> tuple[object, object]:
            assert k == 1
            return labels, probabilities

    detector = glotlid.GlotLIDDetector(
        InvalidOutputModel(), glotlid.ModelIdentity("r", "f", "v", "h")
    )
    with pytest.raises(ValueError):
        detector.predict(["hello"])


def test_detector_reports_invalid_top_one_shape_precisely() -> None:
    class EmptyTopOneModel:
        def predict(
            self, _texts: list[str], *, k: int
        ) -> tuple[list[list[str]], list[list[float]]]:
            assert k == 1
            return [[]], [[0.9]]

    detector = glotlid.GlotLIDDetector(
        EmptyTopOneModel(), glotlid.ModelIdentity("r", "f", "v", "h")
    )

    with pytest.raises(ValueError, match=r"^model returned an invalid top-1 prediction$"):
        detector.predict(["hello"])


def test_detector_reports_invalid_language_label_precisely() -> None:
    class EmptyLabelModel:
        def predict(
            self, _texts: list[str], *, k: int
        ) -> tuple[list[list[str]], list[list[float]]]:
            assert k == 1
            return [[""]], [[0.9]]

    detector = glotlid.GlotLIDDetector(EmptyLabelModel(), glotlid.ModelIdentity("r", "f", "v", "h"))

    with pytest.raises(ValueError, match=r"^model returned an invalid language label$"):
        detector.predict(["hello"])


@pytest.mark.parametrize("probability", [float("nan"), -0.1, 1.1, None, "0.5", True])
def test_detector_rejects_invalid_probabilities(probability: object) -> None:
    class InvalidProbabilityModel:
        def predict(
            self, _texts: list[str], *, k: int
        ) -> tuple[list[list[str]], list[list[object]]]:
            assert k == 1
            return [["__label__eng_Latn"]], [[probability]]

    detector = glotlid.GlotLIDDetector(
        InvalidProbabilityModel(), glotlid.ModelIdentity("r", "f", "v", "h")
    )
    with pytest.raises(ValueError, match="probability"):
        detector.predict(["hello"])


def test_detector_rejects_one_sided_prediction_count_mismatch() -> None:
    class OneSidedOutputModel:
        def predict(
            self, _texts: list[str], *, k: int
        ) -> tuple[list[list[str]], list[list[float]]]:
            assert k == 1
            return [["__label__eng_Latn"]], []

    detector = glotlid.GlotLIDDetector(
        OneSidedOutputModel(), glotlid.ModelIdentity("r", "f", "v", "h")
    )
    with pytest.raises(ValueError, match=r"^model prediction count does not match input count$"):
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


def test_prediction_normalization_requires_parallel_sequences() -> None:
    with pytest.raises(ValueError):
        glotlid._normalise_predictions([["__label__eng_Latn"]], [])


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0.0), (1, 1.0), (0.5, 0.5)],
)
def test_probability_validation_accepts_the_closed_unit_interval(
    value: object, expected: float
) -> None:
    assert glotlid._validated_probability(value) == expected


def test_probability_validation_accepts_fasttext_float32() -> None:
    numpy = pytest.importorskip("numpy")

    assert glotlid._validated_probability(numpy.float32(0.5)) == 0.5


def test_probability_validation_clamps_glotlid_float32_overshoot() -> None:
    numpy = pytest.importorskip("numpy")

    value = numpy.float32(1.0000100135803223)

    assert glotlid._validated_probability(value) == 1.0


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
)
def test_probability_validation_rejects_nonfinite_values_with_stable_message(
    value: float,
) -> None:
    with pytest.raises(ValueError, match=r"^model returned an invalid language probability$"):
        glotlid._validated_probability(value)


@pytest.mark.parametrize("value, expected", [(1.000009, 1.0), (-0.000009, 0.0)])
def test_probability_validation_clamps_fasttext_rounding_at_bounds(
    value: float, expected: float
) -> None:
    assert glotlid._validated_probability(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (-glotlid._PROBABILITY_BOUND_TOLERANCE, 0.0),
        (1 + glotlid._PROBABILITY_BOUND_TOLERANCE, 1.0),
    ],
)
def test_probability_validation_accepts_tolerance_boundaries(value: float, expected: float) -> None:
    assert glotlid._validated_probability(value) == expected


@pytest.mark.parametrize("value", [True, False, None, "0.5"])
def test_probability_validation_rejects_non_numeric_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^model returned an invalid language probability$"):
        glotlid._validated_probability(value)


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_probability_validation_rejects_out_of_range_values_with_stable_message(
    value: float,
) -> None:
    with pytest.raises(ValueError, match=r"^model returned an invalid language probability$"):
        glotlid._validated_probability(value)


@pytest.mark.parametrize("value", [True, False])
def test_real_probability_check_rejects_booleans(value: bool) -> None:
    assert glotlid._is_real_probability(value) is False


def test_one_item_sequence_distinguishes_scalar_empty_and_single_values() -> None:
    assert glotlid._one_item_sequence("scalar") is False
    assert glotlid._one_item_sequence([]) is False
    assert glotlid._one_item_sequence(["one"]) is True


def test_sha256_reads_in_bounded_chunks_and_stops_at_empty_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeHandle:
        def __init__(self) -> None:
            self.sizes: list[int] = []
            self.chunks = iter([b"model", b""])

        def __enter__(self) -> FakeHandle:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            assert size == glotlid._HASH_CHUNK_BYTES
            self.sizes.append(size)
            try:
                return next(self.chunks)
            except StopIteration as error:
                raise AssertionError("hash reader was not stopped by an empty chunk") from error

    handle = FakeHandle()
    monkeypatch.setattr(glotlid.Path, "open", lambda _path, _mode: handle)

    assert glotlid._sha256_file(tmp_path / "model.bin") == hashlib.sha256(b"model").hexdigest()
    assert handle.sizes == [glotlid._HASH_CHUNK_BYTES, glotlid._HASH_CHUNK_BYTES]

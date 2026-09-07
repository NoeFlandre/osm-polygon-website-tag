"""Contract for the pinned SaT sentence-segmentation adapter."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_website_tag.pipeline.sat import (
    DEFAULT_MODEL_BATCH_SIZE,
    MODEL_REPOSITORY,
    SaTSplitter,
    load_sat_splitter_from_path,
    prepare_model,
    sat_model_identity,
    select_device,
)


class _FakeModel:
    """Stands in for wtpsplit.SaT, which needs weights and a torch backend."""

    def __init__(self, results: list[list[Any]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = results

    def split(self, text_or_texts: Sequence[str], batch_size: int | None = None) -> Any:
        self.calls.append(list(text_or_texts))
        results = self._results
        if results is None:
            results = [[text] for text in text_or_texts]
        # wtpsplit returns a generator for a batch; the adapter must materialize it.
        return (list(result) for result in results)


def _model_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "sat-3l-sm"
    directory.mkdir()
    (directory / "config.json").write_bytes(b"{}")
    (directory / "model.safetensors").write_bytes(b"weights")
    return directory


def _splitter(model: _FakeModel, tmp_path: Path) -> SaTSplitter:
    return SaTSplitter(model, sat_model_identity(_model_dir(tmp_path), revision="abc1234"))


def test_split_materializes_the_generator_wtpsplit_returns(tmp_path: Path) -> None:
    model = _FakeModel([["One.", "Two."]])

    result = SaTSplitter(model, sat_model_identity(_model_dir(tmp_path), revision="r")).split(
        ["One. Two."]
    )

    assert result == [["One.", "Two."]]
    assert model.calls == [["One. Two."]]


def test_split_of_nothing_never_calls_the_model(tmp_path: Path) -> None:
    model = _FakeModel()

    assert _splitter(model, tmp_path).split([]) == []
    assert model.calls == []


def test_split_rejects_a_result_count_that_does_not_match(tmp_path: Path) -> None:
    model = _FakeModel([["only"]])

    with pytest.raises(
        ValueError, match=rf"^{re.escape('sentence result count does not match input count')}$"
    ):
        _splitter(model, tmp_path).split(["a", "b"])


def test_split_rejects_a_non_string_sentence(tmp_path: Path) -> None:
    model = _FakeModel([[17]])

    with pytest.raises(ValueError, match=rf"^{re.escape('model returned a non-string sentence')}$"):
        _splitter(model, tmp_path).split(["a"])


def test_identity_pins_repository_directory_and_revision(tmp_path: Path) -> None:
    identity = sat_model_identity(_model_dir(tmp_path), revision="abc1234")

    assert identity.repository == MODEL_REPOSITORY
    assert identity.filename == "sat-3l-sm"
    assert identity.revision == "abc1234"
    assert len(identity.sha256) == 64


def test_identity_changes_when_any_weight_byte_changes(tmp_path: Path) -> None:
    directory = _model_dir(tmp_path)
    before = sat_model_identity(directory, revision="r").sha256
    (directory / "model.safetensors").write_bytes(b"other")

    assert sat_model_identity(directory, revision="r").sha256 != before


def test_identity_changes_when_a_file_is_renamed(tmp_path: Path) -> None:
    """Hashing covers names, so a reshuffled repository is a different model."""
    directory = _model_dir(tmp_path)
    before = sat_model_identity(directory, revision="r").sha256
    (directory / "config.json").rename(directory / "renamed.json")

    assert sat_model_identity(directory, revision="r").sha256 != before


def test_identity_requires_a_real_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError) as error:
        sat_model_identity(tmp_path / "missing", revision="r")

    assert error.value.args == (tmp_path / "missing",)


def test_identity_is_exposed_for_checkpoint_binding(tmp_path: Path) -> None:
    identity = sat_model_identity(_model_dir(tmp_path), revision="abc1234")

    assert SaTSplitter(_FakeModel(), identity).identity is identity


def test_loader_imports_wtpsplit_lazily_and_pins_the_staged_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing this module must never drag in a deep-learning backend."""
    import sys
    import types

    directory = _model_dir(tmp_path)
    constructed: list[str] = []

    class FakeSaT:
        def __init__(self, name: str) -> None:
            constructed.append(name)

        def split(self, text_or_texts: Sequence[str], batch_size: int | None = None) -> Any:
            return ([text] for text in text_or_texts)

    module = types.ModuleType("wtpsplit")
    monkeypatch.setattr(module, "SaT", FakeSaT, raising=False)
    monkeypatch.setitem(sys.modules, "wtpsplit", module)

    torch_module = types.ModuleType("torch")
    monkeypatch.setattr(
        torch_module, "cuda", types.SimpleNamespace(is_available=lambda: False), raising=False
    )
    monkeypatch.setitem(sys.modules, "torch", torch_module)

    splitter = load_sat_splitter_from_path(directory, revision="abc1234")

    assert constructed == [str(directory)]
    assert splitter.identity.revision == "abc1234"
    assert splitter.identity.filename == "sat-3l-sm"
    assert splitter.split(["a"]) == [["a"]]


class _DeviceModel(_FakeModel):
    """Fake model recording the device and precision moves it received."""

    def __init__(self) -> None:
        super().__init__()
        self.moves: list[str] = []
        self.halved = 0
        self.batch_sizes: list[int] = []

    def half(self) -> _DeviceModel:
        self.halved += 1
        return self

    def to(self, device: str) -> _DeviceModel:
        self.moves.append(device)
        return self

    def split(self, text_or_texts: Sequence[str], batch_size: int | None = None) -> Any:
        if batch_size is not None:
            self.batch_sizes.append(batch_size)
        return super().split(text_or_texts, batch_size=batch_size)


def test_select_device_prefers_an_available_accelerator() -> None:
    assert select_device(requested=None, cuda_available=True) == "cuda"
    assert select_device(requested=None, cuda_available=False) == "cpu"


def test_select_device_honours_an_explicit_request() -> None:
    assert select_device(requested="cpu", cuda_available=True) == "cpu"
    assert select_device(requested="cuda", cuda_available=True) == "cuda"


def test_select_device_refuses_an_unavailable_accelerator() -> None:
    with pytest.raises(
        ValueError, match=rf"^{re.escape('requested CUDA device is not available')}$"
    ):
        select_device(requested="cuda", cuda_available=False)


def test_select_device_refuses_an_unknown_device() -> None:
    with pytest.raises(ValueError, match=r"^unsupported segmentation device: 'tpu'$"):
        select_device(requested="tpu", cuda_available=True)


def test_prepare_model_moves_a_cuda_model_to_half_precision(tmp_path: Path) -> None:
    model = _DeviceModel()

    prepared = prepare_model(model, device="cuda")

    assert prepared is model
    assert model.halved == 1
    assert model.moves == ["cuda"]


def test_prepare_model_leaves_a_cpu_model_in_full_precision(tmp_path: Path) -> None:
    model = _DeviceModel()

    prepare_model(model, device="cpu")

    assert model.halved == 0
    assert model.moves == []


def test_split_passes_the_configured_model_batch_size(tmp_path: Path) -> None:
    model = _DeviceModel()

    SaTSplitter(
        model,
        sat_model_identity(_model_dir(tmp_path), revision="r"),
        model_batch_size=128,
    ).split(["a", "b"])

    assert model.batch_sizes == [128]


def test_split_uses_the_documented_default_batch_size(tmp_path: Path) -> None:
    model = _DeviceModel()

    _splitter(model, tmp_path).split(["a"])

    assert model.batch_sizes == [DEFAULT_MODEL_BATCH_SIZE]
    assert DEFAULT_MODEL_BATCH_SIZE == 256


def test_loader_prepares_the_model_for_the_selected_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    directory = _model_dir(tmp_path)
    model = _DeviceModel()
    module = types.ModuleType("wtpsplit")
    monkeypatch.setattr(module, "SaT", lambda _name: model, raising=False)
    monkeypatch.setitem(sys.modules, "wtpsplit", module)
    torch_module = types.ModuleType("torch")
    monkeypatch.setattr(
        torch_module, "cuda", types.SimpleNamespace(is_available=lambda: True), raising=False
    )
    monkeypatch.setitem(sys.modules, "torch", torch_module)

    splitter = load_sat_splitter_from_path(directory, revision="abc1234")

    assert model.moves == ["cuda"]
    assert model.halved == 1
    assert splitter.split(["a"]) == [["a"]]


def _fake_backends(
    monkeypatch: pytest.MonkeyPatch, model: _DeviceModel, *, cuda_available: bool
) -> None:
    """Install torch-free stand-ins for the two backends the loader imports."""
    import sys
    import types

    module = types.ModuleType("wtpsplit")
    monkeypatch.setattr(module, "SaT", lambda _name: model, raising=False)
    monkeypatch.setitem(sys.modules, "wtpsplit", module)
    torch_module = types.ModuleType("torch")
    monkeypatch.setattr(
        torch_module,
        "cuda",
        types.SimpleNamespace(is_available=lambda: cuda_available),
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "torch", torch_module)


def test_loader_honours_an_explicit_cpu_request_on_a_gpu_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _DeviceModel()
    _fake_backends(monkeypatch, model, cuda_available=True)

    load_sat_splitter_from_path(_model_dir(tmp_path), revision="r", device="cpu")

    assert model.moves == []
    assert model.halved == 0


def test_loader_passes_an_explicit_model_batch_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _DeviceModel()
    _fake_backends(monkeypatch, model, cuda_available=False)

    splitter = load_sat_splitter_from_path(_model_dir(tmp_path), revision="r", model_batch_size=8)
    splitter.split(["a"])

    assert model.batch_sizes == [8]


def test_loader_defaults_to_the_documented_model_batch_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _DeviceModel()
    _fake_backends(monkeypatch, model, cuda_available=False)

    load_sat_splitter_from_path(_model_dir(tmp_path), revision="r").split(["a"])

    assert model.batch_sizes == [DEFAULT_MODEL_BATCH_SIZE]

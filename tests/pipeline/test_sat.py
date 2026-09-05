"""Contract for the pinned SaT sentence-segmentation adapter."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from osm_polygon_website_tag.pipeline.sat import (
    MODEL_REPOSITORY,
    SaTSplitter,
    load_sat_splitter_from_path,
    sat_model_identity,
)


class _FakeModel:
    """Stands in for wtpsplit.SaT, which needs weights and a torch backend."""

    def __init__(self, results: list[list[Any]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = results

    def split(self, text_or_texts: Sequence[str]) -> Any:
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

        def split(self, text_or_texts: Sequence[str]) -> Any:
            return ([text] for text in text_or_texts)

    module = types.ModuleType("wtpsplit")
    monkeypatch.setattr(module, "SaT", FakeSaT, raising=False)
    monkeypatch.setitem(sys.modules, "wtpsplit", module)

    splitter = load_sat_splitter_from_path(directory, revision="abc1234")

    assert constructed == [str(directory)]
    assert splitter.identity.revision == "abc1234"
    assert splitter.identity.filename == "sat-3l-sm"
    assert splitter.split(["a"]) == [["a"]]

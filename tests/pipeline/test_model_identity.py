"""Contract for the pinned model identity shared by model-backed stages."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import osm_polygon_website_tag.pipeline.model_identity as model_identity
from osm_polygon_website_tag.pipeline.model_identity import ModelIdentity


def test_identity_is_a_value_compared_by_its_four_pinned_fields() -> None:
    identity = ModelIdentity("repo", "file.bin", "rev", "a" * 64)

    assert identity == ModelIdentity("repo", "file.bin", "rev", "a" * 64)
    assert identity != ModelIdentity("repo", "file.bin", "other", "a" * 64)


def test_sha256_hashes_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"model")

    assert model_identity.sha256_file(path) == hashlib.sha256(b"model").hexdigest()


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
            assert size == model_identity._HASH_CHUNK_BYTES
            self.sizes.append(size)
            try:
                return next(self.chunks)
            except StopIteration as error:
                raise AssertionError("hash reader was not stopped by an empty chunk") from error

    handle = FakeHandle()
    monkeypatch.setattr(model_identity.Path, "open", lambda _path, _mode: handle)

    assert (
        model_identity.sha256_file(tmp_path / "model.bin") == hashlib.sha256(b"model").hexdigest()
    )
    assert handle.sizes == [model_identity._HASH_CHUNK_BYTES, model_identity._HASH_CHUNK_BYTES]


def test_sha256_directory_covers_nested_files_and_is_stable(tmp_path: Path) -> None:
    directory = tmp_path / "model"
    (directory / "nested").mkdir(parents=True)
    (directory / "a.json").write_bytes(b"a")
    (directory / "nested" / "b.bin").write_bytes(b"b")

    digest = model_identity.sha256_directory(directory)

    assert digest == model_identity.sha256_directory(directory)
    (directory / "nested" / "b.bin").write_bytes(b"c")
    assert model_identity.sha256_directory(directory) != digest


def test_sha256_directory_ignores_empty_subdirectories(tmp_path: Path) -> None:
    """Only files carry model content, so an empty directory changes nothing."""
    directory = tmp_path / "model"
    directory.mkdir()
    (directory / "a.json").write_bytes(b"a")
    digest = model_identity.sha256_directory(directory)

    (directory / "empty").mkdir()

    assert model_identity.sha256_directory(directory) == digest


def test_sha256_directory_rejects_a_file(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"x")

    with pytest.raises(NotADirectoryError):
        model_identity.sha256_directory(path)

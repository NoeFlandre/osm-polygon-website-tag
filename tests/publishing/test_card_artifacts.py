"""Focused contract tests for release card artifact promotion."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import osm_polygon_website_tag.publishing.card_artifacts as card_artifacts


class _EncodingProbe(str):
    """Expose the exact encoding requested by a production call."""

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        assert encoding == "utf-8"
        return super().encode(encoding, errors)


def _patch_existing_refresh_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    text_population: object,
    summary: object,
    stats: object,
    geometry: object,
) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def compute_text(root: Path, *, source_names: object) -> object:
        calls["text"] = (root, source_names)
        return text_population

    def compute_summary(
        root: Path,
        *,
        source_names: object,
        aggregation_mode: str,
    ) -> object:
        calls["summary"] = (root, source_names, aggregation_mode)
        return summary

    def compute_stats(
        root: Path,
        *,
        summary: object,
        text_population: object,
        source_names: object,
    ) -> object:
        calls["stats"] = (root, summary, text_population, source_names)
        return stats

    def compute_geometry(
        root: Path,
        *,
        source_names: object,
        text_population: object,
    ) -> object:
        calls["geometry"] = (root, source_names, text_population)
        return geometry

    monkeypatch.setattr(card_artifacts, "compute_text_population_summary", compute_text)
    monkeypatch.setattr(card_artifacts, "compute_polygon_density_summary", compute_summary)
    monkeypatch.setattr(card_artifacts, "compute_card_stats", compute_stats)
    monkeypatch.setattr(card_artifacts, "compute_geometry_stats", compute_geometry)

    def update_front(document: bytes, received_stats: object) -> bytes:
        calls["front"] = (document, received_stats)
        return b"front"

    def update_website(document: bytes, received_stats: object) -> bytes:
        calls["website"] = (document, received_stats)
        return b"website"

    def update_language(document: bytes, received_stats: object) -> bytes:
        calls["language"] = (document, received_stats)
        return b"language"

    def update_sentence(document: bytes, received_stats: object) -> bytes:
        calls["sentence"] = (document, received_stats)
        return b"sentence"

    def update_geometry(document: bytes, received_geometry: object) -> bytes:
        calls["geometry_section"] = (document, received_geometry)
        return b"geometry"

    def update_geographic(document: bytes, received_stats: object) -> bytes:
        calls["geographic"] = (document, received_stats)
        return b"updated-readme"

    monkeypatch.setattr(card_artifacts, "_update_readme_front_matter", update_front)
    monkeypatch.setattr(card_artifacts, "_update_website_text_section", update_website)
    monkeypatch.setattr(card_artifacts, "_update_language_section", update_language)
    monkeypatch.setattr(card_artifacts, "_update_sentence_section", update_sentence)
    monkeypatch.setattr(card_artifacts, "_update_geometry_section", update_geometry)
    monkeypatch.setattr(card_artifacts, "_update_geographic_section", update_geographic)
    return calls


def test_refresh_missing_readme_forwards_trusted_bundle_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    yaml_path = root / "dataset.yaml"
    yaml_path.write_bytes(b"trusted-yaml")
    source_names = ("source-a", "source-b")
    bundle = SimpleNamespace(
        readme=b"rebuilt-readme",
        dataset_yaml=b"rebuilt-yaml",
        summary=object(),
        geometry=object(),
    )
    calls: dict[str, Any] = {}

    def render_card(root_arg: Path, **kwargs: object) -> object:
        calls["render"] = (root_arg, kwargs)
        return bundle

    def validate(readme: bytes, dataset_yaml: bytes, **kwargs: object) -> None:
        calls["validate"] = (readme, dataset_yaml, kwargs)

    def promote(root_arg: Path, **kwargs: object) -> None:
        calls["promote"] = (root_arg, kwargs)

    monkeypatch.setattr(card_artifacts, "render_card_bundle", render_card)
    monkeypatch.setattr(card_artifacts, "_validate_trusted_card_identity", validate)
    monkeypatch.setattr(card_artifacts, "promote_release_card_artifacts", promote)

    result = card_artifacts.refresh_card_for_release(
        root,
        source_names=source_names,
        expected_readme_custom_sha256="readme-custom",
        expected_dataset_custom_sha256="dataset-custom",
        expected_readme_preserved_sha256="readme-body",
    )

    readme = root / "README.md"
    assert result == readme
    assert calls["render"] == (
        root,
        {"source_names": source_names, "_yaml_source": b"trusted-yaml"},
    )
    assert calls["validate"] == (
        b"rebuilt-readme",
        b"rebuilt-yaml",
        {
            "expected_readme_custom_sha256": "readme-custom",
            "expected_dataset_custom_sha256": "dataset-custom",
            "expected_readme_preserved_sha256": "readme-body",
        },
    )
    assert calls["promote"] == (
        root,
        {
            "source_names": source_names,
            "summary": bundle.summary,
            "geometry": bundle.geometry,
            "readme": readme,
            "original_readme": None,
            "updated_readme": b"rebuilt-readme",
            "yaml_path": yaml_path,
            "original_yaml": b"trusted-yaml",
            "updated_yaml": b"rebuilt-yaml",
        },
    )


def test_refresh_existing_readme_updates_all_release_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    readme = root / "README.md"
    yaml_path = root / "dataset.yaml"
    readme.write_bytes(b"original-readme")
    yaml_path.write_bytes(b"original-yaml")
    source_names = ("source",)
    text_population = object()
    summary = object()
    stats = object()
    geometry = object()
    calls = _patch_existing_refresh_dependencies(
        monkeypatch,
        text_population=text_population,
        summary=summary,
        stats=stats,
        geometry=geometry,
    )

    def update_yaml(original: bytes, received_stats: object) -> bytes:
        calls["yaml"] = (original, received_stats)
        return b"updated-yaml"

    def validate(document: bytes, dataset_yaml: bytes, **kwargs: object) -> None:
        calls["validate"] = (document, dataset_yaml, kwargs)

    def promote(root_arg: Path, **kwargs: object) -> None:
        calls["promote"] = (root_arg, kwargs)

    monkeypatch.setattr(card_artifacts, "_update_release_yaml", update_yaml)
    monkeypatch.setattr(card_artifacts, "_validate_trusted_card_identity", validate)
    monkeypatch.setattr(card_artifacts, "promote_release_card_artifacts", promote)

    result = card_artifacts.refresh_card_for_release(
        root,
        source_names=source_names,
        expected_readme_custom_sha256="readme-custom",
        expected_dataset_custom_sha256="dataset-custom",
        expected_readme_preserved_sha256="readme-body",
    )

    assert result == readme
    assert calls["text"] == (root, source_names)
    assert calls["summary"] == (root, source_names, "global_unique_text")
    assert calls["stats"] == (root, summary, text_population, source_names)
    assert calls["geometry"] == (root, source_names, text_population)
    assert calls["front"] == (b"original-readme", stats)
    assert calls["website"] == (b"front", stats)
    assert calls["language"] == (b"website", stats)
    assert calls["sentence"] == (b"language", stats)
    assert calls["geometry_section"] == (b"sentence", geometry)
    assert calls["geographic"] == (b"geometry", stats)
    assert calls["yaml"] == (b"original-yaml", stats)
    assert calls["validate"] == (
        b"updated-readme",
        b"updated-yaml",
        {
            "expected_readme_custom_sha256": "readme-custom",
            "expected_dataset_custom_sha256": "dataset-custom",
            "expected_readme_preserved_sha256": "readme-body",
        },
    )
    assert calls["promote"] == (
        root,
        {
            "source_names": source_names,
            "summary": summary,
            "geometry": geometry,
            "readme": readme,
            "original_readme": b"original-readme",
            "updated_readme": b"updated-readme",
            "yaml_path": yaml_path,
            "original_yaml": b"original-yaml",
            "updated_yaml": b"updated-yaml",
        },
    )


def test_refresh_existing_readme_merges_front_matter_when_yaml_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    readme = root / "README.md"
    readme.write_bytes(b"original-readme")
    text_population = object()
    summary = object()
    stats = object()
    geometry = object()
    calls = _patch_existing_refresh_dependencies(
        monkeypatch,
        text_population=text_population,
        summary=summary,
        stats=stats,
        geometry=geometry,
    )

    rendered = _EncodingProbe("generated-front-matter")

    def render_front(received_stats: object) -> _EncodingProbe:
        assert received_stats is stats
        return rendered

    def read_front(document: bytes) -> bytes:
        calls["read_front"] = document
        return b"trusted-front-matter"

    def merge(generated: bytes, source: bytes) -> bytes:
        calls["merge"] = (generated, source)
        return b"merged-yaml"

    def validate(document: bytes, dataset_yaml: bytes, **kwargs: object) -> None:
        calls["validate"] = (document, dataset_yaml, kwargs)

    def promote(root_arg: Path, **kwargs: object) -> None:
        calls["promote"] = (root_arg, kwargs)

    monkeypatch.setattr(card_artifacts, "_render_yaml_front_matter", render_front)
    monkeypatch.setattr(card_artifacts, "_readme_front_matter", read_front)
    monkeypatch.setattr(card_artifacts, "_merge_yaml_custom_metadata", merge)
    monkeypatch.setattr(card_artifacts, "_update_release_yaml", pytest.fail)
    monkeypatch.setattr(card_artifacts, "_validate_trusted_card_identity", validate)
    monkeypatch.setattr(card_artifacts, "promote_release_card_artifacts", promote)

    result = card_artifacts.refresh_card_for_release(root)

    assert result == readme
    assert calls["read_front"] == b"original-readme"
    assert calls["merge"] == (b"generated-front-matter", b"trusted-front-matter")
    assert calls["validate"][1] == b"merged-yaml"
    assert calls["promote"][1]["original_yaml"] is None
    assert calls["promote"][1]["updated_yaml"] == b"merged-yaml"


def test_promote_release_card_artifacts_forwards_staging_and_cleans_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "nested" / "run"
    source_names = ("source",)
    summary = object()
    geometry = object()
    readme = root / "README.md"
    yaml_path = root / "dataset.yaml"
    seen: dict[str, Any] = {}

    def build_map(root_arg: Path, **kwargs: object) -> None:
        seen["map"] = (root_arg, kwargs)
        output_path = kwargs["output_path"]
        assert isinstance(output_path, Path)
        output_path.write_bytes(b"map")

    def stage_promotions(root_arg: Path, **kwargs: object) -> list[tuple[str, str]]:
        seen["promotions"] = (root_arg, kwargs)
        for key in ("staged_readme", "staged_yaml", "staged_stats"):
            path = kwargs[key]
            assert isinstance(path, Path)
            path.write_bytes(b"staged")
        return [("staged", "target")]

    def promote(promotions: object) -> None:
        seen["atomic"] = promotions

    monkeypatch.setattr(card_artifacts, "build_polygon_density_map", build_map)
    monkeypatch.setattr(card_artifacts, "_release_card_promotions", stage_promotions)
    monkeypatch.setattr(card_artifacts, "atomic_promote_bundle", promote)

    card_artifacts.promote_release_card_artifacts(
        root,
        source_names=source_names,
        summary=cast(Any, summary),
        geometry=cast(Any, geometry),
        readme=readme,
        original_readme=b"old-readme",
        updated_readme=b"new-readme",
        yaml_path=yaml_path,
        original_yaml=b"old-yaml",
        updated_yaml=b"new-yaml",
    )

    staged_map = root / ".assets" / "geographic_polygon_density.png.release.building"
    assert seen["map"] == (
        root,
        {
            "summary": summary,
            "output_path": staged_map,
            "source_names": source_names,
            "aggregation_mode": "global_unique_text",
        },
    )
    assert seen["promotions"] == (
        root,
        {
            "readme": readme,
            "original_readme": b"old-readme",
            "updated_readme": b"new-readme",
            "yaml_path": yaml_path,
            "original_yaml": b"old-yaml",
            "updated_yaml": b"new-yaml",
            "staged_readme": root / ".README.md.release.building",
            "staged_yaml": root / ".dataset.yaml.release.building",
            "staged_map": staged_map,
            "staged_stats": root / ".stats.json.release.building",
            "geometry": geometry,
        },
    )
    assert seen["atomic"] == [("staged", "target")]
    assert not (root / ".README.md.release.building").exists()
    assert not (root / ".dataset.yaml.release.building").exists()
    assert not (root / ".stats.json.release.building").exists()
    assert not staged_map.exists()


def test_promote_release_card_artifacts_skips_empty_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "run"
    atomic_calls: list[object] = []

    def build_map(_root: Path, **kwargs: object) -> None:
        output_path = kwargs["output_path"]
        assert isinstance(output_path, Path)
        output_path.write_bytes(b"map")

    monkeypatch.setattr(card_artifacts, "build_polygon_density_map", build_map)
    monkeypatch.setattr(card_artifacts, "_release_card_promotions", lambda *_a, **_k: [])
    monkeypatch.setattr(card_artifacts, "atomic_promote_bundle", atomic_calls.append)

    card_artifacts.promote_release_card_artifacts(
        root,
        source_names=None,
        summary=cast(Any, object()),
        geometry=cast(Any, object()),
        readme=root / "README.md",
        original_readme=None,
        updated_readme=b"readme",
        yaml_path=root / "dataset.yaml",
        original_yaml=None,
        updated_yaml=None,
    )

    assert atomic_calls == []
    assert not (root / ".assets" / "geographic_polygon_density.png.release.building").exists()


def test_promote_release_card_artifacts_cleans_staging_on_render_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "run"
    (root / ".assets").mkdir(parents=True)
    staged_paths = (
        root / ".README.md.release.building",
        root / ".dataset.yaml.release.building",
        root / ".stats.json.release.building",
        root / ".assets" / "geographic_polygon_density.png.release.building",
    )
    for path in staged_paths:
        path.write_bytes(b"stale")

    def fail_render(_root: Path, **_kwargs: object) -> None:
        raise RuntimeError("render failed")

    monkeypatch.setattr(card_artifacts, "build_polygon_density_map", fail_render)

    with pytest.raises(RuntimeError, match="render failed"):
        card_artifacts.promote_release_card_artifacts(
            root,
            source_names=None,
            summary=cast(Any, object()),
            geometry=cast(Any, object()),
            readme=root / "README.md",
            original_readme=None,
            updated_readme=b"readme",
            yaml_path=root / "dataset.yaml",
            original_yaml=None,
            updated_yaml=None,
        )

    assert all(not path.exists() for path in staged_paths)


def test_release_card_promotions_preserves_helper_order_and_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    readme = root / "README.md"
    yaml_path = root / "dataset.yaml"
    original_readme = b"old-readme"
    updated_readme = b"new-readme"
    original_yaml = b"old-yaml"
    updated_yaml = b"new-yaml"
    staged_readme = root / "staged-readme"
    staged_yaml = root / "staged-yaml"
    staged_map = root / "staged-map"
    staged_stats = root / "staged-stats"
    geometry = object()
    calls: list[tuple[str, tuple[object, ...]]] = []

    def stage_readme(*args: object) -> list[tuple[str, str]]:
        calls.append(("readme", args))
        return [("readme", "target")]

    def stage_yaml(*args: object) -> list[tuple[str, str]]:
        calls.append(("yaml", args))
        return [("yaml", "target")]

    def stage_map(*args: object) -> list[tuple[str, str]]:
        calls.append(("map", args))
        return [("map", "target")]

    def stage_stats(*args: object) -> list[tuple[str, str]]:
        calls.append(("stats", args))
        return [("stats", "target")]

    monkeypatch.setattr(card_artifacts, "_stage_readme_promotion", stage_readme)
    monkeypatch.setattr(card_artifacts, "_stage_yaml_promotion", stage_yaml)
    monkeypatch.setattr(card_artifacts, "_stage_release_map", stage_map)
    monkeypatch.setattr(card_artifacts, "_stage_geometry_stats", stage_stats)

    result = card_artifacts._release_card_promotions(
        root,
        readme=readme,
        original_readme=original_readme,
        updated_readme=updated_readme,
        yaml_path=yaml_path,
        original_yaml=original_yaml,
        updated_yaml=updated_yaml,
        staged_readme=staged_readme,
        staged_yaml=staged_yaml,
        staged_map=staged_map,
        staged_stats=staged_stats,
        geometry=cast(Any, geometry),
    )

    assert result == [
        ("readme", "target"),
        ("yaml", "target"),
        ("map", "target"),
        ("stats", "target"),
    ]
    assert calls == [
        ("readme", (readme, original_readme, updated_readme, staged_readme)),
        ("yaml", (yaml_path, original_yaml, updated_yaml, staged_yaml)),
        ("map", (root, staged_map)),
        ("stats", (staged_stats, root, geometry)),
    ]


def test_stage_readme_promotion_only_skips_unchanged_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    staged = tmp_path / "staged-readme"

    assert card_artifacts._stage_readme_promotion(target, b"same", b"same", staged) == []
    assert card_artifacts._stage_readme_promotion(target, None, b"new", staged) == [
        (staged, target)
    ]
    assert staged.read_bytes() == b"new"
    assert card_artifacts._stage_readme_promotion(target, b"old", b"newer", staged) == [
        (staged, target)
    ]
    assert staged.read_bytes() == b"newer"


def test_stage_yaml_promotion_handles_missing_and_unchanged_content(tmp_path: Path) -> None:
    target = tmp_path / "dataset.yaml"
    staged = tmp_path / "staged-yaml"

    assert card_artifacts._stage_yaml_promotion(target, b"old", None, staged) == []
    assert card_artifacts._stage_yaml_promotion(target, b"same", b"same", staged) == []
    assert card_artifacts._stage_yaml_promotion(target, None, b"new", staged) == [(staged, target)]
    assert staged.read_bytes() == b"new"


def test_readme_front_matter_returns_only_metadata_or_empty_bytes() -> None:
    document = b"---\nlicense: mit\n---\nbody\n"

    assert card_artifacts._readme_front_matter(document) == b"---\nlicense: mit\n---\n"
    assert card_artifacts._readme_front_matter(b"body without front matter") == b""


def test_validate_trusted_card_identity_forwards_all_expected_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    def readme_hash(document: bytes, *, readme: bool) -> str:
        calls["hash"] = (document, readme)
        return "actual-readme"

    def validate_readme(actual: str | None, expected: str | None) -> None:
        calls["readme"] = (actual, expected)

    def validate_yaml(document: bytes, expected: str | None) -> None:
        calls["yaml"] = (document, expected)

    def validate_body(document: bytes, custom: str | None, expected: str | None) -> None:
        calls["body"] = (document, custom, expected)

    monkeypatch.setattr(card_artifacts, "yaml_custom_sha256_bytes", readme_hash)
    monkeypatch.setattr(card_artifacts, "_validate_readme_custom_identity", validate_readme)
    monkeypatch.setattr(card_artifacts, "_validate_dataset_custom_identity", validate_yaml)
    monkeypatch.setattr(card_artifacts, "_validate_readme_body_identity", validate_body)

    card_artifacts._validate_trusted_card_identity(
        b"readme",
        b"dataset-yaml",
        expected_readme_custom_sha256="readme-expected",
        expected_dataset_custom_sha256="dataset-expected",
        expected_readme_preserved_sha256="body-expected",
    )

    assert calls == {
        "hash": (b"readme", True),
        "readme": ("actual-readme", "readme-expected"),
        "yaml": (b"dataset-yaml", "dataset-expected"),
        "body": (b"readme", "actual-readme", "body-expected"),
    }


@pytest.mark.parametrize(
    ("actual", "expected"),
    [(None, None), ("actual", None), ("actual", "actual")],
)
def test_validate_readme_custom_identity_accepts_matching_or_unchecked(
    actual: str | None,
    expected: str | None,
) -> None:
    card_artifacts._validate_readme_custom_identity(actual, expected)


def test_validate_readme_custom_identity_rejects_mismatch() -> None:
    with pytest.raises(ValueError) as error:
        card_artifacts._validate_readme_custom_identity("actual", "expected")

    assert str(error.value) == "missing README custom metadata cannot be recovered"


def test_validate_dataset_custom_identity_hashes_only_when_expected_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bytes] = []

    def custom_hash(document: bytes) -> str:
        calls.append(document)
        return "custom"

    monkeypatch.setattr(card_artifacts, "yaml_custom_sha256_bytes", custom_hash)

    card_artifacts._validate_dataset_custom_identity(b"unchecked", None)
    assert calls == []
    card_artifacts._validate_dataset_custom_identity(b"matching", "custom")
    assert calls == [b"matching"]

    with pytest.raises(ValueError) as error:
        card_artifacts._validate_dataset_custom_identity(b"different", "expected")
    assert str(error.value) == "dataset YAML custom metadata cannot be recovered"
    assert calls == [b"matching", b"different"]


def test_validate_readme_body_identity_requires_both_trusted_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bytes] = []

    def preserved_hash(document: bytes) -> str:
        calls.append(document)
        return "body"

    monkeypatch.setattr(card_artifacts, "readme_preserved_sha256_bytes", preserved_hash)

    card_artifacts._validate_readme_body_identity(b"unchecked", "custom", None)
    card_artifacts._validate_readme_body_identity(b"missing-custom", None, "body")
    assert calls == []
    card_artifacts._validate_readme_body_identity(b"matching", "custom", "body")
    assert calls == [b"matching"]

    monkeypatch.setattr(card_artifacts, "readme_preserved_sha256_bytes", lambda _doc: "wrong")
    with pytest.raises(ValueError) as error:
        card_artifacts._validate_readme_body_identity(b"different", "custom", "body")
    assert str(error.value) == "missing README body cannot be recovered"


def test_stage_release_map_promotes_only_changed_bytes(tmp_path: Path) -> None:
    root = tmp_path
    target = root / card_artifacts.POLYGON_DENSITY_ASSET_REL_PATH
    target.parent.mkdir(parents=True)
    staged = root / "staged-map"
    staged.write_bytes(b"same")
    target.write_bytes(b"same")

    assert card_artifacts._stage_release_map(root, staged) == []
    staged.write_bytes(b"changed")
    assert card_artifacts._stage_release_map(root, staged) == [(staged, target)]


class _FakeTarget:
    def __init__(self, *, existing: bool, content: str = "") -> None:
        self.existing = existing
        self.content = content
        self.read_encoding: str | None = None

    def is_file(self) -> bool:
        return self.existing

    def read_text(self, *, encoding: str) -> str:
        self.read_encoding = encoding
        assert encoding == "utf-8"
        return self.content


class _FakeRunDir:
    def __init__(self, target: _FakeTarget) -> None:
        self.target = target

    def __truediv__(self, name: str) -> _FakeTarget:
        assert name == card_artifacts.GEOMETRY_STATS_FILENAME
        return self.target


class _FakeStaged:
    def __init__(self) -> None:
        self.written: tuple[str, str] | None = None

    def write_text(self, content: str, *, encoding: str) -> None:
        assert encoding == "utf-8"
        self.written = (content, encoding)


def test_stage_geometry_stats_skips_identical_utf8_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _FakeTarget(existing=True, content="rendered")
    staged = _FakeStaged()
    geometry = object()
    monkeypatch.setattr(card_artifacts, "render_geometry_stats", lambda received: "rendered")

    assert (
        card_artifacts._stage_geometry_stats(
            cast(Any, staged),
            cast(Any, _FakeRunDir(target)),
            cast(Any, geometry),
        )
        == []
    )
    assert target.read_encoding == "utf-8"
    assert staged.written is None


def test_stage_geometry_stats_writes_changed_utf8_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _FakeTarget(existing=False)
    staged = _FakeStaged()
    geometry = object()
    monkeypatch.setattr(card_artifacts, "render_geometry_stats", lambda received: "rendered")

    assert card_artifacts._stage_geometry_stats(
        cast(Any, staged),
        cast(Any, _FakeRunDir(target)),
        cast(Any, geometry),
    ) == [(staged, target)]
    assert staged.written == ("rendered", "utf-8")

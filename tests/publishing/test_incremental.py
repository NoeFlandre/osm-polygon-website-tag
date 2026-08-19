"""RED tests for resumable per-source publication decisions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from osm_polygon_website_tag.publishing.incremental import (
    _validate_legacy_entry,
    incremental_publish_changed_shard,
    load_upload_checkpoint,
    reconcile_upload_checkpoint,
    remote_polygon_shard_hashes,
)
from osm_polygon_website_tag.runtime.run_state import hash_shard


def _run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    shard = run_dir / "polygons" / "a.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"shard-a")
    (run_dir / "README.md").write_text("card-a")
    (run_dir / "dataset.yaml").write_text("yaml-a")
    map_path = run_dir / "assets" / "geographic_polygon_density.png"
    map_path.parent.mkdir()
    map_path.write_bytes(b"\x89PNG\r\n\x1a\nmap-a")
    return run_dir, shard


def test_incremental_uploads_changed_shard_and_bundle(tmp_path: Path, monkeypatch) -> None:
    run_dir, shard = _run(tmp_path)
    captured: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)

    assert plan.shard_changed is True
    assert plan.bundle_changed is True
    assert captured == [
        shard,
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "assets" / "geographic_polygon_density.png",
    ]
    checkpoint = json.loads((run_dir / "manifests" / "uploaded_polygons.json").read_text())
    assert checkpoint["schema_version"] == "v2"
    assert checkpoint["sources"]["a.osm.pbf"]["polygon_sha256"]


def test_incremental_bundle_only_upload_does_not_upload_shard(tmp_path: Path, monkeypatch) -> None:
    run_dir, shard = _run(tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: None,
    )
    incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)
    (run_dir / "README.md").write_text("card-b")
    captured: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)

    assert plan.shard_changed is False
    assert plan.bundle_changed is True
    assert shard not in captured
    assert captured == [
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "assets" / "geographic_polygon_density.png",
    ]


def test_incremental_dry_run_does_not_write_checkpoint_or_upload(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir, _ = _run(tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("uploaded")),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=True)

    assert plan.upload_paths
    assert not (run_dir / "manifests" / "uploaded_polygons.json").exists()


def test_incremental_republishes_bundle_when_map_contract_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir, shard = _run(tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **_kwargs: None,
    )
    incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    checkpoint["global_bundle"]["map_contract_version"] = 0
    checkpoint_path.write_text(json.dumps(checkpoint))

    captured: list[Path] = []
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **kwargs: captured.extend(kwargs["artifact_paths"]),
    )

    plan = incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)

    assert plan.shard_changed is False
    assert plan.bundle_changed is True
    assert shard not in captured
    assert captured == [
        run_dir / "README.md",
        run_dir / "dataset.yaml",
        run_dir / "assets" / "geographic_polygon_density.png",
    ]


def test_incremental_rejects_malformed_checkpoint(tmp_path: Path) -> None:
    run_dir, _ = _run(tmp_path)
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(
        json.dumps({"schema_version": "v2", "global_bundle": [], "sources": {}})
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"))


def test_reconcile_checkpoint_uses_remote_shards_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, shard = _run(tmp_path)
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(
        json.dumps(
            {
                "schema_version": "v2",
                "global_bundle": {},
                "sources": {"stale.osm.pbf": {"polygon_sha256": "0" * 64}},
            }
        )
    )
    remote_sha = hash_shard(shard)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental.remote_polygon_shard_hashes",
        lambda **_kwargs: {
            "a.osm.pbf": remote_sha,
            "missing.osm.pbf": "1" * 64,
        },
    )

    checkpoint = reconcile_upload_checkpoint(
        run_dir,
        repo_id="owner/dataset",
        token="token",
    )

    assert checkpoint["sources"] == {"a.osm.pbf": {"polygon_sha256": remote_sha}}


def test_remote_polygon_shard_hashes_reads_only_parquet_lfs_entries(monkeypatch) -> None:
    import huggingface_hub

    class Api:
        def __init__(self, *, token: str) -> None:
            assert token == "token"

        def list_repo_tree(self, *_args, **_kwargs):
            return [
                SimpleNamespace(
                    path="polygons/a.parquet",
                    lfs=SimpleNamespace(sha256="a" * 64),
                ),
                SimpleNamespace(path="polygons/README.md", lfs=None),
            ]

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)

    assert remote_polygon_shard_hashes(repo_id="owner/dataset", token="token") == {
        "a.osm.pbf": "a" * 64
    }


# ---------------------------------------------------------------------------
# Typed-checkpoint parsing and validation
# ---------------------------------------------------------------------------


def _write_checkpoint(run_dir: Path, payload: object) -> Path:
    path = run_dir / "manifests" / "uploaded_polygons.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_upload_checkpoint_returns_default_when_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    checkpoint = load_upload_checkpoint(run_dir)

    assert checkpoint == {
        "schema_version": "v2",
        "global_bundle": {},
        "sources": {},
    }


def test_load_upload_checkpoint_round_trips_v2_checkpoint(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {
                "readme_sha256": "a" * 64,
                "dataset_yaml_sha256": "b" * 64,
                "map_sha256": "c" * 64,
                "map_contract_version": 2,
            },
            "sources": {
                "a.osm.pbf": {"polygon_sha256": "d" * 64},
            },
        },
    )

    checkpoint = load_upload_checkpoint(run_dir)

    assert checkpoint == {
        "schema_version": "v2",
        "global_bundle": {
            "readme_sha256": "a" * 64,
            "dataset_yaml_sha256": "b" * 64,
            "map_sha256": "c" * 64,
            "map_contract_version": 2,
        },
        "sources": {"a.osm.pbf": {"polygon_sha256": "d" * 64}},
    }


def test_load_upload_checkpoint_migrates_valid_legacy_checkpoint(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "a.osm.pbf": {"polygon_sha256": "0" * 64},
            "b.osm.pbf": {"polygon_sha256": "f" * 64},
        },
    )

    checkpoint = load_upload_checkpoint(run_dir)

    assert checkpoint == {
        "schema_version": "v2",
        "global_bundle": {},
        "sources": {
            "a.osm.pbf": {"polygon_sha256": "0" * 64},
            "b.osm.pbf": {"polygon_sha256": "f" * 64},
        },
    }


def test_load_upload_checkpoint_rejects_malformed_root(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(run_dir, ["not", "a", "dict"])

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_unknown_schema_version(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v3",
            "global_bundle": {},
            "sources": {},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_malformed_global_bundle(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": [],
            "sources": {},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_global_bundle_bool_values(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {"map_contract_version": True},
            "sources": {},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_malformed_sources(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {},
            "sources": [],
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_non_string_source_keys(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {},
            "sources": {
                42: {"polygon_sha256": "a" * 64},
            },
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_source_keys_without_osm_pbf_suffix(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {},
            "sources": {
                "monaco-latest": {"polygon_sha256": "a" * 64},
            },
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_non_dict_source_records(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {},
            "sources": {"a.osm.pbf": "not-a-dict"},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


@pytest.mark.parametrize(
    "bad_hash",
    [
        None,
        "",
        "abc",
        "z" * 64,
        "A" * 64,
        "0" * 63,
        "0" * 65,
        123,
        ["a" * 64],
    ],
)
def test_load_upload_checkpoint_rejects_invalid_hashes(tmp_path: Path, bad_hash: object) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {},
            "sources": {"a.osm.pbf": {"polygon_sha256": bad_hash}},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_malformed_legacy_entry(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "a.osm.pbf": {"polygon_sha256": "not-a-hex"},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", {}),
        (42, {}),
        ("a-latest", {}),
        ("a-latest.osm.pbf", "not-a-dict"),
    ],
)
def test_validate_legacy_entry_rejects_invalid_shapes(key: object, value: object) -> None:
    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        _validate_legacy_entry(key, value)


def test_incremental_publish_changed_shard_persists_deterministic_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful incremental upload writes the same checkpoint bytes on
    every equivalent invocation: identical filename set, ordering, and
    formatting."""
    run_dir, _ = _run(tmp_path)
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental._upload_folder",
        lambda _run, **_kwargs: None,
    )
    incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)
    first_bytes = (run_dir / "manifests" / "uploaded_polygons.json").read_text()

    incremental_publish_changed_shard(run_dir, Path("a.osm.pbf"), dry_run=False)
    second_bytes = (run_dir / "manifests" / "uploaded_polygons.json").read_text()

    assert first_bytes == second_bytes
    parsed = json.loads(first_bytes)
    assert parsed["schema_version"] == "v2"
    assert set(parsed["sources"]) == {"a.osm.pbf"}
    assert set(parsed) == {"schema_version", "global_bundle", "sources"}


# ---------------------------------------------------------------------------
# Strict typed-checkpoint contract corrections
# ---------------------------------------------------------------------------


def test_load_upload_checkpoint_wraps_invalid_json_syntax(tmp_path: Path) -> None:
    """Malformed JSON syntax raises the documented ``invalid uploaded polygon
    checkpoint`` :class:`ValueError`; the underlying
    :class:`json.JSONDecodeError` is normalised at the load boundary."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = run_dir / "manifests" / "uploaded_polygons.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_wraps_invalid_utf8_bytes(tmp_path: Path) -> None:
    """Non-UTF-8 bytes on disk raise the documented :class:`ValueError`."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = run_dir / "manifests" / "uploaded_polygons.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0xff is invalid as the start of a UTF-8 code unit; write in binary
    # mode so the encoding error originates from the JSON loader.
    path.write_bytes(b"\xff\xfe\xfd")

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_explicit_null_schema_version(tmp_path: Path) -> None:
    """An explicit ``schema_version: null`` is rejected; legacy migration is
    reserved for checkpoints that omit the ``schema_version`` key entirely."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": None,
            "a.osm.pbf": {"polygon_sha256": "0" * 64},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_invalid_global_bundle_hashes(
    tmp_path: Path,
) -> None:
    """Each known global-bundle hash must be a lowercase 64-character hex
    string. Bad values fail closed with the documented ValueError."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {
                "readme_sha256": "A" * 64,
                "dataset_yaml_sha256": "b" * 64,
                "map_sha256": "c" * 64,
                "map_contract_version": 1,
            },
            "sources": {},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


@pytest.mark.parametrize(
    "bundle",
    [
        {"readme_sha256": "not-hex"},
        {"dataset_yaml_sha256": "g" * 64},
        {"map_sha256": "z" * 64},
        {"map_sha256": "0" * 63},
        {"map_sha256": "0" * 65},
        {"readme_sha256": None},
    ],
)
def test_load_upload_checkpoint_rejects_bad_bundle_hex(
    tmp_path: Path, bundle: dict[str, object]
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload: dict[str, object] = {
        "schema_version": "v2",
        "global_bundle": dict(bundle),
        "sources": {},
    }
    payload["global_bundle"]["map_contract_version"] = 1
    _write_checkpoint(run_dir, payload)

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_string_map_contract_version(
    tmp_path: Path,
) -> None:
    """``map_contract_version`` must be a non-bool integer; string values
    are rejected."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {
                "readme_sha256": "a" * 64,
                "dataset_yaml_sha256": "b" * 64,
                "map_sha256": "c" * 64,
                "map_contract_version": "1",
            },
            "sources": {},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_unknown_source_field(tmp_path: Path) -> None:
    """Per-source entries may only contain ``polygon_sha256``; any extra key
    is rejected at the validation boundary."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {},
            "sources": {
                "a.osm.pbf": {
                    "polygon_sha256": "a" * 64,
                    "polygon_size": 1234,
                },
            },
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_rejects_unknown_global_bundle_field(
    tmp_path: Path,
) -> None:
    """``global_bundle`` may only contain the documented keys
    (readme_sha256, dataset_yaml_sha256, map_sha256, map_contract_version)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "schema_version": "v2",
            "global_bundle": {
                "unexpected_key": "value",
                "map_contract_version": 1,
            },
            "sources": {},
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(run_dir)


def test_load_upload_checkpoint_preserves_legacy_default_global_bundle(
    tmp_path: Path,
) -> None:
    """Legacy migration produces an empty ``global_bundle`` because legacy
    checkpoints contain only source entries; missing bundle keys are
    expected, not unknown."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_checkpoint(
        run_dir,
        {
            "a.osm.pbf": {"polygon_sha256": "0" * 64},
        },
    )

    checkpoint = load_upload_checkpoint(run_dir)

    assert checkpoint["global_bundle"] == {}


def test_checkpoint_v2_schema_version_is_literal_v2() -> None:
    """``CheckpointV2.schema_version`` is statically ``Literal["v2"]``."""
    from typing import get_type_hints

    from osm_polygon_website_tag.publishing.incremental import CheckpointV2

    hints = get_type_hints(CheckpointV2)
    assert hints["schema_version"] == Literal["v2"]


def test_load_upload_checkpoint_distinguishes_missing_from_null_schema_version(
    tmp_path: Path,
) -> None:
    """A checkpoint that omits ``schema_version`` is the legacy case and
    migrates; a checkpoint that explicitly sets ``schema_version: null``
    is rejected."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    legacy_path = run_dir / "manifests" / "uploaded_polygons.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    # Two siblings: one omits the key, one sets it to null.
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    (missing_dir / "manifests").mkdir()
    (missing_dir / "manifests" / "uploaded_polygons.json").write_text(
        json.dumps({"a.osm.pbf": {"polygon_sha256": "0" * 64}}),
        encoding="utf-8",
    )

    null_dir = tmp_path / "null"
    null_dir.mkdir()
    (null_dir / "manifests").mkdir()
    (null_dir / "manifests" / "uploaded_polygons.json").write_text(
        json.dumps({"schema_version": None, "a.osm.pbf": {"polygon_sha256": "0" * 64}}),
        encoding="utf-8",
    )

    migrated = load_upload_checkpoint(missing_dir)
    assert migrated["schema_version"] == "v2"

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        load_upload_checkpoint(null_dir)


def test_reconcile_upload_checkpoint_rejects_malformed_remote_hashes_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed remote SHA-256 fails reconciliation with the documented
    :class:`ValueError`, and ``uploaded_polygons.json`` is **not** written
    (or rewritten); the existing file, if any, remains byte-identical."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    checkpoint_path = run_dir / "manifests" / "uploaded_polygons.json"
    baseline_bytes = json.dumps(
        {
            "schema_version": "v2",
            "global_bundle": {},
            "sources": {"existing.osm.pbf": {"polygon_sha256": "f" * 64}},
        }
    )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(baseline_bytes, encoding="utf-8")

    valid_hash = "a" * 64
    monkeypatch.setattr(
        "osm_polygon_website_tag.publishing.incremental.remote_polygon_shard_hashes",
        lambda **_kwargs: {
            "good.osm.pbf": valid_hash,
            "bad.osm.pbf": "NOT-LOWER-HEX",
        },
    )

    with pytest.raises(ValueError, match="invalid uploaded polygon checkpoint"):
        reconcile_upload_checkpoint(
            run_dir,
            repo_id="owner/dataset",
            token="token",
        )

    assert checkpoint_path.read_text(encoding="utf-8") == baseline_bytes

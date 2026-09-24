"""Tests for the primitives both staged Grid'5000 stages share."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from osm_polygon_website_tag.pipeline import grid5000_bundle
from osm_polygon_website_tag.runtime.run_state import (
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_COMPLETE,
    STATUS_ENRICHED,
    STATUS_ENRICHING,
    STATUS_EXTRACTED,
    RunState,
)


def test_required_bool_names_the_field_it_rejects() -> None:
    assert grid5000_bundle.required_bool({"completed": True}, "completed") is True
    assert grid5000_bundle.required_bool({"completed": False}, "completed") is False

    with pytest.raises(ValueError, match=r"^completed must be a boolean$"):
        grid5000_bundle.required_bool({"completed": "true"}, "completed")
    with pytest.raises(ValueError, match=r"^changed must be a boolean$"):
        grid5000_bundle.required_bool({}, "changed")


def test_receipt_digest_is_short_and_key_order_independent() -> None:
    digest = grid5000_bundle.receipt_digest({"b": 1, "a": 2})

    assert len(digest) == 16
    assert digest == grid5000_bundle.receipt_digest({"a": 2, "b": 1})
    assert digest != grid5000_bundle.receipt_digest({"a": 2, "b": 3})
    assert digest == hashlib.sha256(b'{"a":2,"b":1}').hexdigest()[:16]


def test_reject_frozen_snapshot_names_the_action(tmp_path: Path) -> None:
    frozen = RunState(tmp_path, "run", {"status": STATUS_COMPLETE, "snapshot_status": "done"})
    with pytest.raises(ValueError, match=r"^cannot sync things into a frozen snapshot$"):
        grid5000_bundle.reject_frozen_snapshot(frozen, action="sync things into")

    for metadata in (
        {"status": STATUS_COMPLETE, "snapshot_status": "pending"},
        {"status": STATUS_COMPLETE},
        {"status": STATUS_ENRICHED, "snapshot_status": "done"},
    ):
        state = RunState(tmp_path, "run", metadata)
        assert grid5000_bundle.reject_frozen_snapshot(state, action="x") is None


@pytest.mark.parametrize(
    "status", [STATUS_EXTRACTED, STATUS_ANALYZED, STATUS_CARD_BUILT, STATUS_COMPLETE]
)
def test_prepare_stage_run_state_enters_enriching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    state = RunState(tmp_path, "run", {"status": status})
    transitions: list[tuple[RunState, str]] = []
    monkeypatch.setattr(
        grid5000_bundle,
        "transition_status",
        lambda actual, new_status: transitions.append((actual, new_status)),
    )

    grid5000_bundle.prepare_stage_run_state(state, action="add x to")

    assert transitions == [(state, STATUS_ENRICHING)]


@pytest.mark.parametrize("status", [STATUS_ENRICHING, STATUS_ENRICHED])
def test_prepare_stage_run_state_keeps_an_enriching_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    state = RunState(tmp_path, "run", {"status": status})
    monkeypatch.setattr(grid5000_bundle, "transition_status", pytest.fail)

    grid5000_bundle.prepare_stage_run_state(state, action="add x to")

    assert state.metadata == {"status": status}


def test_prepare_stage_run_state_rejects_frozen_and_unknown_runs(tmp_path: Path) -> None:
    frozen = RunState(tmp_path, "run", {"status": STATUS_COMPLETE, "snapshot_status": "done"})
    with pytest.raises(ValueError, match=r"^cannot add x to a frozen snapshot$"):
        grid5000_bundle.prepare_stage_run_state(frozen, action="add x to")
    other = RunState(tmp_path, "run", {"status": "created"})
    with pytest.raises(
        ValueError, match=r"^Grid'5000 preparation requires an extracted/enriched run$"
    ):
        grid5000_bundle.prepare_stage_run_state(other, action="add x to")


def test_validate_stage_sync_state_checks_identity_freeze_and_status(tmp_path: Path) -> None:
    for status in (STATUS_ENRICHING, STATUS_ENRICHED):
        state = RunState(tmp_path, "run", {"status": status})
        assert grid5000_bundle.validate_stage_sync_state(state, "run", action="x") is None
    enriching = RunState(tmp_path, "run", {"status": STATUS_ENRICHING})
    with pytest.raises(ValueError, match=r"^bundle run identity does not match target run$"):
        grid5000_bundle.validate_stage_sync_state(enriching, "other", action="x")
    frozen = RunState(tmp_path, "run", {"status": STATUS_COMPLETE, "snapshot_status": "done"})
    with pytest.raises(ValueError, match=r"^cannot sync x into a frozen snapshot$"):
        grid5000_bundle.validate_stage_sync_state(frozen, "run", action="sync x into")
    extracted = RunState(tmp_path, "run", {"status": STATUS_EXTRACTED})
    with pytest.raises(
        ValueError, match=r"^Grid'5000 synchronization requires an enriching/enriched run$"
    ):
        grid5000_bundle.validate_stage_sync_state(extracted, "run", action="x")


def test_install_validated_shard_promotes_and_drops_the_checkpoint(tmp_path: Path) -> None:
    local = tmp_path / "a.parquet"
    local.write_bytes(b"old")
    remote = tmp_path / "remote.parquet"
    remote.write_bytes(b"new")
    stale = tmp_path / ".a.parquet.grid5000-syncing"
    stale.write_bytes(b"stale")
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / "part").write_bytes(b"x")
    validated: list[tuple[str, bytes]] = []

    grid5000_bundle.install_validated_shard(
        local,
        remote,
        validate=lambda staged: validated.append((staged.name, staged.read_bytes())),
        checkpoint_directory=checkpoint,
    )

    assert validated == [(".a.parquet.grid5000-syncing", b"new")]
    assert local.read_bytes() == b"new"
    assert remote.read_bytes() == b"new"
    assert not stale.exists()
    assert not checkpoint.exists()


def test_install_validated_shard_keeps_local_and_checkpoint_on_rejection(tmp_path: Path) -> None:
    local = tmp_path / "a.parquet"
    local.write_bytes(b"old")
    remote = tmp_path / "remote.parquet"
    remote.write_bytes(b"bad")
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()

    def reject(_staged: Path) -> None:
        raise ValueError("rejected")

    with pytest.raises(ValueError, match=r"^rejected$"):
        grid5000_bundle.install_validated_shard(
            local, remote, validate=reject, checkpoint_directory=checkpoint
        )

    assert local.read_bytes() == b"old"
    assert not (tmp_path / ".a.parquet.grid5000-syncing").exists()
    assert checkpoint.is_dir()


def test_install_validated_shard_tolerates_a_missing_checkpoint(tmp_path: Path) -> None:
    local = tmp_path / "a.parquet"
    local.write_bytes(b"old")
    remote = tmp_path / "remote.parquet"
    remote.write_bytes(b"new")

    grid5000_bundle.install_validated_shard(
        local, remote, validate=lambda _staged: None, checkpoint_directory=tmp_path / "missing"
    )

    assert local.read_bytes() == b"new"

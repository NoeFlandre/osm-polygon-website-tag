from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts.quality import mutation_checkpoint, mutation_runner

_FILTERS = ("osm_polygon_website_tag.pipeline.checkpoint_storage.*",)
_MUTANT = "osm_polygon_website_tag.pipeline.checkpoint_storage.example__mutmut_1"
_HEAD_SHA = "a" * 40
_FINGERPRINT = "b" * 16
_SHARD_ID = "pipeline-checkpoint-storage"


def _metadata(exit_code: int | None) -> dict[str, Any]:
    return {
        "exit_code_by_key": {_MUTANT: exit_code},
        "hash_by_function_name": {"example": "function-hash"},
        "type_check_error_by_key": {},
        "durations_by_key": {_MUTANT: 1.25},
        "estimated_durations_by_key": {_MUTANT: 2.5},
    }


def _metadata_path(root: Path) -> Path:
    source = mutation_runner._source_paths_for_mutant_names(_FILTERS)[0]
    return root / "mutants" / f"{source}.meta"


def _write_metadata(root: Path, exit_code: int | None) -> Path:
    path = _metadata_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_metadata(exit_code)), encoding="utf-8")
    return path


def test_snapshot_and_restore_require_exact_identity_and_valid_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    metadata_path = _write_metadata(root, 0)
    checkpoint_dir = tmp_path / "checkpoint"

    included = mutation_checkpoint.snapshot_checkpoint(
        root=root,
        destination=checkpoint_dir,
        head_sha=_HEAD_SHA,
        runner_fingerprint=_FINGERPRINT,
        shard_id=_SHARD_ID,
        filters=_FILTERS,
    )

    assert len(included) == 2
    metadata_path.write_text(json.dumps(_metadata(None)), encoding="utf-8")
    assert (
        mutation_checkpoint.restore_checkpoint(
            root=root,
            checkpoint_dir=checkpoint_dir,
            head_sha=_HEAD_SHA,
            runner_fingerprint=_FINGERPRINT,
            shard_id=_SHARD_ID,
            filters=_FILTERS,
        )
        == 1
    )
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["exit_code_by_key"][_MUTANT] == 0

    metadata_path.write_text(json.dumps(_metadata(None)), encoding="utf-8")
    assert (
        mutation_checkpoint.restore_checkpoint(
            root=root,
            checkpoint_dir=checkpoint_dir,
            head_sha="c" * 40,
            runner_fingerprint=_FINGERPRINT,
            shard_id=_SHARD_ID,
            filters=_FILTERS,
        )
        == 0
    )
    restored_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert restored_metadata["exit_code_by_key"][_MUTANT] is None


def test_snapshot_rejects_symlinked_mutmut_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    path = _metadata_path(root)
    path.parent.mkdir(parents=True)
    external_metadata = tmp_path / "valid.meta"
    external_metadata.write_text(json.dumps(_metadata(0)), encoding="utf-8")
    path.symlink_to(external_metadata)
    destination = tmp_path / "checkpoint"

    included = mutation_checkpoint.snapshot_checkpoint(
        root=root,
        destination=destination,
        head_sha=_HEAD_SHA,
        runner_fingerprint=_FINGERPRINT,
        shard_id=_SHARD_ID,
        filters=_FILTERS,
    )

    assert included == (destination / "manifest.json",)


def test_snapshot_ignores_partial_or_nonregular_mutmut_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    path = _write_metadata(root, 0)
    path.write_text('{"exit_code_by_key":', encoding="utf-8")
    destination = tmp_path / "checkpoint"

    included = mutation_checkpoint.snapshot_checkpoint(
        root=root,
        destination=destination,
        head_sha=_HEAD_SHA,
        runner_fingerprint=_FINGERPRINT,
        shard_id=_SHARD_ID,
        filters=_FILTERS,
    )

    assert included == (destination / "manifest.json",)


def test_metadata_write_keeps_previous_file_if_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mutants" / "example.meta"
    target.parent.mkdir()
    target.write_bytes(b"previous complete metadata")

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated rename failure"):
        mutation_checkpoint._atomic_write_bytes(target, b"new metadata")

    assert target.read_bytes() == b"previous complete metadata"
    assert list(target.parent.iterdir()) == [target]


def test_github_api_rejects_non_https_urls() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        mutation_checkpoint._api_json("file:///etc/passwd", "token")


def test_checkpoint_artifact_map_selects_latest_unexpired_matching_shard() -> None:
    artifacts = [
        {
            "name": f"mutation-checkpoint-{'a' * 40}-{'b' * 16}-{_SHARD_ID}-run12-attempt1-seq1",
            "created_at": "2026-10-14T10:00:00Z",
            "expired": False,
        },
        {
            "name": f"mutation-checkpoint-{'a' * 40}-{'b' * 16}-{_SHARD_ID}-run12-attempt1-seq3",
            "created_at": "2026-10-14T11:00:00Z",
            "expired": False,
        },
        {
            "name": f"mutation-checkpoint-{'a' * 40}-{'b' * 16}-{_SHARD_ID}-run12-attempt1-seq4",
            "created_at": "2026-10-14T12:00:00Z",
            "expired": True,
        },
    ]

    mapped = mutation_checkpoint.checkpoint_artifact_map(
        artifacts,
        head_sha=_HEAD_SHA,
        shard_ids={_SHARD_ID},
        run_id=12,
    )

    assert mapped[_SHARD_ID] == [
        {
            "fingerprint": _FINGERPRINT,
            "name": (
                f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt1-seq3"
            ),
            "sequence": "3",
        }
    ]
    assert (
        mutation_checkpoint.select_checkpoint_artifact(
            mapped, shard_id=_SHARD_ID, runner_fingerprint=_FINGERPRINT
        )
        == mapped[_SHARD_ID][0]["name"]
    )
    assert (
        mutation_checkpoint.select_checkpoint_artifact(
            mapped, shard_id=_SHARD_ID, runner_fingerprint="c" * 16
        )
        == ""
    )


@pytest.mark.parametrize(
    ("latest_conclusion", "expected_run_id"),
    [("failure", 12), ("success", None)],
)
def test_resume_run_uses_latest_completed_run_for_the_exact_sha(
    latest_conclusion: str, expected_run_id: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = {
        "workflow_runs": [
            {
                "id": 11,
                "head_sha": _HEAD_SHA,
                "status": "completed",
                "conclusion": "failure",
                "run_number": 4,
            },
            {
                "id": 12,
                "head_sha": _HEAD_SHA,
                "status": "completed",
                "conclusion": latest_conclusion,
                "run_number": 5,
            },
            {
                "id": 13,
                "head_sha": "c" * 40,
                "status": "completed",
                "conclusion": "failure",
                "run_number": 6,
            },
            {
                "id": 14,
                "head_sha": _HEAD_SHA,
                "status": "in_progress",
                "conclusion": None,
                "run_number": 7,
            },
        ]
    }
    requested: list[str] = []

    def api_json(url: str, token: str) -> dict[str, Any]:
        requested.append(url)
        assert token == "token"
        return response

    monkeypatch.setattr(mutation_checkpoint, "_api_json", api_json)

    run_id = mutation_checkpoint._find_resume_run(
        api_url="https://api.github.test",
        repository="owner/repo",
        token="token",
        current_run_id=99,
        current_run_attempt=1,
        head_sha=_HEAD_SHA,
    )

    assert run_id == expected_run_id
    assert len(requested) == 1
    assert f"head_sha={_HEAD_SHA}" in requested[0]


def test_resume_run_uses_prior_attempt_of_current_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_api_request(_url: str, _token: str) -> dict[str, Any]:
        raise AssertionError("a rerun should reuse artifacts from its current run ID")

    monkeypatch.setattr(mutation_checkpoint, "_api_json", unexpected_api_request)

    run_id = mutation_checkpoint._find_resume_run(
        api_url="https://api.github.test",
        repository="owner/repo",
        token="token",
        current_run_id=42,
        current_run_attempt=2,
        head_sha=_HEAD_SHA,
    )

    assert run_id == 42


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        self.returncode = -signum

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        self.killed = True
        self.returncode = -signal.SIGKILL


def test_periodic_checkpoint_publishes_while_mutmut_is_running() -> None:
    clock = [0.0]
    child = _FakeProcess()
    published: list[tuple[int, bool]] = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= 3:
            child.returncode = 0

    def publish(sequence: int) -> bool:
        published.append((sequence, child.poll() is None))
        return True

    result = mutation_checkpoint.run_with_periodic_checkpoints(
        ["mutmut"],
        interval_seconds=2,
        publish=publish,
        cwd=Path.cwd(),
        execution=mutation_checkpoint._CheckpointExecution(
            process_factory=lambda *_args, **_kwargs: child,
            monotonic=lambda: clock[0],
            sleep=sleep,
        ),
    )

    assert result == 0
    assert published == [(1, True), (2, False)]


def test_signal_during_final_publication_returns_cancelled_status() -> None:
    child = _FakeProcess()
    child.returncode = 0
    published: list[int] = []

    def publish(sequence: int) -> bool:
        published.append(sequence)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return True

    result = mutation_checkpoint.run_with_periodic_checkpoints(
        ["mutmut"],
        interval_seconds=1,
        publish=publish,
        cwd=Path.cwd(),
        execution=mutation_checkpoint._CheckpointExecution(
            process_factory=lambda *_args, **_kwargs: child,
        ),
    )

    assert result == 128 + signal.SIGTERM
    assert published == [1]


def test_signal_during_publication_finishes_upload_then_publishes_final_state() -> None:
    child = _FakeProcess()
    published: list[int] = []
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    def publish(sequence: int) -> bool:
        published.append(sequence)
        if sequence == 1:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        return True

    result = mutation_checkpoint.run_with_periodic_checkpoints(
        ["mutmut"],
        interval_seconds=1,
        publish=publish,
        cwd=Path.cwd(),
        execution=mutation_checkpoint._CheckpointExecution(
            process_factory=lambda *_args, **_kwargs: child,
            monotonic=lambda: elapsed[0],
            sleep=sleep,
        ),
    )

    assert result == 128 + signal.SIGTERM
    assert child.signals == [signal.SIGINT]
    assert published == [1, 2, 3]


def test_process_start_failure_restores_signal_handlers() -> None:
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def fail_start(*args: Any, **kwargs: Any) -> _FakeProcess:
        del args, kwargs
        raise RuntimeError("start failed")

    with pytest.raises(RuntimeError, match="start failed"):
        mutation_checkpoint.run_with_periodic_checkpoints(
            ["mutmut"],
            interval_seconds=1,
            publish=lambda _sequence: True,
            cwd=Path.cwd(),
            execution=mutation_checkpoint._CheckpointExecution(process_factory=fail_start),
        )

    assert {signum: signal.getsignal(signum) for signum in before} == before


def test_cancellation_fails_child_after_bounded_wait_and_keeps_checkpoint() -> None:
    class SlowProcess(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("mutmut", timeout)
            return super().wait(timeout)

    child = SlowProcess()
    published: list[tuple[int, bool]] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= 1:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)

    result = mutation_checkpoint.run_with_periodic_checkpoints(
        ["mutmut"],
        interval_seconds=2,
        publish=lambda sequence: published.append((sequence, child.poll() is None)) or True,
        cwd=Path.cwd(),
        execution=mutation_checkpoint._CheckpointExecution(
            process_factory=lambda *_args, **_kwargs: child,
            monotonic=lambda: clock[0],
            sleep=sleep,
        ),
    )

    assert result == 128 + signal.SIGINT
    assert published == [(1, True), (2, False)]
    assert child.terminated


def test_mutation_runner_atomic_save_writes_mutmut_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mutmut.mutation.data import SourceFileMutationData

    monkeypatch.chdir(tmp_path)
    data = SourceFileMutationData(path="src/example.py")
    data.exit_code_by_key = {_MUTANT: 0}
    data.hash_by_function_name = {"example": "function-hash"}
    data.type_check_error_by_key = {}
    data.durations_by_key = {_MUTANT: 1.25}
    data.estimated_time_of_tests_by_mutant = {_MUTANT: 2.5}

    mutation_runner._atomic_mutation_data_save(data)

    saved = json.loads(data.meta_path.read_text(encoding="utf-8"))
    assert saved["exit_code_by_key"] == {_MUTANT: 0}
    assert saved["durations_by_key"] == {_MUTANT: 1.25}
    assert saved["estimated_durations_by_key"] == {_MUTANT: 2.5}
    assert not list(data.meta_path.parent.glob("*.tmp"))


def test_resume_hook_skips_only_selected_completed_mutants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mutmut.mutation.data import SourceFileMutationData

    original_save = SourceFileMutationData.save
    monkeypatch.setattr(SourceFileMutationData, "save", original_save)
    completed = (object(), _MUTANT, 0)
    unfinished = (object(), _MUTANT + "_2", None)
    data_by_path = {"src/example.py": object()}
    fake_main = SimpleNamespace(
        collect_source_file_mutation_data=lambda *, mutant_names: (
            [completed, unfinished],
            data_by_path,
        )
    )

    mutation_runner._install_mutation_checkpoint_hooks(fake_main)
    monkeypatch.setenv("MUTATION_RESUME_COMPLETED", "1")
    selected, returned_data = fake_main.collect_source_file_mutation_data(mutant_names=_FILTERS)

    assert selected == [unfinished]
    assert returned_data is data_by_path
    monkeypatch.delenv("MUTATION_RESUME_COMPLETED")
    selected, _ = fake_main.collect_source_file_mutation_data(mutant_names=_FILTERS)
    assert selected == [completed, unfinished]

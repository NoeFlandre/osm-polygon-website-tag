from __future__ import annotations

import json
import math
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Any, cast

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


def test_snapshot_requires_a_selected_mutant_to_have_a_verdict() -> None:
    metadata = _metadata(None)

    assert not mutation_checkpoint._has_selected_result(metadata, _FILTERS)

    metadata["exit_code_by_key"][_MUTANT] = 0

    assert mutation_checkpoint._has_selected_result(metadata, _FILTERS)


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
    [("failure", 12), ("cancelled", 12), ("timed_out", 12), ("success", None)],
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
    published: list[tuple[int, bool, float]] = []
    sleep_calls: list[float] = []

    def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds
        if clock[0] >= 3:
            child.returncode = 0

    def publish(sequence: int) -> bool:
        published.append((sequence, child.poll() is None, clock[0]))
        if len(published) == 2:
            child.returncode = 0
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
    assert published == [(1, True, 2.0), (2, False, 3.0)]
    assert sleep_calls == [1.0, 1.0, 1.0]


def test_periodic_checkpoints_follow_the_clock_after_each_publication() -> None:
    clock = [0.0]
    child = _FakeProcess()
    published: list[tuple[int, float]] = []
    sleep_calls: list[float] = []

    def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds if seconds else 0.1

    def publish(sequence: int) -> bool:
        published.append((sequence, clock[0]))
        if len(published) == 2:
            child.returncode = 0
        return True

    mutation_checkpoint._run_periodic_checkpoint_loop(
        mutation_checkpoint._CheckpointState(process=child),
        interval_seconds=0.5,
        publish=publish,
        execution=mutation_checkpoint._CheckpointExecution(
            monotonic=lambda: clock[0],
            sleep=sleep,
        ),
    )

    assert published == [(1, 0.5), (2, 1.0)]
    assert sleep_calls == [0.5, 0.5]


def test_signal_during_final_publication_returns_cancelled_status() -> None:
    child = _FakeProcess()
    child.returncode = 0
    published: list[int] = []

    def publish(sequence: int) -> bool:
        published.append(sequence)
        handler = cast(Callable[[int, FrameType | None], Any], signal.getsignal(signal.SIGTERM))
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
        if len(published) == 1:
            handler = cast(Callable[[int, FrameType | None], Any], signal.getsignal(signal.SIGTERM))
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

    sleep_calls: list[float] = []

    def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock[0] += seconds
        if clock[0] >= 1:
            handler = cast(Callable[[int, FrameType | None], Any], signal.getsignal(signal.SIGINT))
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
    assert sleep_calls == [1.0]
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


def test_resume_hook_skips_completed_and_retries_interrupted_or_unchecked_mutants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mutmut.mutation.data import SourceFileMutationData

    original_save = SourceFileMutationData.save
    monkeypatch.setattr(SourceFileMutationData, "save", original_save)
    completed = (object(), _MUTANT, 0)
    unfinished = (object(), _MUTANT + "_2", None)
    interrupted = (object(), _MUTANT + "_3", 2)
    data_by_path = {"src/example.py": object()}
    fake_main = SimpleNamespace(
        collect_source_file_mutation_data=lambda *, mutant_names: (
            [completed, unfinished, interrupted],
            data_by_path,
        )
    )

    mutation_runner._install_mutation_checkpoint_hooks(fake_main)
    monkeypatch.setenv("MUTATION_RESUME_COMPLETED", "1")
    selected, returned_data = fake_main.collect_source_file_mutation_data(mutant_names=_FILTERS)

    assert selected == [unfinished, interrupted]
    assert returned_data is data_by_path
    monkeypatch.delenv("MUTATION_RESUME_COMPLETED")
    selected, _ = fake_main.collect_source_file_mutation_data(mutant_names=_FILTERS)
    assert selected == [completed, unfinished, interrupted]


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {"durations_by_key": {}, "estimated_durations_by_key": {}},
        {**_metadata(0), "unknown": True},
        {**_metadata(0), "exit_code_by_key": {"mutant": True}},
        {**_metadata(0), "hash_by_function_name": {"function": 7}},
        {**_metadata(0), "type_check_error_by_key": {"mutant": 7}},
        {**_metadata(0), "durations_by_key": {"mutant": math.inf}},
        {**_metadata(0), "estimated_durations_by_key": {"mutant": True}},
    ],
)
def test_metadata_validator_rejects_invalid_shapes(invalid: object) -> None:
    assert not mutation_checkpoint._valid_metadata(invalid)


def test_metadata_validator_accepts_optional_fields_and_valid_nulls() -> None:
    metadata = _metadata(None)
    metadata.pop("hash_by_function_name")
    metadata.pop("type_check_error_by_key")

    assert mutation_checkpoint._valid_metadata(metadata)
    assert mutation_checkpoint._valid_exit_code_mapping({"mutant": None, "done": 0})
    assert mutation_checkpoint._valid_error_mapping({"mutant": None, "other": "type error"})
    assert mutation_checkpoint._valid_string_mapping({"function": "hash"})


@pytest.mark.parametrize(
    "invalid",
    [
        [],
        {1: 1.0},
        {"mutant": True},
        {"mutant": "1.0"},
        {"mutant": math.nan},
        {"mutant": math.inf},
        {"mutant": 10**5000},
    ],
)
def test_finite_duration_mapping_rejects_invalid_values(invalid: object) -> None:
    assert not mutation_checkpoint._finite_duration_mapping(invalid)


def test_finite_duration_mapping_accepts_empty_and_finite_values() -> None:
    assert mutation_checkpoint._finite_duration_mapping({})
    assert mutation_checkpoint._finite_duration_mapping({"one": 1, "two": 0.5})


def test_restore_rejects_non_utf8_manifest(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    manifest = checkpoint_dir / "manifest.json"
    manifest.write_bytes(
        json.dumps(
            {
                "schema_version": mutation_checkpoint._SCHEMA_VERSION,
                "head_sha": _HEAD_SHA,
                "runner_fingerprint": _FINGERPRINT,
                "shard_id": _SHARD_ID,
                "filters": _FILTERS,
            }
        ).encode("utf-16")
    )

    assert (
        mutation_checkpoint.restore_checkpoint(
            root=root,
            checkpoint_dir=checkpoint_dir,
            head_sha=_HEAD_SHA,
            runner_fingerprint=_FINGERPRINT,
            shard_id=_SHARD_ID,
            filters=_FILTERS,
        )
        == 0
    )


def test_restore_rejects_unreadable_or_malformed_manifest(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    manifest = checkpoint_dir / "manifest.json"

    assert (
        mutation_checkpoint.restore_checkpoint(
            root=root,
            checkpoint_dir=checkpoint_dir,
            head_sha=_HEAD_SHA,
            runner_fingerprint=_FINGERPRINT,
            shard_id=_SHARD_ID,
            filters=_FILTERS,
        )
        == 0
    )
    manifest.write_text("not json", encoding="utf-8")
    assert (
        mutation_checkpoint.restore_checkpoint(
            root=root,
            checkpoint_dir=checkpoint_dir,
            head_sha=_HEAD_SHA,
            runner_fingerprint=_FINGERPRINT,
            shard_id=_SHARD_ID,
            filters=_FILTERS,
        )
        == 0
    )


@pytest.mark.parametrize(
    "artifact",
    [
        {"expired": True, "name": "anything"},
        {"expired": False, "name": None},
        {"expired": False, "name": "not-a-checkpoint"},
        {
            "expired": False,
            "name": f"mutation-checkpoint-{'c' * 40}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt1-seq1",
        },
        {
            "expired": False,
            "name": f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run13-attempt1-seq1",
        },
        {
            "expired": False,
            "name": f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-other-shard-run12-attempt1-seq1",
        },
    ],
)
def test_checkpoint_artifact_map_ignores_nonmatching_artifacts(artifact: dict[str, Any]) -> None:
    assert (
        mutation_checkpoint.checkpoint_artifact_map(
            [artifact], head_sha=_HEAD_SHA, shard_ids={_SHARD_ID}, run_id=12
        )
        == {}
    )


def test_github_api_json_sends_request_and_decodes_object(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def read(self) -> bytes:
            return b'{"artifacts": []}'

    class Connection:
        def __init__(self) -> None:
            self.request_args: tuple[Any, ...] = ()
            self.closed = False

        def request(self, *args: Any, **kwargs: Any) -> None:
            self.request_args = (*args, kwargs)

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    created: list[tuple[Any, ...]] = []

    def connect(host: str, port: int | None, *, timeout: int) -> Connection:
        created.append((host, port, timeout))
        return connection

    monkeypatch.setattr(mutation_checkpoint, "HTTPSConnection", connect)

    response = mutation_checkpoint._api_json("https://api.github.test:8443/repos?a=1", "token")

    assert response == {"artifacts": []}
    assert created == [("api.github.test", 8443, 30)]
    assert connection.request_args == (
        "GET",
        "/repos?a=1",
        {
            "headers": {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer token",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        },
    )
    assert connection.closed


def test_api_request_target_uses_root_path_and_preserves_query() -> None:
    assert mutation_checkpoint._api_request_target("https://api.github.test?per_page=100") == (
        "api.github.test",
        "/?per_page=100",
        None,
    )


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (400, b"{}", "HTTP 400"),
        (403, b"{}", "HTTP 403"),
        (200, b"[]", "^GitHub Actions API returned an unexpected response$"),
    ],
)
def test_github_api_json_rejects_error_and_nonobject_responses(
    monkeypatch: pytest.MonkeyPatch, status: int, payload: bytes, message: str
) -> None:
    class Response:
        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return payload

    class Connection:
        closed = False

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(
        mutation_checkpoint,
        "HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(RuntimeError, match=message):
        mutation_checkpoint._api_json("https://api.github.test/repos", "token")
    assert connection.closed


def test_list_run_artifacts_paginates_and_filters_items(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        {"artifacts": [{"name": "first"}, "invalid"], "total_count": 2},
        {"artifacts": [{"name": "second"}], "total_count": 2},
    ]
    requests: list[tuple[str, str | None]] = []

    def api_json(url: str, token: str | None) -> dict[str, Any]:
        requests.append((url, token))
        return responses.pop(0)

    monkeypatch.setattr(mutation_checkpoint, "_api_json", api_json)

    artifacts = mutation_checkpoint._list_run_artifacts(
        api_url="https://api.github.test/",
        repository="owner/repo",
        token="token",
        run_id=12,
    )

    assert artifacts == [{"name": "first"}, {"name": "second"}]
    assert requests == [
        (
            "https://api.github.test/repos/owner/repo/actions/runs/12/artifacts?per_page=100&page=1",
            "token",
        ),
        (
            "https://api.github.test/repos/owner/repo/actions/runs/12/artifacts?per_page=100&page=2",
            "token",
        ),
    ]


def test_list_run_artifacts_preserves_api_url_path_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def api_json(url: str, _token: str) -> dict[str, Any]:
        if requested:
            raise AssertionError("empty artifact pages should stop pagination")
        requested.append(url)
        return {"artifacts": [], "total_count": 0}

    monkeypatch.setattr(mutation_checkpoint, "_api_json", api_json)

    assert (
        mutation_checkpoint._list_run_artifacts(
            api_url="https://api.github.test/X",
            repository="owner/repo",
            token="token",
            run_id=12,
        )
        == []
    )
    assert requested == [
        "https://api.github.test/X/repos/owner/repo/actions/runs/12/artifacts?per_page=100&page=1"
    ]


def test_list_run_artifacts_uses_default_count_and_rejects_bad_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def api_json(url: str, _token: str) -> dict[str, Any]:
        if requested:
            raise AssertionError("empty artifact pages should stop pagination")
        requested.append(url)
        return {"artifacts": []}

    monkeypatch.setattr(mutation_checkpoint, "_api_json", api_json)
    assert (
        mutation_checkpoint._list_run_artifacts(
            api_url="https://api.github.test",
            repository="owner/repo",
            token="token",
            run_id=12,
        )
        == []
    )
    assert requested == [
        "https://api.github.test/repos/owner/repo/actions/runs/12/artifacts?per_page=100&page=1"
    ]
    monkeypatch.setattr(mutation_checkpoint, "_api_json", lambda *_args: {"artifacts": "bad"})
    with pytest.raises(
        RuntimeError,
        match=r"^GitHub Actions artifact list had an unexpected shape$",
    ):
        mutation_checkpoint._list_run_artifacts(
            api_url="https://api.github.test",
            repository="owner/repo",
            token="token",
            run_id=12,
        )


def test_resume_run_rejects_unexpected_workflow_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation_checkpoint, "_api_json", lambda *_args: {"workflow_runs": None})
    with pytest.raises(RuntimeError, match=r"^GitHub Actions run list had an unexpected shape$"):
        mutation_checkpoint._find_resume_run(
            api_url="https://api.github.test",
            repository="owner/repo",
            token="token",
            current_run_id=99,
            current_run_attempt=1,
            head_sha=_HEAD_SHA,
        )


def test_write_output_requires_action_output_path_and_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    with pytest.raises(RuntimeError, match=r"^GITHUB_OUTPUT is not set$"):
        mutation_checkpoint._write_output("value", "first")

    output_path = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    mutation_checkpoint._write_output("first", "one")
    mutation_checkpoint._write_output("second", "two")
    assert output_path.read_text(encoding="utf-8") == "first=one\nsecond=two\n"


@pytest.mark.parametrize("resume_run", [None, 12])
def test_plan_writes_resume_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume_run: int | None
) -> None:
    output_path = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.test")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_SHA", _HEAD_SHA)
    monkeypatch.setenv("MUTATION_SHARDS_JSON", json.dumps([{"id": _SHARD_ID}]))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setattr(mutation_checkpoint, "_find_resume_run", lambda **_kwargs: resume_run)
    listed: list[int] = []

    def list_artifacts(**kwargs: Any) -> list[dict[str, Any]]:
        listed.append(kwargs["run_id"])
        return [
            {
                "name": f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt1-seq3",
                "created_at": "2026-10-15T12:00:00Z",
                "expired": False,
            }
        ]

    monkeypatch.setattr(mutation_checkpoint, "_list_run_artifacts", list_artifacts)

    mutation_checkpoint._plan()

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"previous_run_id={resume_run or ''}"
    checkpoint_map = json.loads(lines[1].removeprefix("checkpoint_artifacts="))
    assert lines[1] == ("checkpoint_artifacts=" + json.dumps(checkpoint_map, separators=(",", ":")))
    assert listed == ([] if resume_run is None else [resume_run])
    assert (_SHARD_ID in checkpoint_map) is (resume_run is not None)


def _set_publish_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", _HEAD_SHA)
    monkeypatch.setenv("MUTATION_RUNNER_FINGERPRINT", _FINGERPRINT)
    monkeypatch.setenv("MUTATION_SHARD_ID", _SHARD_ID)
    monkeypatch.setenv("GITHUB_RUN_ID", "12")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")


def test_publish_skips_empty_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_publish_environment(monkeypatch)
    monkeypatch.setattr(
        mutation_checkpoint,
        "snapshot_checkpoint",
        lambda **_kwargs: (Path("manifest.json"),),
    )
    monkeypatch.setattr(
        mutation_checkpoint,
        "_start_artifact_uploader",
        lambda *_args: pytest.fail("an empty checkpoint must not be uploaded"),
    )

    assert mutation_checkpoint._publish(tmp_path, _FILTERS, 1) is False


@pytest.mark.parametrize(("attempt", "expected_attempt"), [("2", "2"), (None, "1")])
def test_publish_uploads_checkpoint_with_attempt_scoped_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempt: str | None, expected_attempt: str
) -> None:
    _set_publish_environment(monkeypatch)
    if attempt is None:
        monkeypatch.delenv("GITHUB_RUN_ATTEMPT")
    else:
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
    node = tmp_path / "node"
    node.write_text("stub", encoding="utf-8")
    node_lookups: list[str] = []
    monkeypatch.setattr(
        mutation_checkpoint.shutil,
        "which",
        lambda name: node_lookups.append(name) or str(node),
    )
    real_resolve = Path.resolve
    resolved_strictly: list[bool] = []

    def resolve(path: Path, *, strict: bool = False) -> Path:
        if path == node:
            resolved_strictly.append(strict)
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    snapshot_calls: list[dict[str, Any]] = []

    def snapshot(**kwargs: Any) -> tuple[Path, ...]:
        snapshot_calls.append(kwargs)
        return (Path("manifest.json"), Path("mutants/source.meta"))

    monkeypatch.setattr(mutation_checkpoint, "snapshot_checkpoint", snapshot)
    captured: list[tuple[list[str], Path]] = []

    class Uploaded:
        def wait(self) -> int:
            return 0

    def start(command: list[str], root: Path) -> Uploaded:
        assert Path(command[3]).is_dir()
        captured.append((command, root))
        return Uploaded()

    monkeypatch.setattr(mutation_checkpoint, "_start_artifact_uploader", start)

    assert mutation_checkpoint._publish(tmp_path, _FILTERS, 3) is True
    command, root = captured[0]
    assert root == tmp_path
    assert node_lookups == ["node"]
    assert snapshot_calls[0]["root"] == tmp_path
    assert snapshot_calls[0]["head_sha"] == _HEAD_SHA
    assert snapshot_calls[0]["runner_fingerprint"] == _FINGERPRINT
    assert snapshot_calls[0]["shard_id"] == _SHARD_ID
    assert snapshot_calls[0]["filters"] == _FILTERS
    assert not snapshot_calls[0]["destination"].exists()
    assert snapshot_calls[0]["destination"].name.startswith("mutation-checkpoint-")
    assert resolved_strictly == [True]
    assert command == [
        str(real_resolve(node, strict=True)),
        str(tmp_path / "scripts/quality/mutation-artifact-uploader/bundle/index.mjs"),
        f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt{expected_attempt}-seq3",
        str(snapshot_calls[0]["destination"]),
    ]


def test_publish_reports_missing_node_and_failed_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_publish_environment(monkeypatch)
    monkeypatch.setattr(
        mutation_checkpoint,
        "snapshot_checkpoint",
        lambda **_kwargs: (Path("manifest.json"), Path("mutants/source.meta")),
    )
    monkeypatch.setattr(mutation_checkpoint.shutil, "which", lambda _name: None)
    with pytest.raises(
        RuntimeError,
        match=r"^Node\.js is required to upload mutation checkpoints$",
    ):
        mutation_checkpoint._publish(tmp_path, _FILTERS, 1)

    node = tmp_path / "node"
    node.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(mutation_checkpoint.shutil, "which", lambda _name: str(node))

    class FailedUpload:
        def wait(self) -> int:
            return 1

    monkeypatch.setattr(
        mutation_checkpoint,
        "_start_artifact_uploader",
        lambda *_args: FailedUpload(),
    )
    with pytest.raises(RuntimeError, match=r"^checkpoint artifact upload failed$"):
        mutation_checkpoint._publish(tmp_path, _FILTERS, 1)


def test_plan_command_calls_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["mutation_checkpoint.py", "plan"])
    called: list[bool] = []
    monkeypatch.setattr(mutation_checkpoint, "_plan", lambda: called.append(True))

    mutation_checkpoint.main()

    assert called == [True]


def test_restore_command_prints_restored_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation_checkpoint.py",
            "restore",
            "--checkpoint-dir",
            str(tmp_path),
            "--head-sha",
            _HEAD_SHA,
            "--runner-fingerprint",
            _FINGERPRINT,
            "--shard-id",
            _SHARD_ID,
            *_FILTERS,
        ],
    )
    monkeypatch.setattr(mutation_checkpoint, "restore_checkpoint", lambda **_kwargs: 2)

    mutation_checkpoint.main()

    assert capsys.readouterr().out == "Restored 2 mutation metadata files\n"


def test_select_artifact_command_prints_selected_artifact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_name = "mutation-checkpoint-selected"
    checkpoint_map = {_SHARD_ID: [{"fingerprint": _FINGERPRINT, "name": artifact_name}]}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation_checkpoint.py",
            "select-artifact",
            "--artifacts-json",
            json.dumps(checkpoint_map),
            "--shard-id",
            _SHARD_ID,
            "--runner-fingerprint",
            _FINGERPRINT,
        ],
    )

    mutation_checkpoint.main()

    assert capsys.readouterr().out == f"{artifact_name}\n"


def test_run_command_exits_with_runner_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation_checkpoint.py",
            "run",
            "--max-children",
            "3",
            "--interval-minutes",
            "4",
            *_FILTERS,
        ],
    )
    monkeypatch.setattr(mutation_checkpoint, "_run", lambda max_children, interval, filters: 17)

    with pytest.raises(SystemExit) as error:
        mutation_checkpoint.main()

    assert error.value.code == 17


def test_run_builds_mutmut_command_and_enables_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}

    def run_with_checkpoints(command: list[str], **kwargs: Any) -> int:
        captured["command"] = command
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(mutation_checkpoint, "run_with_periodic_checkpoints", run_with_checkpoints)

    result = mutation_checkpoint._run(3, 2.5, _FILTERS)

    assert result == 7
    assert captured["command"] == [
        sys.executable,
        str(tmp_path / "scripts/quality/mutation_runner.py"),
        "run",
        "--max-children",
        "3",
        *_FILTERS,
    ]
    assert captured["interval_seconds"] == 150
    assert captured["cwd"] == tmp_path
    assert captured["execution"].environment["MUTATION_RESUME_COMPLETED"] == "1"
    published: list[tuple[Path, tuple[str, ...], int]] = []
    monkeypatch.setattr(
        mutation_checkpoint,
        "_publish",
        lambda root, filters, sequence: published.append((root, tuple(filters), sequence)) or True,
    )
    assert callable(captured["publish"])
    assert captured["publish"](9) is True
    assert published == [(tmp_path, _FILTERS, 9)]


def test_stop_process_handles_lookup_race_and_escalates_to_kill() -> None:
    class DisappearingProcess(_FakeProcess):
        def send_signal(self, signum: int) -> None:
            del signum
            raise ProcessLookupError

    mutation_checkpoint._stop_process(DisappearingProcess())

    class UnresponsiveProcess(_FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("mutmut", timeout)
            return super().wait(timeout)

    child = UnresponsiveProcess()
    mutation_checkpoint._stop_process(child)
    assert child.terminated
    assert child.killed


def test_snapshot_skips_bad_metadata_and_reuses_nested_destination(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    filters = (
        "osm_polygon_website_tag.application.grid5000_runner_base.*",
        _FILTERS[0],
    )
    sources = mutation_runner._source_paths_for_mutant_names(filters)
    assert len(sources) == 2
    relatives = tuple(Path("mutants") / f"{source}.meta" for source in sources)
    bad_path = root / relatives[0]
    good_path = root / relatives[1]
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("partial metadata", encoding="utf-8")
    good_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_metadata(0)).encode("utf-8")
    good_path.write_bytes(payload)
    destination = tmp_path / "nested" / "checkpoint"
    manifest = {
        "schema_version": mutation_checkpoint._SCHEMA_VERSION,
        "head_sha": _HEAD_SHA,
        "runner_fingerprint": _FINGERPRINT,
        "shard_id": _SHARD_ID,
        "filters": list(filters),
    }

    included = mutation_checkpoint.snapshot_checkpoint(
        root=root,
        destination=destination,
        head_sha=_HEAD_SHA,
        runner_fingerprint=_FINGERPRINT,
        shard_id=_SHARD_ID,
        filters=filters,
    )

    assert included == (destination / "manifest.json", destination / relatives[1])
    assert (destination / "manifest.json").read_bytes() == (
        json.dumps(manifest, sort_keys=True) + "\n"
    ).encode("utf-8")
    assert (destination / relatives[1]).read_bytes() == payload
    assert (
        mutation_checkpoint.snapshot_checkpoint(
            root=root,
            destination=destination,
            head_sha=_HEAD_SHA,
            runner_fingerprint=_FINGERPRINT,
            shard_id=_SHARD_ID,
            filters=filters,
        )
        == included
    )


def test_restore_rejects_valid_metadata_when_manifest_is_malformed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = _write_metadata(root, None)
    checkpoint_dir = tmp_path / "checkpoint"
    relative = (
        Path("mutants") / f"{mutation_runner._source_paths_for_mutant_names(_FILTERS)[0]}.meta"
    )
    checkpoint_metadata = checkpoint_dir / relative
    checkpoint_metadata.parent.mkdir(parents=True)
    checkpoint_metadata.write_text(json.dumps(_metadata(0)), encoding="utf-8")
    (checkpoint_dir / "manifest.json").write_text("not json", encoding="utf-8")

    restored = mutation_checkpoint.restore_checkpoint(
        root=root,
        checkpoint_dir=checkpoint_dir,
        head_sha=_HEAD_SHA,
        runner_fingerprint=_FINGERPRINT,
        shard_id=_SHARD_ID,
        filters=_FILTERS,
    )

    assert restored == 0
    assert json.loads(target.read_text(encoding="utf-8"))["exit_code_by_key"][_MUTANT] is None


def test_atomic_write_uses_sibling_temp_and_syncs_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mutants" / "example.meta"
    target.parent.mkdir()
    real_mkstemp = mutation_checkpoint.tempfile.mkstemp
    temp_calls: list[dict[str, Any]] = []
    sync_calls: list[int] = []

    def mkstemp(**kwargs: Any) -> tuple[int, str]:
        temp_calls.append(dict(kwargs))
        return real_mkstemp(**kwargs)

    monkeypatch.setattr(mutation_checkpoint.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(mutation_checkpoint.os, "fsync", sync_calls.append)

    mutation_checkpoint._atomic_write_bytes(target, b"complete metadata")

    assert temp_calls[0]["dir"] == target.parent
    assert temp_calls[0]["prefix"] == f".{target.name}."
    assert temp_calls[0]["suffix"] == ".tmp"
    assert len(sync_calls) == 2
    assert target.read_bytes() == b"complete metadata"
    assert not list(target.parent.glob("*.tmp"))


def test_atomic_write_keeps_original_error_when_temp_disappears_during_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mutants" / "example.meta"
    target.parent.mkdir()

    def remove_then_fail(source: Path, _destination: Path) -> None:
        source.unlink()
        raise OSError("simulated rename failure")

    monkeypatch.setattr(Path, "replace", remove_then_fail)

    with pytest.raises(OSError, match="simulated rename failure"):
        mutation_checkpoint._atomic_write_bytes(target, b"new metadata")


def test_api_request_target_rejects_https_without_a_hostname() -> None:
    with pytest.raises(ValueError, match=r"^GitHub Actions API URL must use HTTPS$"):
        mutation_checkpoint._api_request_target("https:///actions/runs")


def test_checkpoint_run_matches_requires_both_commit_and_run_identity() -> None:
    match = mutation_checkpoint._CHECKPOINT_NAME.fullmatch(
        f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt1-seq1"
    )
    assert match is not None

    assert mutation_checkpoint._checkpoint_run_matches(match, head_sha=_HEAD_SHA, run_id=12)
    assert not mutation_checkpoint._checkpoint_run_matches(match, head_sha="c" * 40, run_id=12)
    assert not mutation_checkpoint._checkpoint_run_matches(match, head_sha=_HEAD_SHA, run_id=13)


def test_artifact_page_items_defaults_to_an_empty_page() -> None:
    assert mutation_checkpoint._artifact_page_items({}) == []


def test_artifact_page_items_rejects_a_non_list_with_a_stable_error() -> None:
    with pytest.raises(
        RuntimeError,
        match=r"^GitHub Actions artifact list had an unexpected shape$",
    ):
        mutation_checkpoint._artifact_page_items({"artifacts": "bad"})


def test_select_checkpoint_artifact_returns_empty_for_a_missing_shard() -> None:
    assert (
        mutation_checkpoint.select_checkpoint_artifact(
            {}, shard_id=_SHARD_ID, runner_fingerprint=_FINGERPRINT
        )
        == ""
    )


def test_checkpoint_artifact_map_uses_attempt_then_sequence_for_equal_timestamps() -> None:
    created_at = "2026-10-14T11:00:00Z"

    def artifact(attempt: int, sequence: int) -> dict[str, Any]:
        return {
            "name": (
                f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-"
                f"run12-attempt{attempt}-seq{sequence}"
            ),
            "created_at": created_at,
            "expired": False,
        }

    attempt_one_sequence_two = artifact(1, 2)
    attempt_one_sequence_one = artifact(1, 1)
    attempt_two_sequence_one = artifact(2, 1)
    attempt_two_sequence_two = artifact(2, 2)
    artifacts = [
        attempt_one_sequence_two,
        attempt_one_sequence_one,
        attempt_two_sequence_one,
        attempt_two_sequence_one,
    ]

    for candidates in (artifacts, list(reversed(artifacts))):
        mapped = mutation_checkpoint.checkpoint_artifact_map(
            candidates, head_sha=_HEAD_SHA, shard_ids={_SHARD_ID}, run_id=12
        )

        assert mapped[_SHARD_ID] == [
            {
                "fingerprint": _FINGERPRINT,
                "name": attempt_two_sequence_one["name"],
                "sequence": "1",
            }
        ]

    mapped = mutation_checkpoint.checkpoint_artifact_map(
        [attempt_two_sequence_one, attempt_two_sequence_two],
        head_sha=_HEAD_SHA,
        shard_ids={_SHARD_ID},
        run_id=12,
    )

    assert mapped[_SHARD_ID] == [
        {
            "fingerprint": _FINGERPRINT,
            "name": attempt_two_sequence_two["name"],
            "sequence": "2",
        }
    ]


def test_checkpoint_artifact_map_prefers_creation_time_before_attempt_and_sequence() -> None:
    def artifact(attempt: int, sequence: int, created_at: str) -> dict[str, Any]:
        return {
            "name": (
                f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-"
                f"run12-attempt{attempt}-seq{sequence}"
            ),
            "created_at": created_at,
            "expired": False,
        }

    older = artifact(9, 9, "2026-10-14T10:00:00Z")
    newer = artifact(1, 1, "2026-10-14T11:00:00Z")
    mapped = mutation_checkpoint.checkpoint_artifact_map(
        [newer, older], head_sha=_HEAD_SHA, shard_ids={_SHARD_ID}, run_id=12
    )

    assert mapped[_SHARD_ID] == [
        {
            "fingerprint": _FINGERPRINT,
            "name": newer["name"],
            "sequence": "1",
        }
    ]


def test_checkpoint_artifact_map_continues_after_a_nonmatching_artifact() -> None:
    matching = {
        "name": f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt1-seq1",
        "created_at": "2026-10-14T11:00:00Z",
        "expired": False,
    }
    expired = {**matching, "expired": True}

    mapped = mutation_checkpoint.checkpoint_artifact_map(
        [expired, matching], head_sha=_HEAD_SHA, shard_ids={_SHARD_ID}, run_id=12
    )

    assert mapped[_SHARD_ID][0]["name"] == matching["name"]


@pytest.mark.parametrize("created_at", [None, "", 12])
def test_checkpoint_artifact_map_ignores_artifacts_without_a_timestamp(
    created_at: object,
) -> None:
    artifact = {
        "name": f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt1-seq1",
        "created_at": created_at,
        "expired": False,
    }

    assert (
        mutation_checkpoint.checkpoint_artifact_map(
            [artifact], head_sha=_HEAD_SHA, shard_ids={_SHARD_ID}, run_id=12
        )
        == {}
    )


def test_latest_resume_candidate_returns_none_when_no_run_matches() -> None:
    assert (
        mutation_checkpoint._latest_resume_candidate([], current_run_id=99, head_sha=_HEAD_SHA)
        is None
    )


def test_latest_resume_candidate_ignores_runs_without_identity_or_order() -> None:
    missing_id = {
        "head_sha": _HEAD_SHA,
        "status": "completed",
        "run_number": 5,
    }
    missing_run_number = {
        "id": 12,
        "head_sha": _HEAD_SHA,
        "status": "completed",
    }

    assert (
        mutation_checkpoint._latest_resume_candidate(
            [missing_id, missing_run_number], current_run_id=99, head_sha=_HEAD_SHA
        )
        is None
    )


@pytest.mark.parametrize("run_number", [None, "5", True])
def test_latest_resume_candidate_ignores_non_integer_run_numbers(run_number: object) -> None:
    run = {
        "id": 12,
        "head_sha": _HEAD_SHA,
        "status": "completed",
        "run_number": run_number,
    }

    assert (
        mutation_checkpoint._latest_resume_candidate([run], current_run_id=99, head_sha=_HEAD_SHA)
        is None
    )


@pytest.mark.parametrize("run_id", [None, "12", True])
def test_latest_resume_candidate_ignores_non_integer_run_ids(run_id: object) -> None:
    run = {
        "id": run_id,
        "head_sha": _HEAD_SHA,
        "status": "completed",
        "run_number": 5,
    }

    assert (
        mutation_checkpoint._latest_resume_candidate([run], current_run_id=99, head_sha=_HEAD_SHA)
        is None
    )


def test_latest_resume_candidate_chooses_the_largest_run_number() -> None:
    older = {
        "id": 10,
        "head_sha": _HEAD_SHA,
        "status": "completed",
        "run_number": 5,
    }
    newer = {**older, "id": 11, "run_number": 6}

    assert (
        mutation_checkpoint._latest_resume_candidate(
            [older, newer], current_run_id=99, head_sha=_HEAD_SHA
        )
        is newer
    )


def test_latest_resume_candidate_keeps_first_run_when_numbers_tie() -> None:
    first = {
        "id": 10,
        "head_sha": _HEAD_SHA,
        "status": "completed",
        "run_number": 5,
    }
    second = {**first, "id": 11}

    assert (
        mutation_checkpoint._latest_resume_candidate(
            [first, second], current_run_id=99, head_sha=_HEAD_SHA
        )
        is first
    )


def test_find_resume_run_preserves_api_url_path_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []
    monkeypatch.setattr(
        mutation_checkpoint,
        "_api_json",
        lambda url, _token: requested.append(url) or {"workflow_runs": []},
    )

    assert (
        mutation_checkpoint._find_resume_run(
            api_url="https://api.github.test/X",
            repository="owner/repo",
            token="token",
            current_run_id=99,
            current_run_attempt=1,
            head_sha=_HEAD_SHA,
        )
        is None
    )
    assert requested == [
        "https://api.github.test/X/repos/owner/repo/actions/workflows/mutation-sweep.yml/runs"
        f"?branch=main&head_sha={_HEAD_SHA}&per_page=100"
    ]


def test_find_resume_run_builds_a_single_slash_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def api_json(url: str, _token: str) -> dict[str, Any]:
        requested.append(url)
        return {"workflow_runs": []}

    monkeypatch.setattr(mutation_checkpoint, "_api_json", api_json)

    assert (
        mutation_checkpoint._find_resume_run(
            api_url="https://api.github.test/",
            repository="owner/repo",
            token="token",
            current_run_id=99,
            current_run_attempt=1,
            head_sha=_HEAD_SHA,
        )
        is None
    )
    assert requested == [
        "https://api.github.test/repos/owner/repo/actions/workflows/mutation-sweep.yml/runs"
        f"?branch=main&head_sha={_HEAD_SHA}&per_page=100"
    ]


def test_resume_run_treats_a_missing_workflow_run_list_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mutation_checkpoint, "_api_json", lambda *_args: {})

    assert (
        mutation_checkpoint._find_resume_run(
            api_url="https://api.github.test",
            repository="owner/repo",
            token="token",
            current_run_id=99,
            current_run_attempt=1,
            head_sha=_HEAD_SHA,
        )
        is None
    )


def test_resume_run_does_not_select_the_current_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = {
        "id": 99,
        "head_sha": _HEAD_SHA,
        "status": "completed",
        "conclusion": "failure",
        "run_number": 10,
    }
    monkeypatch.setattr(mutation_checkpoint, "_api_json", lambda *_args: {"workflow_runs": [run]})

    assert (
        mutation_checkpoint._find_resume_run(
            api_url="https://api.github.test",
            repository="owner/repo",
            token="token",
            current_run_id=99,
            current_run_attempt=1,
            head_sha=_HEAD_SHA,
        )
        is None
    )


def test_plan_passes_current_actions_identity_to_api_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output_path = tmp_path / "output.txt"
    values = {
        "GITHUB_API_URL": "https://api.github.test",
        "GITHUB_REPOSITORY": "owner/repo",
        "GH_TOKEN": "token",
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": _HEAD_SHA,
        "MUTATION_SHARDS_JSON": json.dumps([{"id": _SHARD_ID}]),
        "GITHUB_OUTPUT": str(output_path),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    resume_calls: list[dict[str, Any]] = []
    artifact_calls: list[dict[str, Any]] = []

    def find_resume(**kwargs: Any) -> int:
        resume_calls.append(kwargs)
        return 12

    def list_artifacts(**kwargs: Any) -> list[dict[str, Any]]:
        artifact_calls.append(kwargs)
        return []

    monkeypatch.setattr(mutation_checkpoint, "_find_resume_run", find_resume)
    monkeypatch.setattr(mutation_checkpoint, "_list_run_artifacts", list_artifacts)

    mutation_checkpoint._plan()

    assert resume_calls == [
        {
            "api_url": values["GITHUB_API_URL"],
            "repository": values["GITHUB_REPOSITORY"],
            "token": values["GH_TOKEN"],
            "current_run_id": 99,
            "current_run_attempt": 1,
            "head_sha": _HEAD_SHA,
        }
    ]
    assert artifact_calls == [
        {
            "api_url": values["GITHUB_API_URL"],
            "repository": values["GITHUB_REPOSITORY"],
            "token": values["GH_TOKEN"],
            "run_id": 12,
        }
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["mutation_checkpoint.py"],
        ["mutation_checkpoint.py", "run"],
        [
            "mutation_checkpoint.py",
            "restore",
            "--runner-fingerprint",
            _FINGERPRINT,
            "--shard-id",
            _SHARD_ID,
            *_FILTERS,
        ],
        [
            "mutation_checkpoint.py",
            "restore",
            "--head-sha",
            _HEAD_SHA,
            "--shard-id",
            _SHARD_ID,
            *_FILTERS,
        ],
        [
            "mutation_checkpoint.py",
            "restore",
            "--head-sha",
            _HEAD_SHA,
            "--runner-fingerprint",
            _FINGERPRINT,
            *_FILTERS,
        ],
        [
            "mutation_checkpoint.py",
            "restore",
            "--head-sha",
            _HEAD_SHA,
            "--runner-fingerprint",
            _FINGERPRINT,
            "--shard-id",
            _SHARD_ID,
        ],
        [
            "mutation_checkpoint.py",
            "select-artifact",
            "--shard-id",
            _SHARD_ID,
            "--runner-fingerprint",
            _FINGERPRINT,
        ],
        [
            "mutation_checkpoint.py",
            "select-artifact",
            "--artifacts-json",
            "{}",
            "--runner-fingerprint",
            _FINGERPRINT,
        ],
        [
            "mutation_checkpoint.py",
            "select-artifact",
            "--artifacts-json",
            "{}",
            "--shard-id",
            _SHARD_ID,
        ],
        ["mutation_checkpoint.py", "select-artifact"],
    ],
)
def test_cli_rejects_missing_command_arguments(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as error:
        mutation_checkpoint.main()

    assert error.value.code == 2


def test_restore_cli_uses_default_checkpoint_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation_checkpoint.py",
            "restore",
            "--head-sha",
            _HEAD_SHA,
            "--runner-fingerprint",
            _FINGERPRINT,
            "--shard-id",
            _SHARD_ID,
            *_FILTERS,
        ],
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mutation_checkpoint,
        "restore_checkpoint",
        lambda **kwargs: captured.update(kwargs) or 1,
    )

    mutation_checkpoint.main()

    assert captured == {
        "root": tmp_path,
        "checkpoint_dir": Path(".mutation-checkpoint"),
        "head_sha": _HEAD_SHA,
        "runner_fingerprint": _FINGERPRINT,
        "shard_id": _SHARD_ID,
        "filters": list(_FILTERS),
    }


def test_restore_cli_parses_explicit_checkpoint_directory_as_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    checkpoint_dir = tmp_path / "saved-checkpoint"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mutation_checkpoint.py",
            "restore",
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--head-sha",
            _HEAD_SHA,
            "--runner-fingerprint",
            _FINGERPRINT,
            "--shard-id",
            _SHARD_ID,
            *_FILTERS,
        ],
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mutation_checkpoint,
        "restore_checkpoint",
        lambda **kwargs: captured.update(kwargs) or 0,
    )

    mutation_checkpoint.main()

    assert captured["checkpoint_dir"] == checkpoint_dir
    assert isinstance(captured["checkpoint_dir"], Path)


def test_cli_help_describes_checkpoint_behavior(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["mutation_checkpoint.py", "--help"])

    with pytest.raises(SystemExit) as error:
        mutation_checkpoint.main()

    assert error.value.code == 0
    assert "Persist and restore exact, resumable checkpoints" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("option", "value"),
    [("--max-children", "many"), ("--interval-minutes", "soon")],
)
def test_run_cli_rejects_non_numeric_values(
    option: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["mutation_checkpoint.py", "run", option, value, *_FILTERS],
    )
    monkeypatch.setattr(
        mutation_checkpoint, "_run", lambda *_args: pytest.fail("invalid values reached runner")
    )

    with pytest.raises(SystemExit) as error:
        mutation_checkpoint.main()

    assert error.value.code == 2


def test_run_cli_uses_default_worker_and_checkpoint_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["mutation_checkpoint.py", "run", *_FILTERS])
    captured: list[tuple[int, float, list[str]]] = []
    monkeypatch.setattr(
        mutation_checkpoint,
        "_run",
        lambda workers, interval, filters: captured.append((workers, interval, filters)) or 0,
    )

    with pytest.raises(SystemExit) as error:
        mutation_checkpoint.main()

    assert error.value.code == 0
    assert captured == [(4, 15, list(_FILTERS))]


def test_stop_process_does_not_escalate_when_child_exits_on_interrupt() -> None:
    child = _FakeProcess()

    mutation_checkpoint._stop_process(child)

    assert child.signals == [signal.SIGINT]
    assert not child.terminated
    assert not child.killed


def test_stop_process_uses_bounded_escalation_waits() -> None:
    class SlowProcess(_FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.waits: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("mutmut", timeout)
            return super().wait()

    child = SlowProcess()

    mutation_checkpoint._stop_process(child)

    assert child.waits == [2, 1, None]
    assert child.terminated
    assert child.killed


def test_stop_process_returns_after_a_successful_interrupt_wait() -> None:
    class ExitsWhenWaited(_FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.waits: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.waits.append(timeout)
            self.returncode = 0
            return 0

    child = ExitsWhenWaited()

    mutation_checkpoint._stop_process(child)

    assert child.waits == [2]
    assert not child.terminated
    assert not child.killed


def test_publish_next_checkpoint_publishes_and_increments_once() -> None:
    state = mutation_checkpoint._CheckpointState(process=_FakeProcess())
    state.sequence = 7
    published: list[int] = []

    mutation_checkpoint._publish_next_checkpoint(
        lambda sequence: published.append(sequence) or True, state
    )

    assert published == [7]
    assert state.sequence == 8


def test_run_with_periodic_checkpoints_passes_command_directory_and_environment() -> None:
    child = _FakeProcess()
    child.returncode = 7
    captured: dict[str, Any] = {}

    def process_factory(command: list[str], *, cwd: Path, env: dict[str, str]) -> _FakeProcess:
        captured.update(command=command, cwd=cwd, env=env)
        return child

    result = mutation_checkpoint.run_with_periodic_checkpoints(
        ["mutmut", "run", "module.*"],
        interval_seconds=1,
        publish=lambda _sequence: True,
        cwd=Path("/tmp/checkpoint-test"),
        execution=mutation_checkpoint._CheckpointExecution(
            environment={"GITHUB_SHA": _HEAD_SHA}, process_factory=process_factory
        ),
    )

    assert result == 7
    assert captured == {
        "command": ["mutmut", "run", "module.*"],
        "cwd": Path("/tmp/checkpoint-test"),
        "env": {"GITHUB_SHA": _HEAD_SHA},
    }


def test_run_with_periodic_checkpoints_rejects_zero_interval() -> None:
    with pytest.raises(ValueError, match=r"^checkpoint interval must be positive$"):
        mutation_checkpoint.run_with_periodic_checkpoints(
            ["mutmut"], interval_seconds=0, publish=lambda _sequence: True, cwd=Path.cwd()
        )


def test_run_with_periodic_checkpoints_uses_defaults_and_clears_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def capture_signal_handlers(
        *_args: Any,
        execution: mutation_checkpoint._CheckpointExecution,
        state: mutation_checkpoint._CheckpointState,
        **_kwargs: Any,
    ) -> int:
        captured["execution"] = execution
        captured["state"] = state
        state.process = object()
        return 0

    monkeypatch.setattr(mutation_checkpoint, "_run_with_signal_capture", capture_signal_handlers)

    assert (
        mutation_checkpoint.run_with_periodic_checkpoints(
            ["mutmut"],
            interval_seconds=1,
            publish=lambda _sequence: True,
            cwd=Path.cwd(),
        )
        == 0
    )
    assert isinstance(captured["execution"], mutation_checkpoint._CheckpointExecution)
    assert captured["state"].process is None

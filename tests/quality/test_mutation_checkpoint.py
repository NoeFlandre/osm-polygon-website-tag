from __future__ import annotations

import json
import math
import signal
import subprocess
import sys
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

    response = mutation_checkpoint._api_json("https://api.github.test/repos?a=1", "token")

    assert response == {"artifacts": []}
    assert created == [("api.github.test", None, 30)]
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


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (403, b"{}", "HTTP 403"),
        (200, b"[]", "unexpected response"),
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
    requests: list[str] = []

    def api_json(url: str, _token: str) -> dict[str, Any]:
        requests.append(url)
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
        "https://api.github.test/repos/owner/repo/actions/runs/12/artifacts?per_page=100&page=1",
        "https://api.github.test/repos/owner/repo/actions/runs/12/artifacts?per_page=100&page=2",
    ]


def test_list_run_artifacts_uses_default_count_and_rejects_bad_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mutation_checkpoint, "_api_json", lambda *_args: {"artifacts": []})
    assert (
        mutation_checkpoint._list_run_artifacts(
            api_url="https://api.github.test",
            repository="owner/repo",
            token="token",
            run_id=12,
        )
        == []
    )
    monkeypatch.setattr(mutation_checkpoint, "_api_json", lambda *_args: {"artifacts": "bad"})
    with pytest.raises(RuntimeError, match="unexpected shape"):
        mutation_checkpoint._list_run_artifacts(
            api_url="https://api.github.test",
            repository="owner/repo",
            token="token",
            run_id=12,
        )


def test_resume_run_rejects_unexpected_workflow_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation_checkpoint, "_api_json", lambda *_args: {"workflow_runs": None})
    with pytest.raises(RuntimeError, match="unexpected shape"):
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
    with pytest.raises(RuntimeError, match="GITHUB_OUTPUT is not set"):
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


def test_publish_uploads_checkpoint_with_attempt_scoped_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_publish_environment(monkeypatch)
    node = tmp_path / "node"
    node.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(mutation_checkpoint.shutil, "which", lambda _name: str(node))
    monkeypatch.setattr(
        mutation_checkpoint,
        "snapshot_checkpoint",
        lambda **_kwargs: (Path("manifest.json"), Path("mutants/source.meta")),
    )
    captured: list[tuple[list[str], Path]] = []

    class Uploaded:
        def wait(self) -> int:
            return 0

    def start(command: list[str], root: Path) -> Uploaded:
        captured.append((command, root))
        return Uploaded()

    monkeypatch.setattr(mutation_checkpoint, "_start_artifact_uploader", start)

    assert mutation_checkpoint._publish(tmp_path, _FILTERS, 3) is True
    command, root = captured[0]
    assert root == tmp_path
    assert command[0] == str(node.resolve())
    assert (
        command[2]
        == f"mutation-checkpoint-{_HEAD_SHA}-{_FINGERPRINT}-{_SHARD_ID}-run12-attempt2-seq3"
    )


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
    with pytest.raises(RuntimeError, match=r"Node\.js is required"):
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
    with pytest.raises(RuntimeError, match="artifact upload failed"):
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

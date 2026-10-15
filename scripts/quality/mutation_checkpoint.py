#!/usr/bin/env python3
"""Persist and restore exact, resumable checkpoints for mutation sweeps."""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.quality import mutation_runner

_SCHEMA_VERSION = 1
_CHECKPOINT_PREFIX = "mutation-checkpoint-"
_REQUIRED_META_FIELDS = frozenset(
    {"exit_code_by_key", "durations_by_key", "estimated_durations_by_key"}
)
_OPTIONAL_META_FIELDS = frozenset({"hash_by_function_name", "type_check_error_by_key"})
_META_FIELDS = _REQUIRED_META_FIELDS | _OPTIONAL_META_FIELDS
_CHECKPOINT_NAME = re.compile(
    r"^mutation-checkpoint-(?P<sha>[0-9a-f]{40})-(?P<fingerprint>[0-9a-f]{16})-"
    r"(?P<shard>.+)-run(?P<run_id>\d+)-attempt(?P<attempt>\d+)-seq(?P<sequence>\d+)$"
)


def _meta_paths(root: Path, filters: Iterable[str]) -> tuple[tuple[Path, Path], ...]:
    result = []
    for source in mutation_runner._source_paths_for_mutant_names(filters):
        relative = Path("mutants") / f"{source}.meta"
        result.append((root / relative, relative))
    return tuple(result)


def _finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_duration_entry(key: object, value: object) -> bool:
    return isinstance(key, str) and _finite_number(value)


def _finite_duration_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        _valid_duration_entry(key, duration) for key, duration in value.items()
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _valid_exit_code_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and (code is None or type(code) is int) for key, code in value.items()
    )


def _valid_string_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _valid_error_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and (error is None or isinstance(error, str))
        for key, error in value.items()
    )


def _valid_metadata_keys(value: Mapping[str, Any]) -> bool:
    keys = value.keys()
    return keys >= _REQUIRED_META_FIELDS and not keys - _META_FIELDS


def _valid_metadata_mappings(value: Mapping[str, Any]) -> bool:
    return (
        _valid_exit_code_mapping(value["exit_code_by_key"])
        and _valid_string_mapping(value.get("hash_by_function_name", {}))
        and _valid_error_mapping(value.get("type_check_error_by_key", {}))
    )


def _valid_metadata_durations(value: Mapping[str, Any]) -> bool:
    return _finite_duration_mapping(value["durations_by_key"]) and _finite_duration_mapping(
        value["estimated_durations_by_key"]
    )


def _valid_metadata(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        _valid_metadata_keys(value)
        and _valid_metadata_mappings(value)
        and _valid_metadata_durations(value)
    )


def _read_valid_metadata(path: Path) -> tuple[bytes, dict[str, Any]] | None:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not _valid_metadata(value):
        return None
    return payload, value


def _has_selected_result(metadata: Mapping[str, Any], filters: Sequence[str]) -> bool:
    return any(
        code is not None and any(fnmatch.fnmatchcase(name, pattern) for pattern in filters)
        for name, code in metadata["exit_code_by_key"].items()
    )


def snapshot_checkpoint(
    *,
    root: Path,
    destination: Path,
    head_sha: str,
    runner_fingerprint: str,
    shard_id: str,
    filters: Sequence[str],
) -> tuple[Path, ...]:
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "head_sha": head_sha,
        "runner_fingerprint": runner_fingerprint,
        "shard_id": shard_id,
        "filters": list(filters),
    }
    manifest_path = destination / "manifest.json"
    _atomic_write_bytes(
        manifest_path,
        (json.dumps(manifest, sort_keys=True) + "\n").encode(),
    )
    included: list[Path] = [manifest_path]
    for source, relative in _meta_paths(root, filters):
        loaded = _read_valid_metadata(source)
        if loaded is None:
            continue
        payload, metadata = loaded
        if not _has_selected_result(metadata, filters):
            continue
        target = destination / relative
        _atomic_write_bytes(target, payload)
        included.append(target)
    return tuple(included)


def _checkpoint_manifest_matches(
    *,
    checkpoint_dir: Path,
    head_sha: str,
    runner_fingerprint: str,
    shard_id: str,
    filters: Sequence[str],
) -> bool:
    manifest_path = checkpoint_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_bytes().decode())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    expected = {
        "schema_version": _SCHEMA_VERSION,
        "head_sha": head_sha,
        "runner_fingerprint": runner_fingerprint,
        "shard_id": shard_id,
        "filters": list(filters),
    }
    return manifest == expected


def _checkpoint_metadata_candidates(
    *, root: Path, checkpoint_dir: Path, filters: Sequence[str]
) -> list[tuple[Path, bytes]]:
    candidates: list[tuple[Path, bytes]] = []
    for _, relative in _meta_paths(root, filters):
        loaded = _read_valid_metadata(checkpoint_dir / relative)
        if loaded is None:
            continue
        payload, metadata = loaded
        if _has_selected_result(metadata, filters):
            candidates.append((root / relative, payload))
    return candidates


def restore_checkpoint(
    *,
    root: Path,
    checkpoint_dir: Path,
    head_sha: str,
    runner_fingerprint: str,
    shard_id: str,
    filters: Sequence[str],
) -> int:
    if not _checkpoint_manifest_matches(
        checkpoint_dir=checkpoint_dir,
        head_sha=head_sha,
        runner_fingerprint=runner_fingerprint,
        shard_id=shard_id,
        filters=filters,
    ):
        return 0
    candidates = _checkpoint_metadata_candidates(
        root=root, checkpoint_dir=checkpoint_dir, filters=filters
    )
    for target, payload in candidates:
        _atomic_write_bytes(target, payload)
    return len(candidates)


def select_checkpoint_artifact(
    checkpoint_map: Mapping[str, Sequence[Mapping[str, str]]],
    *,
    shard_id: str,
    runner_fingerprint: str,
) -> str:
    for candidate in checkpoint_map.get(shard_id, ()):
        if candidate.get("fingerprint") == runner_fingerprint:
            return candidate["name"]
    return ""


def _checkpoint_name_match(artifact: Mapping[str, Any]) -> re.Match[str] | None:
    if artifact.get("expired") is True:
        return None
    name = artifact.get("name")
    if not isinstance(name, str):
        return None
    return _CHECKPOINT_NAME.fullmatch(name)


def _checkpoint_run_matches(match: re.Match[str], *, head_sha: str, run_id: int) -> bool:
    return match["sha"] == head_sha and int(match["run_id"]) == run_id


def _checkpoint_created_at(artifact: Mapping[str, Any]) -> str | None:
    created_at = artifact.get("created_at")
    return created_at if isinstance(created_at, str) and created_at else None


def _matching_checkpoint_artifact(
    artifact: Mapping[str, Any], *, head_sha: str, shard_ids: set[str], run_id: int
) -> tuple[str, str, str, str, int] | None:
    match = _checkpoint_name_match(artifact)
    if match is None:
        return None
    if not _checkpoint_run_matches(match, head_sha=head_sha, run_id=run_id):
        return None
    shard_id = match["shard"]
    if shard_id not in shard_ids:
        return None
    created_at = _checkpoint_created_at(artifact)
    if created_at is None:
        return None
    return (
        shard_id,
        match["fingerprint"],
        created_at,
        str(artifact["name"]),
        int(match["sequence"]),
    )


def _checkpoint_artifact_is_newer(
    created_at: str, sequence: int, previous: tuple[str, str, int] | None
) -> bool:
    return previous is None or (created_at, sequence) > (previous[0], previous[2])


def checkpoint_artifact_map(
    artifacts: Iterable[Mapping[str, Any]], *, head_sha: str, shard_ids: set[str], run_id: int
) -> dict[str, list[dict[str, str]]]:
    selected: dict[tuple[str, str], tuple[str, str, int]] = {}
    for artifact in artifacts:
        match = _matching_checkpoint_artifact(
            artifact, head_sha=head_sha, shard_ids=shard_ids, run_id=run_id
        )
        if match is None:
            continue
        shard_id, fingerprint, created_at, name, sequence = match
        key = (shard_id, fingerprint)
        previous = selected.get(key)
        if _checkpoint_artifact_is_newer(created_at, sequence, previous):
            selected[key] = (created_at, name, sequence)

    result: dict[str, list[dict[str, str]]] = {}
    for (shard_id, fingerprint), (_, name, sequence) in sorted(selected.items()):
        result.setdefault(shard_id, []).append(
            {"fingerprint": fingerprint, "name": name, "sequence": str(sequence)}
        )
    return result


def _api_request_target(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise ValueError("GitHub Actions API URL must use HTTPS")
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return parsed.hostname, target, parsed.port


def _read_api_response(connection: Any, target: str, token: str) -> bytes:
    connection.request(
        "GET",
        target,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    response = connection.getresponse()
    if response.status >= 400:
        raise RuntimeError(f"GitHub Actions API returned HTTP {response.status}")
    return response.read()


def _decode_api_object(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub Actions API returned an unexpected response")
    return value


def _api_json(url: str, token: str) -> dict[str, Any]:
    hostname, target, port = _api_request_target(url)
    connection = HTTPSConnection(hostname, port, timeout=30)
    try:
        payload = _read_api_response(connection, target, token)
    finally:
        connection.close()
    return _decode_api_object(payload)


def _artifact_page_items(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = response.get("artifacts", [])
    if not isinstance(items, list):
        raise RuntimeError("GitHub Actions artifact list had an unexpected shape")
    return [item for item in items if isinstance(item, dict)]


def _list_run_artifacts(
    *, api_url: str, repository: str, token: str, run_id: int
) -> list[dict[str, Any]]:
    base = f"{api_url.rstrip('/')}/repos/{repository}/actions/runs/{run_id}/artifacts"
    artifacts: list[dict[str, Any]] = []
    page = 1
    while True:
        response = _api_json(f"{base}?per_page=100&page={page}", token)
        artifacts.extend(_artifact_page_items(response))
        total_count = int(response.get("total_count", len(artifacts)))
        if len(artifacts) >= total_count:
            return artifacts
        page += 1


def _resume_run_id(run: Mapping[str, Any]) -> int | None:
    run_id = run.get("id")
    return run_id if type(run_id) is int else None


def _is_resume_run(run: Mapping[str, Any], *, current_run_id: int, head_sha: str) -> bool:
    run_id = _resume_run_id(run)
    return (
        run_id is not None
        and run_id != current_run_id
        and run.get("head_sha") == head_sha
        and run.get("status") == "completed"
    )


def _resume_run_number(run: Mapping[str, Any]) -> int | None:
    run_number = run.get("run_number")
    return run_number if type(run_number) is int else None


def _resume_candidate(
    run: object, *, current_run_id: int, head_sha: str
) -> tuple[int, dict[str, Any]] | None:
    if not isinstance(run, dict) or not _is_resume_run(
        run, current_run_id=current_run_id, head_sha=head_sha
    ):
        return None
    run_number = _resume_run_number(run)
    if run_number is None:
        return None
    return run_number, run


def _latest_resume_candidate(
    runs: Sequence[Any], *, current_run_id: int, head_sha: str
) -> dict[str, Any] | None:
    candidates = [
        candidate
        for run in runs
        if (candidate := _resume_candidate(run, current_run_id=current_run_id, head_sha=head_sha))
        is not None
    ]
    latest = max(candidates, key=lambda candidate: candidate[0], default=None)
    return latest[1] if latest is not None else None


def _find_resume_run(
    *,
    api_url: str,
    repository: str,
    token: str,
    current_run_id: int,
    current_run_attempt: int,
    head_sha: str,
) -> int | None:
    if current_run_attempt > 1:
        return current_run_id

    url = (
        f"{api_url.rstrip('/')}/repos/{repository}/actions/workflows/mutation-sweep.yml/runs"
        f"?branch=main&head_sha={head_sha}&per_page=100"
    )
    response = _api_json(url, token)
    runs = response.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise RuntimeError("GitHub Actions run list had an unexpected shape")
    latest = _latest_resume_candidate(runs, current_run_id=current_run_id, head_sha=head_sha)
    if latest is None or latest.get("conclusion") not in {"failure", "cancelled", "timed_out"}:
        return None
    return int(latest["id"])


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT is not set")
    with Path(output_path).open("ab") as output:
        output.write(f"{name}={value}\n".encode())


def _plan() -> None:
    api_url = os.environ["GITHUB_API_URL"]
    repository = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GH_TOKEN"]
    current_run_id = int(os.environ["GITHUB_RUN_ID"])
    current_run_attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    head_sha = os.environ["GITHUB_SHA"]
    shards = json.loads(os.environ["MUTATION_SHARDS_JSON"])
    shard_ids = {shard["id"] for shard in shards}
    resume_run = _find_resume_run(
        api_url=api_url,
        repository=repository,
        token=token,
        current_run_id=current_run_id,
        current_run_attempt=current_run_attempt,
        head_sha=head_sha,
    )
    checkpoint_map: dict[str, list[dict[str, str]]] = {}
    if resume_run is not None:
        artifacts = _list_run_artifacts(
            api_url=api_url, repository=repository, token=token, run_id=resume_run
        )
        checkpoint_map = checkpoint_artifact_map(
            artifacts, head_sha=head_sha, shard_ids=shard_ids, run_id=resume_run
        )
    _write_output("previous_run_id", str(resume_run or ""))
    _write_output("checkpoint_artifacts", json.dumps(checkpoint_map, separators=(",", ":")))


class _CheckpointSignal(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


@dataclass(frozen=True)
class _CheckpointExecution:
    environment: Mapping[str, str] | None = None
    process_factory: Callable[..., Any] = subprocess.Popen
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass
class _CheckpointState:
    process: Any | None = None
    pending_signal: int | None = None
    sequence: int = 1


@contextmanager
def _capture_signals(state: _CheckpointState) -> Any:
    previous_handlers: dict[int, Any] = {}

    def handle_signal(signum: int, _frame: Any) -> None:
        if state.pending_signal is None:
            state.pending_signal = signum

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, handle_signal)
        yield
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def _wait_or_escalate(child: Any, timeout: float, stop: Callable[[], None]) -> bool:
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop()
        return False
    return True


def _stop_process(child: Any) -> None:
    if child.poll() is not None:
        return
    try:
        child.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    if _wait_or_escalate(child, 2, child.terminate):
        return
    if _wait_or_escalate(child, 1, child.kill):
        return
    child.wait()


def _publish_next_checkpoint(publish: Callable[[int], bool], state: _CheckpointState) -> None:
    publish(state.sequence)
    state.sequence += 1


def _execution_environment(execution: _CheckpointExecution) -> dict[str, str]:
    if execution.environment is not None:
        return dict(execution.environment)
    return os.environ.copy()


def _run_periodic_checkpoint_loop(
    state: _CheckpointState,
    *,
    interval_seconds: float,
    publish: Callable[[int], bool],
    execution: _CheckpointExecution,
) -> None:
    process = state.process
    if process is None:
        raise RuntimeError("cannot checkpoint before the mutmut process starts")
    next_checkpoint = execution.monotonic() + interval_seconds
    while state.pending_signal is None:
        if process.poll() is not None:
            return
        now = execution.monotonic()
        if now >= next_checkpoint:
            _publish_next_checkpoint(publish, state)
            next_checkpoint = execution.monotonic() + interval_seconds
        else:
            execution.sleep(min(1.0, next_checkpoint - now))


def _run_until_complete(
    command: Sequence[str],
    interval_seconds: float,
    publish: Callable[[int], bool],
    cwd: Path,
    execution: _CheckpointExecution,
    state: _CheckpointState,
) -> int:
    state.process = execution.process_factory(
        command, cwd=cwd, env=_execution_environment(execution)
    )
    _run_periodic_checkpoint_loop(
        state,
        interval_seconds=interval_seconds,
        publish=publish,
        execution=execution,
    )
    if state.pending_signal is not None:
        raise _CheckpointSignal(state.pending_signal)
    return_code = int(state.process.wait())
    if state.pending_signal is not None:
        raise _CheckpointSignal(state.pending_signal)
    _publish_next_checkpoint(publish, state)
    if state.pending_signal is not None:
        return 128 + state.pending_signal
    return return_code


def _finish_interrupted_run(publish: Callable[[int], bool], state: _CheckpointState) -> None:
    try:
        # Actions gives cancelled processes a short signal grace period, so publish the
        # last-known-good snapshot before child shutdown begins.
        _publish_next_checkpoint(publish, state)
    finally:
        if state.process is not None:
            _stop_process(state.process)
    if state.process is not None:
        _publish_next_checkpoint(publish, state)


def _run_with_signal_capture(
    command: Sequence[str],
    *,
    interval_seconds: float,
    publish: Callable[[int], bool],
    cwd: Path,
    execution: _CheckpointExecution,
    state: _CheckpointState,
) -> int:
    with _capture_signals(state):
        try:
            return _run_until_complete(command, interval_seconds, publish, cwd, execution, state)
        except _CheckpointSignal as interrupted:
            _finish_interrupted_run(publish, state)
            return 128 + interrupted.signum
        except BaseException:
            if state.process is not None:
                _stop_process(state.process)
            raise


def run_with_periodic_checkpoints(
    command: Sequence[str],
    *,
    interval_seconds: float,
    publish: Callable[[int], bool],
    cwd: Path,
    execution: _CheckpointExecution | None = None,
) -> int:
    if interval_seconds <= 0:
        raise ValueError("checkpoint interval must be positive")
    dependencies = execution if execution is not None else _CheckpointExecution()
    state = _CheckpointState()
    try:
        return _run_with_signal_capture(
            command,
            interval_seconds=interval_seconds,
            publish=publish,
            cwd=cwd,
            execution=dependencies,
            state=state,
        )
    finally:
        state.process = None


def _start_artifact_uploader(
    command: Sequence[str],
    root: Path,
    process_factory: Callable[..., Any] = subprocess.Popen,
) -> Any:
    return process_factory(command, cwd=root, start_new_session=True)


def _publish(root: Path, filters: Sequence[str], sequence: int) -> bool:
    head_sha = os.environ["GITHUB_SHA"]
    fingerprint = os.environ["MUTATION_RUNNER_FINGERPRINT"]
    shard_id = os.environ["MUTATION_SHARD_ID"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    with tempfile.TemporaryDirectory(prefix="mutation-checkpoint-") as temporary:
        snapshot = Path(temporary)
        included = snapshot_checkpoint(
            root=root,
            destination=snapshot,
            head_sha=head_sha,
            runner_fingerprint=fingerprint,
            shard_id=shard_id,
            filters=filters,
        )
        if len(included) == 1:
            return False
        name = (
            f"{_CHECKPOINT_PREFIX}{head_sha}-{fingerprint}-{shard_id}-"
            f"run{run_id}-attempt{attempt}-seq{sequence}"
        )
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required to upload mutation checkpoints")
        uploader = root / "scripts/quality/mutation-artifact-uploader/bundle/index.mjs"
        command = [str(Path(node).resolve(strict=True)), str(uploader), name, str(snapshot)]
        process = _start_artifact_uploader(command, root)
        if process.wait() != 0:
            raise RuntimeError("checkpoint artifact upload failed")
        return True


def _run(max_children: int, interval_minutes: float, filters: Sequence[str]) -> int:
    root = Path.cwd().resolve()
    runner = root / "scripts/quality/mutation_runner.py"
    command = [
        sys.executable,
        str(runner),
        "run",
        "--max-children",
        str(max_children),
        *filters,
    ]
    environment = os.environ.copy()
    environment["MUTATION_RESUME_COMPLETED"] = "1"
    return run_with_periodic_checkpoints(
        command,
        interval_seconds=interval_minutes * 60,
        publish=lambda sequence: _publish(root, filters, sequence),
        cwd=root,
        execution=_CheckpointExecution(environment=environment),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")

    restore = commands.add_parser("restore")
    restore.add_argument("--checkpoint-dir", type=Path, default=Path(".mutation-checkpoint"))
    restore.add_argument("--head-sha", required=True)
    restore.add_argument("--runner-fingerprint", required=True)
    restore.add_argument("--shard-id", required=True)
    restore.add_argument("filters", nargs="+")

    choose = commands.add_parser("select-artifact")
    choose.add_argument("--artifacts-json", required=True)
    choose.add_argument("--shard-id", required=True)
    choose.add_argument("--runner-fingerprint", required=True)

    run = commands.add_parser("run")
    run.add_argument("--max-children", type=int, default=4)
    run.add_argument("--interval-minutes", type=float, default=15)
    run.add_argument("filters", nargs="+")

    args = parser.parse_args()
    if args.command == "plan":
        _plan()
    elif args.command == "restore":
        restored = restore_checkpoint(
            root=Path.cwd(),
            checkpoint_dir=args.checkpoint_dir,
            head_sha=args.head_sha,
            runner_fingerprint=args.runner_fingerprint,
            shard_id=args.shard_id,
            filters=args.filters,
        )
        print(f"Restored {restored} mutation metadata files")
    elif args.command == "select-artifact":
        artifacts = json.loads(args.artifacts_json)
        print(
            select_checkpoint_artifact(
                artifacts,
                shard_id=args.shard_id,
                runner_fingerprint=args.runner_fingerprint,
            )
        )
    else:
        raise SystemExit(_run(args.max_children, args.interval_minutes, args.filters))


if __name__ == "__main__":
    main()

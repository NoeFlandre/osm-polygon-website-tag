#!/usr/bin/env python3
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


def _finite_duration_mapping(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key, duration in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
        ):
            return False
        try:
            if not math.isfinite(duration):
                return False
        except OverflowError:
            return False
    return True


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


def _valid_metadata(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    keys = value.keys()
    if not keys >= _REQUIRED_META_FIELDS or keys - _META_FIELDS:
        return False
    exit_codes = value["exit_code_by_key"]
    hashes = value.get("hash_by_function_name", {})
    type_errors = value.get("type_check_error_by_key", {})
    return (
        isinstance(exit_codes, dict)
        and all(
            isinstance(key, str) and (code is None or type(code) is int)
            for key, code in exit_codes.items()
        )
        and isinstance(hashes, dict)
        and all(isinstance(key, str) and isinstance(digest, str) for key, digest in hashes.items())
        and isinstance(type_errors, dict)
        and all(
            isinstance(key, str) and (error is None or isinstance(error, str))
            for key, error in type_errors.items()
        )
        and _finite_duration_mapping(value["durations_by_key"])
        and _finite_duration_mapping(value["estimated_durations_by_key"])
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
        (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
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


def restore_checkpoint(
    *,
    root: Path,
    checkpoint_dir: Path,
    head_sha: str,
    runner_fingerprint: str,
    shard_id: str,
    filters: Sequence[str],
) -> int:
    manifest_path = checkpoint_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    expected = {
        "schema_version": _SCHEMA_VERSION,
        "head_sha": head_sha,
        "runner_fingerprint": runner_fingerprint,
        "shard_id": shard_id,
        "filters": list(filters),
    }
    if manifest != expected:
        return 0

    candidates: list[tuple[Path, Path, bytes]] = []
    for _, relative in _meta_paths(root, filters):
        loaded = _read_valid_metadata(checkpoint_dir / relative)
        if loaded is None:
            continue
        payload, metadata = loaded
        if _has_selected_result(metadata, filters):
            candidates.append((root / relative, relative, payload))

    for target, _, payload in candidates:
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


def checkpoint_artifact_map(
    artifacts: Iterable[Mapping[str, Any]], *, head_sha: str, shard_ids: set[str], run_id: int
) -> dict[str, list[dict[str, str]]]:
    selected: dict[tuple[str, str], tuple[str, str, int]] = {}
    for artifact in artifacts:
        if artifact.get("expired") is True:
            continue
        name = artifact.get("name")
        if not isinstance(name, str):
            continue
        match = _CHECKPOINT_NAME.fullmatch(name)
        if match is None:
            continue
        if match["sha"] != head_sha or int(match["run_id"]) != run_id:
            continue
        shard_id = match["shard"]
        if shard_id not in shard_ids:
            continue
        fingerprint = match["fingerprint"]
        sequence = int(match["sequence"])
        created_at = str(artifact.get("created_at", ""))
        key = (shard_id, fingerprint)
        previous = selected.get(key)
        if previous is None or (created_at, sequence) > (previous[0], previous[2]):
            selected[key] = (created_at, name, sequence)

    result: dict[str, list[dict[str, str]]] = {}
    for (shard_id, fingerprint), (_, name, sequence) in sorted(selected.items()):
        result.setdefault(shard_id, []).append(
            {"fingerprint": fingerprint, "name": name, "sequence": str(sequence)}
        )
    return result


def _api_json(url: str, token: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise ValueError("GitHub Actions API URL must use HTTPS")
    request_target = parsed.path or "/"
    if parsed.query:
        request_target = f"{request_target}?{parsed.query}"
    connection = HTTPSConnection(parsed.hostname, parsed.port, timeout=30)
    try:
        connection.request(
            "GET",
            request_target,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response = connection.getresponse()
        if response.status >= 400:
            raise RuntimeError(f"GitHub Actions API returned HTTP {response.status}")
        payload = json.loads(response.read())
    finally:
        connection.close()
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub Actions API returned an unexpected response")
    return payload


def _list_run_artifacts(
    *, api_url: str, repository: str, token: str, run_id: int
) -> list[dict[str, Any]]:
    base = f"{api_url.rstrip('/')}/repos/{repository}/actions/runs/{run_id}/artifacts"
    artifacts: list[dict[str, Any]] = []
    page = 1
    while True:
        response = _api_json(f"{base}?per_page=100&page={page}", token)
        page_artifacts = response.get("artifacts", [])
        if not isinstance(page_artifacts, list):
            raise RuntimeError("GitHub Actions artifact list had an unexpected shape")
        artifacts.extend(item for item in page_artifacts if isinstance(item, dict))
        if len(artifacts) >= int(response.get("total_count", len(artifacts))):
            return artifacts
        page += 1


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
    candidates = [
        run
        for run in runs
        if isinstance(run, dict)
        and int(run.get("id", 0)) != current_run_id
        and run.get("head_sha") == head_sha
        and run.get("status") == "completed"
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda run: int(run.get("run_number", 0)))
    if latest.get("conclusion") not in {"failure", "cancelled", "timed_out"}:
        return None
    return int(latest["id"])


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT is not set")
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


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


def _stop_process(child: Any) -> None:
    if child.poll() is not None:
        return
    try:
        child.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    for timeout, stop in ((2, child.terminate), (1, child.kill)):
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop()
        else:
            return
    child.wait()


def _publish_next_checkpoint(publish: Callable[[int], bool], state: _CheckpointState) -> None:
    publish(state.sequence)
    state.sequence += 1


def _run_until_complete(
    command: Sequence[str],
    interval_seconds: float,
    publish: Callable[[int], bool],
    cwd: Path,
    execution: _CheckpointExecution,
    state: _CheckpointState,
) -> int:
    environment = (
        dict(execution.environment) if execution.environment is not None else os.environ.copy()
    )
    state.process = execution.process_factory(command, cwd=cwd, env=environment)
    next_checkpoint = execution.monotonic() + interval_seconds
    while state.pending_signal is None:
        return_code = state.process.poll()
        if return_code is not None:
            break
        now = execution.monotonic()
        if now >= next_checkpoint:
            _publish_next_checkpoint(publish, state)
            next_checkpoint = execution.monotonic() + interval_seconds
        else:
            execution.sleep(min(1.0, next_checkpoint - now))
    if state.pending_signal is not None:
        raise _CheckpointSignal(state.pending_signal)
    return_code = int(state.process.wait())
    if state.pending_signal is not None:
        raise _CheckpointSignal(state.pending_signal)
    _publish_next_checkpoint(publish, state)
    return 128 + state.pending_signal if state.pending_signal is not None else return_code


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
    dependencies = execution or _CheckpointExecution()
    state = _CheckpointState()
    try:
        with _capture_signals(state):
            try:
                return _run_until_complete(
                    command, interval_seconds, publish, cwd, dependencies, state
                )
            except _CheckpointSignal as interrupted:
                _finish_interrupted_run(publish, state)
                return 128 + interrupted.signum
            except BaseException:
                if state.process is not None:
                    _stop_process(state.process)
                raise
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

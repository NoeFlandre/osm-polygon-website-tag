r"""Run state: run-owned directory, source manifests, and metadata.

A *run* is the unit of reproducibility. Each run owns a directory
inside the configured output root; the run directory layout is:

    <run_id>/
        polygons/
            <source-stem>.parquet         (POLYGON_PUBLIC_SCHEMA)
        analysis_observations/
            <source-stem>.parquet         (COMPARISON_OBSERVATION_SCHEMA)
        rejections/
            <source-stem>.parquet         (REJECTION_SCHEMA)
        analysis/
            *.parquet                     (analysis tables, see analyze.py)
        manifests/
            run.json
            sources.json
            expected_sources.json
            analysis_index.json
        failures.jsonl
        completion_receipt.json           (written only by finalize-run)

State machine
-------------

    INITIALIZED -> EXTRACTING -> EXTRACTED -> ENRICHING -> ENRICHED
        -> ANALYZED -> CARD_BUILT -> VERIFIED -> COMPLETE

    COMPLETE -> ENRICHING is the sole migration path for an older public
    schema or retryable website-text failures.

The run state is read from ``run.json`` via :func:`load_run`. Only
explicit reviewed mutations advance the state. The read-only
verifier never mutates ``run.json``.

Source identity contract
------------------------

* ``expected_sources.json`` records filename, size_bytes, mtime_ns only
  -- NO source SHA-256.
* Per-source mutation check: exact pre/post equality on size_bytes and
  mtime_ns.
* No SHA-256 of the PBF is computed during extraction; the card and
  documentation state this explicitly.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Run state names. Transitions are documented in the module docstring.
STATUS_INITIALIZED = "initialized"
STATUS_EXTRACTING = "extracting"
STATUS_EXTRACTED = "extracted"
STATUS_ENRICHING = "enriching"
STATUS_ENRICHED = "enriched"
STATUS_ANALYZED = "analyzed"
STATUS_CARD_BUILT = "card_built"
STATUS_VERIFIED = "verified"
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"

OPERATIONAL_MANIFEST_NAMES = frozenset({"uploaded_polygons.json", "completion_receipt.json"})


STATUS_VALUES: tuple[str, ...] = (
    STATUS_INITIALIZED,
    STATUS_EXTRACTING,
    STATUS_EXTRACTED,
    STATUS_ENRICHING,
    STATUS_ENRICHED,
    STATUS_ANALYZED,
    STATUS_CARD_BUILT,
    STATUS_VERIFIED,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
)


# Allowed forward transitions; verified -> incomplete is also allowed
# (a verified run can be marked incomplete without removing it).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_INITIALIZED: frozenset({STATUS_EXTRACTING, STATUS_INCOMPLETE}),
    STATUS_EXTRACTING: frozenset({STATUS_EXTRACTED, STATUS_INCOMPLETE}),
    STATUS_EXTRACTED: frozenset({STATUS_ENRICHING, STATUS_INCOMPLETE}),
    STATUS_ENRICHING: frozenset({STATUS_ENRICHED, STATUS_INCOMPLETE}),
    STATUS_ENRICHED: frozenset({STATUS_ANALYZED, STATUS_INCOMPLETE}),
    STATUS_ANALYZED: frozenset({STATUS_CARD_BUILT, STATUS_ENRICHING, STATUS_INCOMPLETE}),
    STATUS_CARD_BUILT: frozenset({STATUS_VERIFIED, STATUS_ENRICHING, STATUS_INCOMPLETE}),
    STATUS_VERIFIED: frozenset({STATUS_COMPLETE, STATUS_ENRICHING, STATUS_INCOMPLETE}),
    STATUS_COMPLETE: frozenset({STATUS_ENRICHING}),
    STATUS_INCOMPLETE: frozenset(),
}


def default_run_id() -> str:
    """Return a deterministic run ID derived from UTC timestamp."""
    return dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class SourceFingerprint:
    """A lightweight snapshot of one source PBF file's identity.

    Only filename, size_bytes, and mtime_ns are recorded. No SHA-256
    is computed at the run level; see the module docstring.
    """

    filename: str
    size_bytes: int
    mtime_ns: int

    def short_id(self) -> str:
        """Stable short identifier used as ``polygon_id`` prefix."""
        return self.filename.removesuffix(".osm.pbf")


@dataclass
class RunState:
    """Mutable handle to a run directory on disk."""

    run_dir: Path
    run_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON through a same-directory temporary file and replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(tmp).replace(path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Internal compatibility alias for run-state writers."""
    atomic_write_json(path, payload)


def _source_fingerprint_payload(fp: SourceFingerprint) -> dict[str, int | str]:
    return {
        "filename": fp.filename,
        "size_bytes": fp.size_bytes,
        "mtime_ns": fp.mtime_ns,
    }


def _write_sources_manifest(state: RunState) -> None:
    entries = sorted(
        state.sources.values(),
        key=lambda entry: str(entry["filename"]),
    )
    _atomic_write_json(state.run_dir / "manifests" / "sources.json", entries)


def snapshot_source_fingerprint(pbf_path: Path) -> SourceFingerprint:
    """Capture filename, size, and mtime of ``pbf_path``.

    No SHA-256 is computed.
    """
    st = pbf_path.stat()
    return SourceFingerprint(
        filename=pbf_path.name,
        size_bytes=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )


def initialise_run(
    output_root: Path,
    run_id: str | None = None,
    *,
    expected_sources: list[SourceFingerprint] | None = None,
) -> tuple[Path, RunState]:
    """Create a fresh run directory under ``output_root``.

    Returns ``(run_dir, run_state)``. The run directory layout is
    created on disk; ``run.json`` and ``sources.json`` are written.

    If ``expected_sources`` is provided, ``manifests/expected_sources.json``
    is written with one entry per source (filename, size_bytes,
    mtime_ns only).
    """
    if run_id is None:
        run_id = default_run_id()
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "polygons").mkdir()
    (run_dir / "analysis_observations").mkdir()
    (run_dir / "rejections").mkdir()
    (run_dir / "analysis").mkdir()
    (run_dir / "manifests").mkdir()
    state = RunState(run_dir=run_dir, run_id=run_id)
    state.metadata["run_id"] = run_id
    state.metadata["created_at"] = dt.datetime.now(tz=dt.UTC).isoformat()
    state.metadata["status"] = STATUS_INITIALIZED
    _atomic_write_json(run_dir / "manifests" / "run.json", state.metadata)
    _atomic_write_json(run_dir / "manifests" / "sources.json", [])
    if expected_sources is not None:
        entries = sorted(
            (_source_fingerprint_payload(fp) for fp in expected_sources),
            key=lambda e: str(e["filename"]),
        )
        _atomic_write_json(run_dir / "manifests" / "expected_sources.json", entries)
    return run_dir, state


def load_run(run_dir: Path) -> RunState:
    """Reload an existing run from ``run_dir``.

    Raises :class:`FileNotFoundError` if the run metadata is missing.
    """
    run_json = run_dir / "manifests" / "run.json"
    sources_json = run_dir / "manifests" / "sources.json"
    if not run_json.exists():
        raise FileNotFoundError(f"missing {run_json}")
    metadata: dict[str, Any] = json.loads(run_json.read_text())
    sources_raw = json.loads(sources_json.read_text()) if sources_json.exists() else []
    sources = {entry["filename"]: entry for entry in sources_raw}
    run_id = metadata.get("run_id", run_dir.name)
    return RunState(run_dir=run_dir, run_id=run_id, metadata=metadata, sources=sources)


def upsert_run_metadata(state: RunState, patch: dict[str, Any]) -> None:
    """Merge ``patch`` into ``state.metadata`` and write to disk.

    ``status`` transitions are validated against
    :data:`ALLOWED_TRANSITIONS`. To force a status change, call
    :func:`transition_status` explicitly.
    """
    if "status" in patch:
        raise ValueError(
            "use transition_status() to change run status; upsert_run_metadata "
            "refuses to set it implicitly"
        )
    state.metadata.update(patch)
    _atomic_write_json(state.run_dir / "manifests" / "run.json", state.metadata)


def transition_status(state: RunState, new_status: str) -> None:
    """Transition ``state`` to ``new_status``.

    Validates against :data:`ALLOWED_TRANSITIONS`. ``INCOMPLETE`` and
    ``COMPLETE`` are terminal.
    """
    if new_status not in STATUS_VALUES:
        raise ValueError(f"unknown run status: {new_status!r}")
    current = state.metadata.get("status", STATUS_INITIALIZED)
    if current == new_status:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise ValueError(f"illegal run-status transition: {current!r} -> {new_status!r}")
    state.metadata["status"] = new_status
    state.metadata["status_changed_at"] = dt.datetime.now(tz=dt.UTC).isoformat()
    _atomic_write_json(state.run_dir / "manifests" / "run.json", state.metadata)


def record_processed_source(
    state: RunState,
    fp: SourceFingerprint,
    *,
    public_row_count: int = 0,
    observation_row_count: int = 0,
    rejection_count: int = 0,
    started_at: str | None = None,
    finished_at: str | None = None,
    public_shard_sha256: str | None = None,
    observation_shard_sha256: str | None = None,
    rejection_shard_sha256: str | None = None,
    status: str = "extracted",
) -> None:
    """Append or update the sources manifest for one source PBF.

    Counts are mandatory; shard SHA-256 hashes are for the *output*
    shards, never for the source PBF itself.
    """
    entry: dict[str, Any] = _source_fingerprint_payload(fp)
    entry["public_row_count"] = public_row_count
    entry["observation_row_count"] = observation_row_count
    entry["rejection_count"] = rejection_count
    entry["status"] = status
    if started_at is not None:
        entry["started_at"] = started_at
    if finished_at is not None:
        entry["finished_at"] = finished_at
    if public_shard_sha256 is not None:
        entry["public_shard_sha256"] = public_shard_sha256
    if observation_shard_sha256 is not None:
        entry["observation_shard_sha256"] = observation_shard_sha256
    if rejection_shard_sha256 is not None:
        entry["rejection_shard_sha256"] = rejection_shard_sha256
    state.sources[fp.filename] = entry
    _write_sources_manifest(state)


def hash_shard(shard_path: Path) -> str:
    """Compute the SHA-256 of a Parquet shard's bytes."""
    import hashlib

    h = hashlib.sha256()
    with shard_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def update_public_shard_metadata(
    state: RunState,
    *,
    filename: str,
    row_count: int,
    shard_sha256: str,
) -> None:
    """Update only the enriched public shard fields for a processed source."""
    entry = state.sources.get(filename)
    if entry is None:
        raise ValueError(f"source is not processed: {filename}")
    entry["public_row_count"] = row_count
    entry["public_shard_sha256"] = shard_sha256
    _write_sources_manifest(state)


def source_is_unchanged(state: RunState, fp: SourceFingerprint) -> bool:
    """Return ``True`` if ``fp`` matches a previously recorded source.

    Comparison is exact equality of ``size_bytes`` AND ``mtime_ns``.
    """
    prior = state.sources.get(fp.filename)
    if prior is None:
        return False
    return prior.get("size_bytes") == fp.size_bytes and prior.get("mtime_ns") == fp.mtime_ns


def expected_source_inventory(run_dir: Path) -> list[dict[str, Any]]:
    """Return the parsed ``expected_sources.json`` content.

    Raises :class:`FileNotFoundError` if the inventory has not been
    written.
    """
    path = run_dir / "manifests" / "expected_sources.json"
    if not path.exists():
        raise FileNotFoundError(f"missing {path}")
    raw = json.loads(path.read_text())
    return list(raw)


def source_inventory_matches(run_dir: Path) -> bool:
    """Return ``True`` iff the sources manifest exactly matches the
    expected source inventory (same filenames, same sizes, same mtimes)."""
    try:
        expected = expected_source_inventory(run_dir)
    except FileNotFoundError:
        return False
    actual = json.loads((run_dir / "manifests" / "sources.json").read_text())
    actual_by_name = {e["filename"]: e for e in actual}
    if set(actual_by_name) != {e["filename"] for e in expected}:
        return False
    for entry in expected:
        a = actual_by_name[entry["filename"]]
        if a["size_bytes"] != entry["size_bytes"]:
            return False
        if a["mtime_ns"] != entry["mtime_ns"]:
            return False
    return True


__all__ = [
    "ALLOWED_TRANSITIONS",
    "STATUS_ANALYZED",
    "STATUS_CARD_BUILT",
    "STATUS_COMPLETE",
    "STATUS_ENRICHED",
    "STATUS_ENRICHING",
    "STATUS_EXTRACTED",
    "STATUS_EXTRACTING",
    "STATUS_INCOMPLETE",
    "STATUS_INITIALIZED",
    "STATUS_VALUES",
    "STATUS_VERIFIED",
    "RunState",
    "SourceFingerprint",
    "atomic_write_json",
    "default_run_id",
    "expected_source_inventory",
    "hash_shard",
    "initialise_run",
    "load_run",
    "record_processed_source",
    "snapshot_source_fingerprint",
    "source_inventory_matches",
    "source_is_unchanged",
    "transition_status",
    "update_public_shard_metadata",
    "upsert_run_metadata",
]

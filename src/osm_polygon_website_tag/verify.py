"""Strict, bounded verification of extraction artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .analyze import ANALYSIS_FILES
from .card import _render_markdown, _render_yaml_front_matter
from .card_stats import compute_card_stats
from .comparison_schema import COMPARISON_OBSERVATION_SCHEMA
from .polygon_schema import POLYGON_PUBLIC_SCHEMA
from .rejection_schema import REJECTION_SCHEMA
from .text_schema import TEXT_STATUSES, count_words


@dataclass
class VerificationReport:
    """Result of :func:`verify_results`."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    checked_shards: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ShardContract:
    """Complete verification contract for one per-source shard.

    Binds the user-facing verification label to its directory, its
    manifest row-count key, its manifest SHA-256 key, and the exact
    Arrow schema the shard must match.
    """

    kind: str
    directory: str
    count_key: str
    hash_key: str
    schema: pa.Schema


_SHARD_CONTRACTS: tuple[_ShardContract, ...] = (
    _ShardContract(
        kind="public",
        directory="polygons",
        count_key="public_row_count",
        hash_key="public_shard_sha256",
        schema=POLYGON_PUBLIC_SCHEMA,
    ),
    _ShardContract(
        kind="comparison",
        directory="analysis_observations",
        count_key="observation_row_count",
        hash_key="observation_shard_sha256",
        schema=COMPARISON_OBSERVATION_SCHEMA,
    ),
    _ShardContract(
        kind="rejection",
        directory="rejections",
        count_key="rejection_count",
        hash_key="rejection_shard_sha256",
        schema=REJECTION_SCHEMA,
    ),
)


def verify_results(run_dir: Path | str) -> VerificationReport:
    """Verify exact schemas, counts, hashes, inventory, and row invariants."""
    root = Path(run_dir)
    errors: list[str] = []
    checked: list[str] = []
    metadata = _read_json_object(root / "manifests" / "run.json", errors)
    manifest = _read_json_array(root / "manifests" / "sources.json", errors)
    if not manifest:
        errors.append("sources manifest is empty")

    declared: set[str] = set()
    for entry in manifest:
        filename = entry.get("filename")
        if not isinstance(filename, str) or not filename.endswith(".osm.pbf"):
            errors.append("manifest entry has invalid filename")
            continue
        stem = filename.removesuffix(".osm.pbf")
        declared.add(stem)
        for contract in _SHARD_CONTRACTS:
            kind = contract.kind
            directory = contract.directory
            count_key = contract.count_key
            hash_key = contract.hash_key
            schema = contract.schema
            path = root / directory / f"{stem}.parquet"
            checked.append(f"{kind}:{stem}")
            if not path.is_file():
                errors.append(f"missing {kind} shard: {path}")
                continue
            try:
                parquet = pq.ParquetFile(path)
                actual_schema = parquet.schema_arrow
                actual_count = int(parquet.metadata.num_rows)
            except Exception as exc:
                errors.append(f"unreadable {kind} shard {path}: {exc}")
                continue
            if not actual_schema.equals(schema, check_metadata=True):
                errors.append(f"exact schema mismatch in {kind} shard {path}")
            expected_count = entry.get(count_key)
            if not isinstance(expected_count, int) or isinstance(expected_count, bool):
                errors.append(f"invalid {count_key} for {filename}")
            elif actual_count != expected_count:
                errors.append(
                    f"{kind} row count mismatch for {filename}: "
                    f"manifest={expected_count}, parquet={actual_count}"
                )
            expected_hash = entry.get(hash_key)
            if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                errors.append(f"missing {kind} shard hash for {filename}")
            else:
                actual_hash = _hash_file(path)
                if actual_hash != expected_hash:
                    errors.append(
                        f"{kind} shard hash mismatch for {filename}: "
                        f"{actual_hash} != {expected_hash}"
                    )

    for contract in _SHARD_CONTRACTS:
        kind = contract.kind
        directory = contract.directory
        shard_dir = root / directory
        if not shard_dir.is_dir():
            errors.append(f"missing shard directory: {shard_dir}")
            continue
        for path in sorted(shard_dir.glob("*.parquet")):
            if path.stem not in declared:
                errors.append(f"extra undeclared {kind} shard: {path}")

    _verify_expected_inventory(root, manifest, errors)
    _verify_row_invariants(root, errors)
    if not metadata:
        errors.append("run metadata is empty")
    status = metadata.get("status")
    _verify_text_invariants(root, status, errors)
    if status in {"card_built", "verified", "complete"}:
        _verify_analysis_and_card(root, errors)
    if status == "complete":
        _verify_receipt(root, errors)
    return VerificationReport(not errors, errors, checked)


def _verify_text_invariants(root: Path, status: object, errors: list[str]) -> None:
    pending_forbidden = status in {
        "enriched",
        "analyzed",
        "card_built",
        "verified",
        "complete",
    }
    for shard in sorted((root / "polygons").glob("*.parquet")):
        try:
            parquet = pq.ParquetFile(shard)
            columns = [
                "website",
                "contact_website",
                "website_text",
                "website_word_count",
                "website_text_status",
                "contact_website_text",
                "contact_website_word_count",
                "contact_website_text_status",
            ]
            for batch in parquet.iter_batches(columns=columns, batch_size=512):
                for row in batch.to_pylist():
                    _verify_one_text_value(
                        tag_value=row["website"],
                        text=row["website_text"],
                        word_count=row["website_word_count"],
                        text_status=row["website_text_status"],
                        label=f"{shard.name}:website",
                        pending_forbidden=pending_forbidden,
                        errors=errors,
                    )
                    _verify_one_text_value(
                        tag_value=row["contact_website"],
                        text=row["contact_website_text"],
                        word_count=row["contact_website_word_count"],
                        text_status=row["contact_website_text_status"],
                        label=f"{shard.name}:contact_website",
                        pending_forbidden=pending_forbidden,
                        errors=errors,
                    )
        except Exception as exc:
            errors.append(f"text invariant verification failed for {shard.name}: {exc}")


def _verify_one_text_value(
    *,
    tag_value: object,
    text: object,
    word_count: object,
    text_status: object,
    label: str,
    pending_forbidden: bool,
    errors: list[str],
) -> None:
    if text_status not in TEXT_STATUSES:
        errors.append(f"{label} has invalid text status")
        return
    if tag_value is None:
        if text_status != "absent" or text is not None or word_count is not None:
            errors.append(f"{label} absent tag has inconsistent text fields")
        return
    if text_status == "absent":
        errors.append(f"{label} present tag has absent text status")
    if text_status == "pending" and pending_forbidden:
        errors.append(f"{label} remains pending after enrichment")
    if text_status == "success":
        if not isinstance(text, str):
            errors.append(f"{label} success has no text")
        elif (
            not isinstance(word_count, int)
            or isinstance(word_count, bool)
            or word_count != count_words(text)
        ):
            errors.append(f"{label} word count does not match stored text")
    elif text_status == "empty":
        if text != "" or word_count != 0:
            errors.append(f"{label} empty result has inconsistent text fields")
    elif text is not None or word_count is not None:
        errors.append(f"{label} non-success status must have null text and word count")


def _verify_expected_inventory(
    root: Path,
    manifest: list[dict[str, Any]],
    errors: list[str],
) -> None:
    path = root / "manifests" / "expected_sources.json"
    if not path.exists():
        return
    expected = _read_json_array(path, errors)

    def identity(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
        return (
            entry.get("filename"),
            entry.get("size_bytes"),
            entry.get("mtime_ns"),
        )

    if sorted(map(identity, expected)) != sorted(map(identity, manifest)):
        errors.append("processed sources do not exactly match expected source inventory")


def _verify_row_invariants(root: Path, errors: list[str]) -> None:
    con = duckdb.connect(":memory:")
    try:
        for directory, predicate, label in (
            (
                "polygons",
                """has_any_website IS DISTINCT FROM
                     (has_website OR has_contact_website)
                   OR has_website IS DISTINCT FROM
                     (website IS NOT NULL AND trim(website) <> '')
                   OR has_contact_website IS DISTINCT FROM
                     (contact_website IS NOT NULL AND trim(contact_website) <> '')
                   OR NOT has_any_website
                   OR preferred_website_source NOT IN ('website', 'contact:website')
                   OR preferred_website IS DISTINCT FROM
                     CASE WHEN has_website THEN website ELSE contact_website END
                   OR osm_type NOT IN ('way', 'relation')
                   OR NOT isfinite(lat) OR NOT isfinite(lon)
                   OR NOT isfinite(area_m2) OR area_m2 < 0""",
                "public",
            ),
            (
                "analysis_observations",
                """has_any_website IS DISTINCT FROM
                     (has_website OR has_contact_website)
                   OR has_website IS DISTINCT FROM
                     (website IS NOT NULL AND trim(website) <> '')
                   OR has_contact_website IS DISTINCT FROM
                     (contact_website IS NOT NULL AND trim(contact_website) <> '')
                   OR has_wikidata IS DISTINCT FROM
                     (wikidata IS NOT NULL AND trim(wikidata) <> '')
                   OR (NOT has_any_website AND NOT has_wikidata)
                   OR osm_type NOT IN ('way', 'relation')""",
                "comparison",
            ),
            (
                "rejections",
                """has_any_website IS DISTINCT FROM
                     (has_website OR has_contact_website)
                   OR has_website IS DISTINCT FROM
                     (website IS NOT NULL AND trim(website) <> '')
                   OR has_contact_website IS DISTINCT FROM
                     (contact_website IS NOT NULL AND trim(contact_website) <> '')
                   OR osm_type NOT IN ('way', 'relation')
                   OR rejection_kind IS NULL OR rejection_kind = ''""",
                "rejection",
            ),
        ):
            files = sorted((root / directory).glob("*.parquet"))
            if not files:
                continue
            paths = [str(path) for path in files]
            count = con.execute(
                f"SELECT COUNT(*) FROM read_parquet(?) WHERE {predicate}",  # noqa: S608
                [paths],
            ).fetchone()
            if count and int(count[0]) != 0:
                errors.append(f"{label} row invariant violations: {int(count[0])}")
    except Exception as exc:
        errors.append(f"row invariant verification failed: {exc}")
    finally:
        con.close()


def _verify_analysis_and_card(root: Path, errors: list[str]) -> None:
    if not (root / "manifests" / "expected_sources.json").is_file():
        errors.append("missing exact expected source inventory")
    actual = {path.name for path in (root / "analysis").glob("*.parquet")}
    expected = set(ANALYSIS_FILES)
    for name in sorted(expected - actual):
        errors.append(f"missing analysis artifact: analysis/{name}")
    for name in sorted(actual - expected):
        errors.append(f"unexpected analysis artifact: analysis/{name}")
    for name in ("README.md", "dataset.yaml"):
        if not (root / name).is_file():
            errors.append(f"missing card artifact: {name}")
    readable = True
    for name in sorted(actual & expected):
        try:
            pq.ParquetFile(root / "analysis" / name)
        except Exception as exc:
            readable = False
            errors.append(f"unreadable analysis artifact {name}: {exc}")
    if actual == expected and readable:
        try:
            _verify_analysis_arithmetic(root, errors)
        except Exception as exc:
            errors.append(f"analysis arithmetic verification failed: {exc}")
    try:
        stats = compute_card_stats(root)
        expected_yaml = _render_yaml_front_matter(stats)
        expected_readme = expected_yaml + "\n" + _render_markdown(stats)
        if (root / "dataset.yaml").is_file() and (
            root / "dataset.yaml"
        ).read_text() != expected_yaml:
            errors.append("dataset.yaml does not match artifact-derived statistics")
        if (root / "README.md").is_file() and (root / "README.md").read_text() != expected_readme:
            errors.append("README.md does not match artifact-derived statistics")
    except Exception as exc:
        errors.append(f"card statistic verification failed: {exc}")


def _verify_analysis_arithmetic(root: Path, errors: list[str]) -> None:
    cells = pq.read_table(root / "analysis" / "cells_global.parquet").to_pylist()
    expected_cells = {
        "cell_000_w0_c0_d0",
        "cell_001_w0_c0_d1",
        "cell_010_w0_c1_d0",
        "cell_011_w0_c1_d1",
        "cell_100_w1_c0_d0",
        "cell_101_w1_c0_d1",
        "cell_110_w1_c1_d0",
        "cell_111_w1_c1_d1",
    }
    for level in ("observation", "canonical"):
        level_rows = [row for row in cells if row.get("level") == level]
        if {row.get("cell") for row in level_rows} != expected_cells:
            errors.append(f"{level} analysis does not contain exactly eight cells")
            continue
        total = sum(int(row["row_count"]) for row in level_rows)
        if level == "observation":
            manifest = json.loads((root / "manifests" / "sources.json").read_text())
            expected_total = sum(int(entry["observation_row_count"]) for entry in manifest)
            if total != expected_total:
                errors.append(f"observation cell total mismatch: {total} != {expected_total}")
        elif total > sum(
            int(row["row_count"]) for row in cells if row.get("level") == "observation"
        ):
            errors.append("canonical cell total exceeds observation total")


def _verify_receipt(root: Path, errors: list[str]) -> None:
    path = root / "manifests" / "completion_receipt.json"
    receipt = _read_json_object(path, errors)
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("completion receipt has no artifact list")
        return
    seen: set[str] = set()
    canonical_entries: list[dict[str, Any]] = []
    for entry in artifacts:
        if not isinstance(entry, dict):
            errors.append("invalid completion receipt artifact entry")
            continue
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            errors.append("unsafe completion receipt path")
            continue
        if relative in seen:
            errors.append(f"duplicate completion receipt path: {relative}")
            continue
        seen.add(relative)
        artifact = root / relative
        if not artifact.is_file():
            errors.append(f"missing receipt-bound artifact: {relative}")
            continue
        size = artifact.stat().st_size
        digest = _hash_file(artifact)
        if entry.get("size_bytes") != size or entry.get("sha256") != digest:
            errors.append(f"receipt-bound artifact mismatch: {relative}")
        canonical_entries.append({"path": relative, "size_bytes": size, "sha256": digest})
    expected_paths = _current_publishable_relative_paths(root)
    if seen != expected_paths:
        errors.append("completion receipt artifact inventory mismatch")
    canonical = json.dumps(
        sorted(canonical_entries, key=lambda item: str(item["path"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    if receipt.get("manifest_digest") != hashlib.sha256(canonical.encode()).hexdigest():
        errors.append("completion receipt digest mismatch")


def _current_publishable_relative_paths(root: Path) -> set[str]:
    result: set[str] = set()
    for directory in (
        "polygons",
        "analysis_observations",
        "rejections",
        "analysis",
        "manifests",
    ):
        for path in (root / directory).glob("*"):
            if path.is_file() and path.name != "completion_receipt.json":
                result.add(path.relative_to(root).as_posix())
    for name in ("README.md", "dataset.yaml", "failures.jsonl"):
        if (root / name).is_file():
            result.add(name)
    return result


def _read_json_object(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON object {path}: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"expected JSON object: {path}")
        return {}
    return value


def _read_json_array(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON array {path}: {exc}")
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        errors.append(f"expected array of objects: {path}")
        return []
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

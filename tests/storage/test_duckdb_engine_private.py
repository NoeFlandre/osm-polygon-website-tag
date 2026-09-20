"""Focused contracts for the bounded DuckDB engine helpers."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from osm_polygon_website_tag.storage import duckdb_engine
from osm_polygon_website_tag.storage.duckdb_engine import (
    DUCKDB_REPORTING_THREADS,
    _make_connection,
    canonical_observations,
    cells_global_canonical,
    cells_global_observation,
    cleanup_temp_dir,
    copy_query_atomic,
    ensure_temp_dir,
    fresh_connection,
    register_comparison_parquets,
    register_public_parquets,
    register_rejection_parquets,
    reporting_connection,
)


def _settings(connection: duckdb.DuckDBPyConnection) -> tuple[object, ...]:
    row = connection.execute(
        """
        SELECT current_setting('memory_limit'),
               current_setting('threads'),
               current_setting('temp_directory'),
               current_setting('enable_progress_bar')
        """
    ).fetchone()
    assert row is not None
    return row


def test_make_connection_applies_all_resource_settings(tmp_path: Path) -> None:
    temp_dir = tmp_path / "duckdb'quoted"
    connection = _make_connection(temp_dir, memory_limit="32MB", threads=3)
    try:
        memory_limit, threads, configured_temp_dir, progress_bar = _settings(connection)
        assert str(memory_limit) == "30.5 MiB"
        assert threads == 3
        assert configured_temp_dir == str(temp_dir)
        assert progress_bar is False
        assert temp_dir.is_dir()
    finally:
        connection.close()


def test_make_connection_emits_the_expected_progress_setting(monkeypatch, tmp_path: Path) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

    fake = FakeConnection()
    monkeypatch.setattr(duckdb_engine.duckdb, "connect", lambda **_: fake)

    assert _make_connection(tmp_path / "duckdb") is fake
    assert fake.statements[-1] == "SET enable_progress_bar = false"


def test_connection_wrappers_use_the_run_owned_staging_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    analysis = fresh_connection(run_dir)
    reporting = reporting_connection(run_dir)
    try:
        expected = str(run_dir / "staging" / "duckdb")
        assert _settings(analysis)[2] == expected
        assert _settings(reporting)[1] == DUCKDB_REPORTING_THREADS
        assert _settings(reporting)[2] == expected
    finally:
        analysis.close()
        reporting.close()


def test_cell_wrappers_pass_their_exact_view_names(monkeypatch) -> None:
    seen: list[str] = []

    def fake_cells(_connection: duckdb.DuckDBPyConnection, view: str) -> list[dict[str, int]]:
        seen.append(view)
        return [{"value": 1}]

    monkeypatch.setattr(duckdb_engine, "_cells_global", fake_cells)
    connection = cast(duckdb.DuckDBPyConnection, object())

    assert cells_global_observation(connection) == [{"value": 1}]
    assert cells_global_canonical(connection) == [{"value": 1}]
    assert seen == ["observations", "canonical_observations"]


def test_empty_registration_views_have_stable_schemas(tmp_path: Path) -> None:
    connection = _make_connection(tmp_path / "duckdb")
    try:
        register_comparison_parquets(connection, tmp_path / "comparison")
        register_public_parquets(connection, tmp_path / "public")
        register_rejection_parquets(connection, tmp_path / "rejections")

        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM public_polygons").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM rejection_rows").fetchone() == (0,)
        assert [row[0] for row in connection.execute("DESCRIBE rejection_rows").fetchall()] == [
            "source_pbf",
            "rejection_kind",
        ]
    finally:
        connection.close()


def test_registration_reads_nonempty_files_and_escapes_quoted_paths(tmp_path: Path) -> None:
    connection = _make_connection(tmp_path / "duckdb")
    comparison = tmp_path / "comparison'quoted"
    public = tmp_path / "public'quoted"
    rejections = tmp_path / "rejections'quoted"
    comparison.mkdir()
    public.mkdir()
    rejections.mkdir()
    pq.write_table(pa.table({"source_pbf": ["comparison.pbf"]}), comparison / "one.parquet")
    pq.write_table(pa.table({"polygon_id": ["polygon/1"]}), public / "one.parquet")
    pq.write_table(
        pa.table({"source_pbf": ["rejected.pbf"], "rejection_kind": ["invalid"]}),
        rejections / "one.parquet",
    )
    try:
        register_comparison_parquets(connection, comparison)
        register_public_parquets(connection, public)
        register_rejection_parquets(connection, rejections)
        assert connection.execute("SELECT source_pbf FROM observations").fetchall() == [
            ("comparison.pbf",)
        ]
        assert connection.execute("SELECT polygon_id FROM public_polygons").fetchall() == [
            ("polygon/1",)
        ]
        assert connection.execute("SELECT * FROM rejection_rows").fetchall() == [
            ("rejected.pbf", "invalid")
        ]
    finally:
        connection.close()


def test_cells_global_returns_zeroes_for_empty_observations(tmp_path: Path) -> None:
    connection = _make_connection(tmp_path / "duckdb")
    try:
        connection.execute(
            """
            CREATE VIEW observations AS
            SELECT CAST(NULL AS BOOLEAN) AS has_website,
                   CAST(NULL AS BOOLEAN) AS has_contact_website,
                   CAST(NULL AS BOOLEAN) AS has_wikidata
            WHERE FALSE
            """
        )
        cells = cells_global_observation(connection)
        assert len(cells) == 1
        assert set(cells[0]) == set(duckdb_engine.EIGHT_CELL_EXPRESSIONS)
        assert set(cells[0].values()) == {0}
    finally:
        connection.close()


def test_cells_global_counts_every_cell_and_canonicalizes_duplicates(tmp_path: Path) -> None:
    connection = _make_connection(tmp_path / "duckdb")
    try:
        connection.execute(
            """
            CREATE TABLE observations (
              osm_type VARCHAR, osm_id BIGINT, osm_version INTEGER,
              osm_timestamp TIMESTAMP, source_pbf VARCHAR,
              has_website BOOLEAN, has_contact_website BOOLEAN, has_wikidata BOOLEAN
            )
            """
        )
        rows = [
            ("way", index, 1, f"source-{index}", bool(index & 4), bool(index & 2), bool(index & 1))
            for index in range(8)
        ]
        connection.executemany(
            "INSERT INTO observations VALUES (?, ?, ?, TIMESTAMP '2024-01-01', ?, ?, ?, ?)",
            rows,
        )
        connection.execute(
            "INSERT INTO observations VALUES ('way', 0, 0, TIMESTAMP '2023-01-01', 'old', true, true, true)"
        )

        observation_cells = cells_global_observation(connection)[0]
        assert sorted(observation_cells.values()) == [1] * 7 + [2]

        canonical_observations(connection)
        canonical_cells = cells_global_canonical(connection)[0]
        assert set(canonical_cells.values()) == {1}
    finally:
        connection.close()


def test_copy_query_atomic_removes_temporary_output_after_promotion_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _make_connection(tmp_path / "duckdb")
    destination = tmp_path / "nested" / "result.parquet"

    def fail_promotion(_temporary: Path, _destination: Path) -> None:
        raise RuntimeError("promotion failed")

    monkeypatch.setattr(duckdb_engine, "atomic_write_file", fail_promotion)
    try:
        with pytest.raises(RuntimeError, match="promotion failed"):
            copy_query_atomic(connection, "SELECT 1 AS value", destination)
    finally:
        connection.close()

    assert not destination.exists()
    assert not destination.with_name(f".{destination.name}.tmp.parquet").exists()


def test_copy_query_atomic_creates_every_missing_parent_level(tmp_path: Path) -> None:
    """A destination several levels below an existing directory must still be written.

    ``mkdir`` without ``parents=True`` happens to succeed when exactly one level
    is missing, so only a deeper destination pins the recursive creation down.
    """
    connection = _make_connection(tmp_path / "duckdb")
    destination = tmp_path / "one" / "two" / "three" / "result.parquet"
    try:
        copy_query_atomic(connection, "SELECT 1 AS value", destination)
    finally:
        connection.close()

    assert pq.read_table(destination).to_pylist() == [{"value": 1}]


def test_copy_query_atomic_escapes_quoted_output_paths(tmp_path: Path) -> None:
    connection = _make_connection(tmp_path / "duckdb")
    destination = tmp_path / "quoted'parent" / "result.parquet"
    try:
        copy_query_atomic(connection, "SELECT 1 AS value", destination)
    finally:
        connection.close()

    assert pq.read_table(destination).to_pylist() == [{"value": 1}]


def test_copy_query_atomic_passes_the_exact_temporary_path_to_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _make_connection(tmp_path / "duckdb")
    destination = tmp_path / "nested" / "result.parquet"
    calls: list[tuple[Path, Path, bool]] = []

    def promote(temporary: Path, target: Path) -> None:
        calls.append((temporary, target, temporary.is_file()))
        temporary.replace(target)

    monkeypatch.setattr(duckdb_engine, "atomic_write_file", promote)
    try:
        copy_query_atomic(connection, "SELECT 1 AS value", destination)
    finally:
        connection.close()

    assert calls == [
        (
            destination.with_name(f".{destination.name}.tmp.parquet"),
            destination,
            True,
        )
    ]
    assert pq.read_table(destination).to_pylist() == [{"value": 1}]


def test_copy_query_atomic_preserves_the_original_copy_error_when_no_temp_exists(
    tmp_path: Path,
) -> None:
    class FailingConnection:
        def execute(self, _query: str) -> None:
            raise RuntimeError("copy failed")

    destination = tmp_path / "nested" / "result.parquet"
    with pytest.raises(RuntimeError, match="copy failed"):
        copy_query_atomic(
            cast(duckdb.DuckDBPyConnection, FailingConnection()), "SELECT 1", destination
        )


def test_temp_directory_lifecycle_preserves_nonempty_failure_evidence(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    assert cleanup_temp_dir(run_dir) is False
    staging = ensure_temp_dir(run_dir)
    assert staging == run_dir / "staging" / "duckdb"
    assert staging.is_dir()
    assert cleanup_temp_dir(run_dir) is True

    staging = ensure_temp_dir(run_dir)
    sentinel = staging / "spill.tmp"
    sentinel.write_text("evidence", encoding="utf-8")
    assert cleanup_temp_dir(run_dir) is False
    assert sentinel.read_text(encoding="utf-8") == "evidence"
    sentinel.unlink()
    assert cleanup_temp_dir(run_dir) is True


def test_ensure_temp_dir_uses_idempotent_recursive_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = tmp_path / "run" / "staging" / "duckdb"
    calls: list[tuple[int, bool, bool]] = []
    original_mkdir = Path.mkdir

    def spy_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == expected:
            calls.append((mode, parents, exist_ok))
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", spy_mkdir)
    assert ensure_temp_dir(tmp_path / "run") == expected
    assert calls[0] == (0o777, True, True)


def test_temp_directory_helpers_use_the_exact_staging_path() -> None:
    class PathSpy:
        created: ClassVar[list[PathSpy]] = []

        def __init__(self, parts: tuple[str, ...] = ()) -> None:
            self.parts = parts
            self.created.append(self)

        def __truediv__(self, part: str) -> PathSpy:
            return PathSpy((*self.parts, part))

        def exists(self) -> bool:
            return True

        def rmdir(self) -> None:
            return None

    run_dir = PathSpy(("run",))
    assert cleanup_temp_dir(cast(Path, run_dir)) is True
    assert PathSpy.created[-1].parts == ("run", "staging", "duckdb")

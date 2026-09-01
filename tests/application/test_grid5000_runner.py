"""Tests for the dependency-light Grid'5000 runner entry point."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_website_tag.application import grid5000_runner
from osm_polygon_website_tag.application.grid5000_runner import main


def test_main_delegates_bundle_options_and_emits_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[Path, dict[str, object]]] = []
    result = SimpleNamespace(payload=lambda: {"completed": False, "processed_rows": 2})

    def run_bundle(bundle_dir: Path, **kwargs: object) -> SimpleNamespace:
        calls.append((bundle_dir, kwargs))
        return result

    monkeypatch.setattr(grid5000_runner, "run_language_bundle", run_bundle)

    assert (
        main(
            [
                "--bundle-dir",
                str(tmp_path / "bundle"),
                "--time-budget-seconds",
                "1500",
                "--batch-rows",
                "256",
                "--job-id",
                "123",
            ]
        )
        == 0
    )

    assert calls == [
        (
            tmp_path / "bundle",
            {"time_budget_seconds": 1500.0, "batch_rows": 256, "job_id": "123"},
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"completed": False, "processed_rows": 2}


def test_main_reports_invalid_bundle_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise ValueError("invalid bundle")

    monkeypatch.setattr(grid5000_runner, "run_language_bundle", fail)

    assert main(["--bundle-dir", "/tmp/bundle"]) == 2
    assert capsys.readouterr().err == "error: invalid bundle\n"

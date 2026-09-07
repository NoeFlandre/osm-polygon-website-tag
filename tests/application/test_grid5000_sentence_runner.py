"""Tests for the dependency-light Grid'5000 sentence runner entry point."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from osm_polygon_website_tag.application import grid5000_sentence_runner
from osm_polygon_website_tag.application.grid5000_sentence_runner import main


def _stub_bundle(filename: str = "sat-3l-sm", revision: str = "137da05") -> SimpleNamespace:
    return SimpleNamespace(model=SimpleNamespace(filename=filename, revision=revision))


def test_main_loads_the_staged_model_and_emits_a_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loaded: list[tuple[Path, str]] = []
    calls: list[tuple[Path, dict[str, object]]] = []
    splitter = object()
    result = SimpleNamespace(
        payload=lambda: {"shards": [], "completed": True, "path": Path("/tmp/x")}
    )

    bundles: list[Path] = []
    monkeypatch.setattr(
        grid5000_sentence_runner,
        "load_sentence_bundle",
        lambda bundle_dir: bundles.append(bundle_dir) or _stub_bundle(),
    )
    monkeypatch.setattr(
        grid5000_sentence_runner,
        "load_sat_splitter_from_path",
        lambda model_dir, *, revision: loaded.append((model_dir, revision)) or splitter,
    )

    def run_bundle(bundle_dir: Path, **kwargs: object) -> SimpleNamespace:
        calls.append((bundle_dir, kwargs))
        return result

    monkeypatch.setattr(grid5000_sentence_runner, "run_sentence_bundle", run_bundle)

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

    assert bundles == [tmp_path / "bundle"]
    assert loaded == [(tmp_path / "bundle" / "sat-3l-sm", "137da05")]
    assert calls == [
        (
            tmp_path / "bundle",
            {
                "splitter": splitter,
                "time_budget_seconds": 1500.0,
                "batch_rows": 256,
                "job_id": "123",
            },
        )
    ]
    assert capsys.readouterr().out == (
        '{\n  "completed": true,\n  "path": "/tmp/x",\n  "shards": []\n}\n'
    )


def test_parser_requires_a_bundle_and_describes_the_runner() -> None:
    parser = grid5000_sentence_runner._parser()

    with pytest.raises(SystemExit) as error:
        parser.parse_args([])

    assert error.value.code == 2
    assert "sentence" in parser.format_help()


def test_main_reports_invalid_bundle_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise ValueError("invalid bundle")

    monkeypatch.setattr(grid5000_sentence_runner, "load_sentence_bundle", fail)

    assert main(["--bundle-dir", "/tmp/bundle"]) == 2
    assert capsys.readouterr().err == "error: invalid bundle\n"

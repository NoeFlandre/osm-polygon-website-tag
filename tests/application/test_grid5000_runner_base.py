import json
from pathlib import Path

import pytest

from osm_polygon_website_tag.application.grid5000_runner_base import (
    base_runner_parser,
    print_receipt,
)


def test_base_runner_parser_accepts_the_shared_bundle_flags() -> None:
    args = base_runner_parser("desc").parse_args(
        [
            "--bundle-dir",
            "bundle",
            "--time-budget-seconds",
            "12.5",
            "--batch-rows",
            "7",
            "--job-id",
            "42",
        ]
    )

    assert args.bundle_dir == Path("bundle")
    assert args.time_budget_seconds == 12.5
    assert args.batch_rows == 7
    assert args.job_id == "42"


def test_base_runner_parser_requires_a_bundle_dir() -> None:
    with pytest.raises(SystemExit):
        base_runner_parser(None).parse_args([])


def test_print_receipt_prints_sorted_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert print_receipt(lambda: {"b": 1, "a": Path("x")}) == 0

    out = capsys.readouterr().out
    assert json.loads(out) == {"a": "x", "b": 1}
    assert out == json.dumps({"a": "x", "b": 1}, indent=2, sort_keys=True) + "\n"


def test_print_receipt_maps_value_error_to_exit_two(capsys: pytest.CaptureFixture[str]) -> None:
    def fail() -> dict[str, object]:
        raise ValueError("bad bundle")

    assert print_receipt(fail) == 2
    assert capsys.readouterr().err == "error: bad bundle\n"

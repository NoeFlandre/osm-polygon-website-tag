"""Bounded-run options: each command accepts exactly its documented type, once per type."""

from __future__ import annotations

import pytest
import typer
import typer.main
from typer.core import TyperOption
from typer.testing import CliRunner

from osm_polygon_website_tag.application.cli import app

TIME_BUDGET = "--time-budget-seconds"
BATCH_ROWS = "--batch-rows"

# The type each command documents for each option, as the help output shows it (`<int>`
# or `<float>`). The prepare commands take whole seconds, because the bundle schema stores
# an integer. The run and detection commands take fractional seconds.
DOCUMENTED_TYPES: dict[str, dict[str, str]] = {
    "grid5000-prepare": {TIME_BUDGET: "int", BATCH_ROWS: "int"},
    "grid5000-prepare-sentences": {TIME_BUDGET: "int", BATCH_ROWS: "int"},
    "grid5000-run": {TIME_BUDGET: "float", BATCH_ROWS: "int"},
    "grid5000-run-sentences": {TIME_BUDGET: "float", BATCH_ROWS: "int"},
    "detect-languages": {TIME_BUDGET: "float", BATCH_ROWS: "int"},
    "segment-sentences": {TIME_BUDGET: "float", BATCH_ROWS: "int"},
}
CASES = [
    (command, flag, kind)
    for command, flags in DOCUMENTED_TYPES.items()
    for flag, kind in flags.items()
]


def _option(command: str, flag: str) -> TyperOption:
    """Return one command's option, found by its flag."""
    click_command = typer.main.get_command(app).commands[command]  # ty: ignore[unresolved-attribute]
    matches = [param for param in click_command.params if flag in param.opts]
    assert len(matches) == 1
    option = matches[0]
    assert isinstance(option, TyperOption)
    return option


@pytest.mark.parametrize(("command", "flag", "kind"), CASES)
def test_option_has_exactly_its_documented_type(command: str, flag: str, kind: str) -> None:
    assert _option(command, flag).type.name == kind


@pytest.mark.parametrize(("command", "flag", "kind"), CASES)
def test_help_output_documents_the_same_type(command: str, flag: str, kind: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"], color=False, terminal_width=100)

    assert f"{flag} <{kind}>" in result.output


@pytest.mark.parametrize(("command", "flag", "kind"), CASES)
def test_option_accepts_only_values_of_its_type(command: str, flag: str, kind: str) -> None:
    convert = _option(command, flag).type.convert

    assert convert("3", None, None) == 3
    if kind == "int":
        with pytest.raises(typer.BadParameter):
            convert("1.5", None, None)
    else:
        assert convert("1.5", None, None) == 1.5


def test_each_type_of_bounded_option_has_exactly_one_declaration() -> None:
    """Commands that take one type share its help text.

    The optional form (default None) is its own declaration, so it is keyed apart.
    There are four declarations: whole and optional-fractional seconds, and batch rows
    with a default and as an optional override.
    """
    texts: dict[tuple[str, str, bool], set[str]] = {}
    for command, flag, kind in CASES:
        option = _option(command, flag)
        texts.setdefault((flag, kind, option.default is None), set()).add(option.help or "")

    assert len(texts) == 4
    assert all(len(found) == 1 for found in texts.values())

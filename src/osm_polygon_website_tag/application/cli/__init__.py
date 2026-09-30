"""Typed CLI application and shared option definitions."""

from __future__ import annotations

import json
import logging
import os
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
from huggingface_hub.errors import HfHubHTTPError
from rich.console import Console

from osm_polygon_website_tag.publishing.errors import TrackioUnavailableError
from osm_polygon_website_tag.runtime.config import Settings
from osm_polygon_website_tag.runtime.paths import data_root_source

app = typer.Typer(
    name="osm-polygon-website-tag",
    help="Analyze and publish OSM polygons carrying website tags.",
    no_args_is_help=True,
    rich_markup_mode=None,
)

_error_console = Console(stderr=True, markup=False, highlight=False)

EXIT_INVALID_INPUT = 3

EXIT_REMOTE = 4

EXIT_MISSING_DEPENDENCY = 5

EXIT_INTERRUPTED = 130

_EXIT_CODES: tuple[tuple[type[Exception], int], ...] = (
    (HfHubHTTPError, EXIT_REMOTE),
    (httpx.HTTPError, EXIT_REMOTE),
    (TrackioUnavailableError, EXIT_MISSING_DEPENDENCY),
    (ValueError, EXIT_INVALID_INPUT),
    (OSError, EXIT_INVALID_INPUT),
)

_HANDLED_ERRORS = tuple(error_type for error_type, _code in _EXIT_CODES)

DEBUG_ENV = "OSM_PWT_DEBUG"

_CLICK_EXCEPTION = cast(
    "type[Exception]",
    next(kind for kind in typer.BadParameter.__mro__ if kind.__name__ == "ClickException"),
)

_debug = {"enabled": False}

_quiet = {"enabled": False}

_DISTRIBUTION = "osm-polygon-website-tag"

_LOGGER = logging.getLogger("osm_polygon_website_tag")

_VERBOSITY_LEVELS = (logging.WARNING, logging.INFO, logging.DEBUG)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(package_version(_DISTRIBUTION))
        raise typer.Exit


@app.callback()
def _global_options(
    version: Annotated[  # noqa: ARG001 - consumed by its eager callback
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Print the package version and exit.",
        ),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "-v", "--verbose", count=True, help="More log detail on stderr; -vv shows DEBUG."
        ),
    ] = 0,
    quiet: Annotated[
        bool,
        typer.Option("-q", "--quiet", help="Only errors on stderr, and no progress output."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", help=f"Show full tracebacks (also: {DEBUG_ENV}=1)."),
    ] = False,
) -> None:
    """Analyze and publish OSM polygons carrying website tags."""
    if verbose and quiet:
        raise typer.BadParameter("--verbose and --quiet cannot be combined")
    _debug["enabled"] = debug
    _quiet["enabled"] = quiet
    _configure_logging(_log_level(verbose, quiet=quiet))
    _LOGGER.info("%s %s", _DISTRIBUTION, package_version(_DISTRIBUTION))
    _LOGGER.debug("data root: %s", data_root_source())


def _log_level(verbose: int, *, quiet: bool) -> int:
    if quiet:
        return logging.ERROR
    return _VERBOSITY_LEVELS[min(verbose, len(_VERBOSITY_LEVELS) - 1)]


def _configure_logging(level: int) -> None:
    """Log the package to the current stderr, so stdout stays pure JSON."""
    _reset_logging()
    handler = logging.StreamHandler()  # no argument: the current sys.stderr
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(level)


def _reset_logging() -> None:
    for handler in list(_LOGGER.handlers):
        _LOGGER.removeHandler(handler)
    _LOGGER.setLevel(logging.NOTSET)


RunDir = Annotated[Path, typer.Option("--run-dir", help="Existing run directory.")]

RepoId = Annotated[
    str | None,
    typer.Option(
        "--repo-id", help="Hugging Face dataset repository (defaults to HF_DATASET_REPO)."
    ),
]


def _configured_hf_dataset_repo(repo_id: str | None) -> str:
    """Resolve an explicit CLI override or the current environment/.env setting."""
    return repo_id if repo_id is not None else Settings().hf_dataset_repo


def _json(payload: Any, *, sort_keys: bool = False) -> None:
    typer.echo(json.dumps(payload, default=str, indent=2, sort_keys=sort_keys))


def exit_code_for(error: Exception) -> int:
    """Map a handled error to its documented exit code."""
    return next(code for error_type, code in _EXIT_CODES if isinstance(error, error_type))


def main(argv: list[str] | None = None) -> int:
    """Run the Typer app and turn every expected failure into one stderr line."""
    try:
        return _run_app(argv)
    except _HANDLED_ERRORS as exc:
        if _debug_requested():
            raise
        _error_console.print(f"error: {exc}")
        return exit_code_for(exc)
    finally:
        # Global options apply to one invocation only.
        _debug["enabled"] = False
        _quiet["enabled"] = False
        _reset_logging()


def _run_app(argv: list[str] | None) -> int:
    try:
        result = app(args=argv, prog_name="osm-polygon-website-tag", standalone_mode=False)
    except _CLICK_EXCEPTION as exc:
        return _show_click_error(exc)
    except (KeyboardInterrupt, typer.Abort):
        _error_console.print("interrupted")
        return EXIT_INTERRUPTED
    except SystemExit as exc:
        return int(exc.code or 0)
    return _as_exit_code(result)


def _as_exit_code(result: object) -> int:
    """Without standalone mode, Click returns an Exit's code instead of raising."""
    return result if isinstance(result, int) else 0


def _show_click_error(error: Any) -> int:
    error.show()
    return int(error.exit_code)


def _debug_requested() -> bool:
    return _debug["enabled"] or os.environ.get(DEBUG_ENV) == "1"


def _register_commands() -> None:
    from . import grid5000, languages, publish, run, sentences, verify

    app.command(
        "init",
        epilog='Example: osm-polygon-website-tag init --source-root /path/to/pbf-root --output-root "${OSM_POLY_DATA_DIR:-./data}/runs" --run-id website-v1',
    )(run.init_command)
    app.command(
        "extract",
        epilog='Example: osm-polygon-website-tag extract region.osm.pbf --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/website-v1"',
    )(run.extract_command)
    app.command("analyze-results")(verify.analyze_command)
    app.command("build-card")(verify.card_command)
    app.command("verify-results")(verify.verify_command)
    app.command("refresh-card")(verify.refresh_card_command)
    app.command("finalize-run")(verify.finalize_command)
    app.command("finalize-snapshot")(verify.finalize_snapshot_command)
    app.command("publish-plan")(publish.publish_plan_command)
    app.command(
        "publish",
        epilog='Example: osm-polygon-website-tag publish --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/website-v1" --apply',
    )(publish.publish_command)
    app.command(
        "release-stats",
        epilog='Example: osm-polygon-website-tag release-stats --run-dir "${OSM_POLY_DATA_DIR:-./data}/runs/website-v1"',
    )(publish.release_stats_command)
    app.command(
        "create-repo",
        epilog="Example: osm-polygon-website-tag create-repo --repo-id owner/name --apply",
    )(publish.create_repo_command)
    app.command("card-stats")(verify.card_stats_command)
    app.command("geometry-stats")(verify.geometry_stats_command)
    app.command("publish-trackio")(publish.publish_trackio_command)
    app.command(
        "run-all",
        epilog='Example: osm-polygon-website-tag run-all --source-root /path/to/pbf-root --output-root "${OSM_POLY_DATA_DIR:-./data}/runs" --run-id website-v1',
    )(run.run_all_command)
    app.command("detect-languages")(languages.detect_languages_command)
    app.command("segment-sentences")(sentences.segment_sentences_command)
    app.command(
        "grid5000-prepare",
        epilog='Example: osm-polygon-website-tag grid5000-prepare --run-dir <run> --bundle-dir <bundle> --model-path <model_v3.bin> --commit "$(git rev-parse HEAD)"',
    )(grid5000.grid5000_prepare_command)
    app.command("grid5000-run")(grid5000.grid5000_run_command)
    app.command("grid5000-sync")(grid5000.grid5000_sync_command)
    app.command("grid5000-prepare-sentences")(grid5000.grid5000_prepare_sentences_command)
    app.command("grid5000-run-sentences")(grid5000.grid5000_run_sentences_command)
    app.command("grid5000-sync-sentences")(grid5000.grid5000_sync_sentences_command)


_register_commands()

__all__ = ["app", "main"]

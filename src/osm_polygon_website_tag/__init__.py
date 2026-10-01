"""osm-polygon-website-tag: analyze OpenStreetMap polygons carrying a website tag."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of the version (#87).
    __version__ = version("osm-polygon-website-tag")
except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled tree
    __version__ = "0+unknown"

__all__ = ["__version__"]

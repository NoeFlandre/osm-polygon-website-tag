"""Typed runtime configuration.

Reads from environment variables and ``.env`` (via ``pydantic-settings``).
All values are documented in ``.env.example``. Import the singleton ``settings``
to read configuration from anywhere in the codebase.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from osm_polygon_website_tag.paths import data_root

# Default remote destinations, captured here so they live in one obvious place.
DEFAULT_GITHUB_REPO = "https://github.com/NoeFlandre/osm-polygon-website-tag.git"
DEFAULT_HF_DATASET = "NoeFlandre/osm-polygon-website-tag"


class Settings(BaseSettings):
    """Runtime configuration loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Local data root (see osm_polygon_website_tag.paths for resolution rules).
    osm_poly_data_dir: str = Field(default="", description="Local data root override.")

    # Remote destinations.
    github_repo: str = Field(default=DEFAULT_GITHUB_REPO, description="GitHub repo URL.")
    hf_dataset_repo: str = Field(default=DEFAULT_HF_DATASET, description="HF dataset slug.")

    def resolved_data_root(self) -> str:
        """Absolute path to the local data root, after resolution."""
        return str(data_root())


# Module-level singleton; cheap to import, recreated only if tests override it.
settings = Settings()

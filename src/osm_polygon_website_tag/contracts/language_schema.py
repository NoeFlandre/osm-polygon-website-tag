"""Language-detection fields for public polygon schema v1.4."""

from __future__ import annotations

import pyarrow as pa

LANGUAGE_SCHEMA_VERSION = "v1.4"

LANGUAGE_COLUMN_NAMES = (
    "website_language",
    "website_language_probability",
    "contact_website_language",
    "contact_website_language_probability",
)

LANGUAGE_FIELDS = (
    pa.field("website_language", pa.string(), nullable=True),
    pa.field("website_language_probability", pa.float64(), nullable=True),
    pa.field("contact_website_language", pa.string(), nullable=True),
    pa.field("contact_website_language_probability", pa.float64(), nullable=True),
)


__all__ = ["LANGUAGE_COLUMN_NAMES", "LANGUAGE_FIELDS", "LANGUAGE_SCHEMA_VERSION"]

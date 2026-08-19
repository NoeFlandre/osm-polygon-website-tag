"""Contract tests for the analysis-observation and rejection schemas."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.contracts.comparison_schema import (
    COMPARISON_OBSERVATION_SCHEMA,
    comparison_column_names,
)
from osm_polygon_website_tag.contracts.comparison_schema import column_doc as comparison_column_doc
from osm_polygon_website_tag.contracts.comparison_schema import (
    column_documentation as comparison_column_documentation,
)
from osm_polygon_website_tag.contracts.rejection_schema import (
    REJECTION_SCHEMA,
    rejection_column_names,
)
from osm_polygon_website_tag.contracts.rejection_schema import column_doc as rejection_column_doc
from osm_polygon_website_tag.contracts.rejection_schema import (
    column_documentation as rejection_column_documentation,
)


def test_comparison_schema_helpers_follow_declared_schema() -> None:
    names = comparison_column_names(COMPARISON_OBSERVATION_SCHEMA)

    assert names
    assert names == list(COMPARISON_OBSERVATION_SCHEMA.names)
    assert comparison_column_doc("website")
    assert comparison_column_documentation()["website"] == comparison_column_doc("website")


def test_rejection_schema_helpers_follow_declared_schema() -> None:
    names = rejection_column_names(REJECTION_SCHEMA)

    assert names
    assert names == list(REJECTION_SCHEMA.names)
    assert rejection_column_doc("message")
    assert rejection_column_documentation()["message"] == rejection_column_doc("message")


@pytest.mark.parametrize("column_doc", [comparison_column_doc, rejection_column_doc])
def test_analysis_schema_docs_reject_unknown_columns(column_doc) -> None:
    with pytest.raises(KeyError, match="no documentation"):
        column_doc("not_a_column")

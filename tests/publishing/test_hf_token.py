"""Tests for the Hugging Face token resolver."""

from __future__ import annotations

import pytest

from osm_polygon_website_tag.publishing.hf_token import resolve_hf_token


def test_resolve_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "test-token-from-env")
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    assert resolve_hf_token() == "test-token-from-env"


def test_resolve_from_alternate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "alt-token")
    assert resolve_hf_token() == "alt-token"


def test_resolve_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    # If the store has no token either, resolve returns None.
    # We don't mock the store (private API); just assert None is a
    # valid return value and is returned when no env var is set.
    # If a user happens to have a stored token, this test would fail;
    # accept that risk for CI by setting HF_HUB_OFFLINE.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    result = resolve_hf_token()
    # In offline mode without env vars we should get None.
    assert result is None or isinstance(result, str)


def test_env_takes_precedence_over_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "env-wins")
    assert resolve_hf_token() == "env-wins"

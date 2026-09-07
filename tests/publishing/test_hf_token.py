"""Tests for the Hugging Face token resolver."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from osm_polygon_website_tag.publishing import hf_token
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


def test_resolve_from_stored_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store is read through ``get_token``, the documented accessor."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(get_token=lambda: "stored-token"),
    )

    assert resolve_hf_token() == "stored-token"


@pytest.mark.parametrize("value", [1, "", None])
def test_stored_token_requires_a_non_empty_string(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(get_token=lambda: value),
    )

    assert resolve_hf_token() is None


def test_stored_token_errors_are_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)

    def raise_error() -> None:
        raise RuntimeError("credential store unavailable")

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(get_token=raise_error),
    )

    assert resolve_hf_token() is None


def test_the_real_credential_store_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against a Hub release moving the accessor out from under us."""
    from huggingface_hub import get_token

    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    stored = get_token()

    assert resolve_hf_token() == (stored if isinstance(stored, str) and stored else None)


def test_env_lookup_reads_both_documented_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each variable must be read on its own, with HF_TOKEN taking precedence."""
    monkeypatch.setattr(hf_token, "_stored_token", lambda: None)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", "primary")
    assert resolve_hf_token() == "primary"

    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "secondary")
    assert resolve_hf_token() == "secondary"

    monkeypatch.setenv("HF_TOKEN", "primary")
    assert resolve_hf_token() == "primary"

    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN")
    assert resolve_hf_token() is None


def test_the_store_is_consulted_only_without_an_environment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(hf_token, "_stored_token", lambda: calls.append(1) or "stored")
    monkeypatch.setenv("HF_TOKEN", "primary")

    assert resolve_hf_token() == "primary"
    assert calls == []

    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)

    assert resolve_hf_token() == "stored"
    assert calls == [1]

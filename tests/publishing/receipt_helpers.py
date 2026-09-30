"""Small fakes shared by completion-receipt contract tests."""

from __future__ import annotations

from types import ModuleType

import pytest


def recording_receipt_reads(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    payload: dict[str, object],
) -> list[tuple[str, dict[str, object]]]:
    """Replace receipt parsing and identity calculation while recording their options."""
    calls: list[tuple[str, dict[str, object]]] = []

    def read(_path: object, **kwargs: object) -> dict[str, object]:
        calls.append(("read", kwargs))
        return payload

    def identity(_payload: dict[str, object], **kwargs: object) -> str:
        calls.append(("identity", kwargs))
        return "id"

    monkeypatch.setattr(module, "_read_receipt_payload", read)
    monkeypatch.setattr(module, "_receipt_data_identity", identity)
    return calls

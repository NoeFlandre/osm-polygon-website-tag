"""Small reusable recording stubs for collaborator-boundary tests."""

from __future__ import annotations

from collections.abc import Callable

Call = tuple[str, tuple[object, ...], dict[str, object]]


def recording_stub(calls: list[Call], name: str, result: object = None) -> Callable[..., object]:
    """Return a stub that records arguments and yields a selected result."""

    def stub(*args: object, **kwargs: object) -> object:
        calls.append((name, args, kwargs))
        return result

    return stub

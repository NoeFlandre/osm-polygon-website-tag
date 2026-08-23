"""Small adapters around dynamically registered PyArrow compute kernels."""

from __future__ import annotations

from typing import Any

import pyarrow.compute as pc


def call_arrow_kernel(name: str, *args: Any) -> Any:
    """Call a named Arrow compute kernel through the shared dispatch boundary."""
    return pc.call_function(name, list(args))


__all__ = ["call_arrow_kernel"]

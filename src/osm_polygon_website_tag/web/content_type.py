"""Parse ``Content-Type`` values from HTTP headers and ``<meta http-equiv>``."""

from __future__ import annotations

import re

# One ``; name=value`` parameter. Quoted values are consumed whole, so a
# ``charset=`` inside another parameter's quotes is never read.
_PARAMETER = re.compile(r"""\s*;\s*([^=;\s]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([^;\s]*))""")


def charset_parameter(content_type: str) -> str | None:
    """Return the ``charset`` parameter of a Content-Type value, if any."""
    for name, double_quoted, single_quoted, token in _PARAMETER.findall(content_type):
        if name.lower() == "charset":
            return double_quoted + single_quoted + token or None
    return None


def media_type(content_type: str) -> str | None:
    """Return the lowercased ``type/subtype`` of a Content-Type value, if any."""
    essence = content_type.split(";", 1)[0].strip().lower()
    return essence or None


__all__ = ["charset_parameter", "media_type"]

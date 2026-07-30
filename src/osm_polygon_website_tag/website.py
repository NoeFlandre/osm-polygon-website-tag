"""Website value classification and hostname extraction.

Pure functions only. No I/O, no global state.

Contract: the caller must pass a non-empty ``value`` to
:class:`classify_website`, :func:`extract_hostname`, and :func:`is_redacted`.
Absent or whitespace-only values are filtered out by
:mod:`osm_polygon_website_tag.tags` before any object is considered for
inclusion in the public dataset. The classification functions therefore
do not treat empty input as "malformed" -- an absent website simply means
the object is not in the website-tagged dataset.

``classify_contact_website`` and ``extract_contact_hostname`` are
trivial aliases that document the *which-key* semantics: the value's
classification depends only on the value, never on which OSM key it
came from.

Classes (in priority order):
    - ``absolute_url``     ``http://`` or ``https://`` (any case)
    - ``scheme_relative``  starts with ``//``
    - ``bare_hostname``    a hostname/domain with at least one dot
    - ``other_scheme``     any other scheme (e.g. ``ftp://``, ``mailto:``)
    - ``multiple``         contains multiple values separated by ``;``,
                            ``,``, whitespace or newline
    - ``malformed``        the value is non-empty but not parseable as any
                            of the above

Hostname extraction:
    - Strips the scheme (``https://``).
    - Removes any userinfo (``user:pass@`` or ``user@``).
    - Removes path, query, fragment.
    - Lowercases the result.
    - Returns ``None`` if no usable hostname can be derived.

Redaction detection:
    - ``is_redacted`` returns ``True`` if the value contains userinfo,
      query, or fragment, or if the value is empty / whitespace-only.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlsplit


class WebsiteClass(StrEnum):
    """Discrete website-value classes."""

    ABSOLUTE_URL = "absolute_url"
    SCHEME_RELATIVE = "scheme_relative"
    BARE_HOSTNAME = "bare_hostname"
    OTHER_SCHEME = "other_scheme"
    MULTIPLE = "multiple"
    MALFORMED = "malformed"


_MULTIPLE_SEP_RE = re.compile(r"[;,\s]+")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(//)?")
_USERINFO_HOST_RE = re.compile(r"(?:[^@/]+@)?([^/?#]+)")
_SCHEME_RELATIVE_RE = re.compile(r"^//([^/?#]+)")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_USERINFO_RE = re.compile(r"[^@/]+@")


def classify_website(value: str) -> WebsiteClass:
    """Classify a non-empty ``value`` into one of the :class:`WebsiteClass` kinds.

    The caller must guarantee ``value`` is non-empty after trimming; this is
    the responsibility of :mod:`osm_polygon_website_tag.tags`. Calling this
    function with an empty value will still return
    :attr:`WebsiteClass.MALFORMED` but the result is meaningless.
    """
    stripped = value.strip()
    if not stripped:
        return WebsiteClass.MALFORMED

    # Multi-value detection must come first; a single value containing
    # ";" is still multiple.
    parts = [p for p in _MULTIPLE_SEP_RE.split(stripped) if p]
    if len(parts) > 1:
        return WebsiteClass.MULTIPLE

    if stripped.startswith("http://") or stripped.startswith("https://"):
        return WebsiteClass.ABSOLUTE_URL

    if stripped.startswith("//"):
        return WebsiteClass.SCHEME_RELATIVE

    if stripped[:5].lower() == "http:" or stripped[:6].lower() == "https:":
        return WebsiteClass.ABSOLUTE_URL

    if _SCHEME_RE.match(stripped):
        return WebsiteClass.OTHER_SCHEME

    if "." in stripped and _HOSTNAME_RE.match(stripped):
        return WebsiteClass.BARE_HOSTNAME

    return WebsiteClass.MALFORMED


def classify_contact_website(value: str) -> WebsiteClass:
    """Alias of :func:`classify_website` for the ``contact:website`` key.

    The classification depends only on the value; ``classify_contact_website``
    is provided as a named alias so callers reading the code can see which
    key is being processed.
    """
    return classify_website(value)


def extract_hostname(value: str) -> str | None:
    """Return the lowercased hostname extracted from a non-empty ``value``.

    The caller must guarantee ``value`` is non-empty after trimming.
    Returns ``None`` when no usable hostname can be derived.
    """
    stripped = value.strip()
    if not stripped:
        return None

    if len([part for part in _MULTIPLE_SEP_RE.split(stripped) if part]) > 1:
        return None
    candidate = stripped if _SCHEME_RE.match(stripped) else f"//{stripped.lstrip('/')}"
    try:
        hostname = urlsplit(candidate).hostname
    except ValueError:
        return None
    if not hostname or "." not in hostname:
        return None
    try:
        return hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


def extract_contact_hostname(value: str) -> str | None:
    """Alias of :func:`extract_hostname` for the ``contact:website`` key."""
    return extract_hostname(value)


def is_redacted(value: str) -> bool:
    """Return ``True`` if the value contains userinfo, query, or fragment.

    Empty or whitespace-only values are also reported as redacted so callers
    can surface them as "no usable information".
    """
    stripped = value.strip()
    if not stripped:
        return True
    if _USERINFO_RE.search(stripped):
        return True
    if "?" in stripped:
        return True
    return "#" in stripped


def _strip_port(host: str) -> str:
    """Strip a trailing ``:port`` from a hostname."""
    if ":" in host:
        return host.split(":", 1)[0]
    return host


__all__ = [
    "WebsiteClass",
    "classify_contact_website",
    "classify_website",
    "extract_contact_hostname",
    "extract_hostname",
    "is_redacted",
]

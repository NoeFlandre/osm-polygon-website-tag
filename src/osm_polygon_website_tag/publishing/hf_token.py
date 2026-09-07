"""Resolve a Hugging Face API token from environment / local store.

The CLI never accepts a token as a command-line flag. Tokens are
resolved by :func:`resolve_hf_token` from, in order:

1. The ``HF_TOKEN`` environment variable.
2. The ``HUGGING_FACE_HUB_TOKEN`` environment variable.
3. The local credential store (only if huggingface_hub is installed
   and a token has been persisted via ``hf auth login``).

``HfApi().token`` is deliberately not used: from huggingface_hub 1.0 it
reports only a token passed to the constructor, so reading it silently
resolved nothing and refused every publish.
"""

from __future__ import annotations

import os


def resolve_hf_token() -> str | None:
    """Return the available HF token or ``None`` if none is configured."""
    env_token = _environment_token()
    if env_token is not None:
        return env_token
    return _stored_token()


def _environment_token() -> str | None:
    """Return the first non-empty supported environment token."""
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


def _stored_token() -> str | None:
    """Read the optional huggingface_hub local credential store."""
    try:
        from huggingface_hub import get_token

        token = get_token()
    except Exception:
        return None
    return token if isinstance(token, str) and token else None


__all__ = ["resolve_hf_token"]

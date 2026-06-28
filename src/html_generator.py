"""Backward-compatible shim.

The HTML edition renderer now lives in the :mod:`src.html_edition` package,
which emits a multi-file bundle (index.html + assets/ + tei/ + facsimiles/)
and a zip rather than a single monolithic file. This module re-exports the
public entry points so existing imports keep working.
"""

from __future__ import annotations

from .html_edition import (  # noqa: F401
    build_edition_bundle,
    generate_html_edition,
    zip_bundle,
)

__all__ = ["generate_html_edition", "build_edition_bundle", "zip_bundle"]

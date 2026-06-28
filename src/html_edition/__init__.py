"""Static digital-edition renderer (multi-file bundle output).

Public surface:
    generate_html_edition  – build a bundle (and zip) from a desired index path
    build_edition_bundle   – write the bundle directory only
    zip_bundle             – archive an existing bundle directory
"""

from __future__ import annotations

from .builder import build_edition_bundle, generate_html_edition, zip_bundle

__all__ = ["generate_html_edition", "build_edition_bundle", "zip_bundle"]

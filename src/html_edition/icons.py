"""Inline SVG icon set for the viewer chrome."""

from __future__ import annotations

_ICON_PATHS = {
    "map":
        '<path d="M3 5l5-2 5 2 5-2v13l-5 2-5-2-5 2V5z" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>'
        '<path d="M8 3v14M13 5v14" fill="none" stroke="currentColor" '
        'stroke-width="1.5"/>',
    "copy":
        '<rect x="6" y="6" width="11" height="12" rx="1.5" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/>'
        '<path d="M4 14V4a1 1 0 011-1h10" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round"/>',
    "check":
        '<path d="M4 10l4 4 8-8" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "search":
        '<circle cx="9" cy="9" r="5.5" fill="none" stroke="currentColor" '
        'stroke-width="1.5"/>'
        '<path d="m13.5 13.5 4 4" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round"/>',
    "prev":
        '<path d="M13 5l-6 6 6 6" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "next":
        '<path d="M7 5l6 6-6 6" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "zoom-in":
        '<circle cx="9" cy="9" r="5.5" fill="none" stroke="currentColor" '
        'stroke-width="1.5"/>'
        '<path d="M6.5 9h5M9 6.5v5m4.5 2.5 4 4" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
    "zoom-out":
        '<circle cx="9" cy="9" r="5.5" fill="none" stroke="currentColor" '
        'stroke-width="1.5"/>'
        '<path d="M6.5 9h5m2 4.5 4 4" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
    "boxes":
        '<rect x="3" y="3" width="7" height="7" rx="1" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/>'
        '<rect x="11" y="3" width="6" height="4" rx="1" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/>'
        '<rect x="11" y="9" width="6" height="8" rx="1" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/>'
        '<rect x="3" y="12" width="6" height="5" rx="1" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/>',
    "document":
        '<rect x="4" y="3" width="12" height="14" rx="1" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/>'
        '<path d="M7 7h6M7 10h6M7 13h4" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round"/>',
    "reading":
        '<path d="M10 4v13M3 6c3 0 5 .5 7 2V17c-2-1.5-4-2-7-2V6z" '
        'fill="none" stroke="currentColor" stroke-width="1.5" '
        'stroke-linejoin="round"/>'
        '<path d="M17 6c-3 0-5 .5-7 2V17c2-1.5 4-2 7-2V6z" '
        'fill="none" stroke="currentColor" stroke-width="1.5" '
        'stroke-linejoin="round"/>',
    "menu":
        '<path d="M4 6h12M4 10h12M4 14h12" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    "close":
        '<path d="M5 5l10 10M15 5L5 15" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    "fit":
        '<path d="M4 7V4h3M16 4h-3v3M4 13v3h3M13 16h3v-3" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
        'stroke-linejoin="round"/>',
    "fullscreen":
        '<path d="M3 7V3h4M17 7V3h-4M3 13v4h4M17 13v4h-4" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        'stroke-linejoin="round"/>',
    "fullscreen-exit":
        '<path d="M7 3v4H3M13 3v4h4M7 17v-4H3M13 17v-4h4" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        'stroke-linejoin="round"/>',
    "filter":
        '<path d="M3 5h14l-5 6v5l-4 1v-6L3 5z" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" '
        'stroke-linecap="round"/>',
    "kbd":
        '<rect x="3" y="6" width="14" height="9" rx="1.5" fill="none" '
        'stroke="currentColor" stroke-width="1.4"/>'
        '<path d="M6 10h.5M9 10h.5M12 10h.5M6.5 13h7" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
    "tei":
        '<path d="M4 3h12v3M4 17h12M10 3v14" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>'
        '<path d="M14 9l3 2-3 2" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
}

def icon(name: str, size: int = 14) -> str:
    path = _ICON_PATHS.get(name, "")
    return (
        f'<svg viewBox="0 0 20 20" width="{size}" height="{size}" '
        f'class="i-{name}" aria-hidden="true">{path}</svg>'
    )

"""Inline text markup: entity highlighting and editorial apparatus (~~, <u>, [?])."""

from __future__ import annotations

import re
import html as html_lib
from typing import List, Optional

from ..models import Entity

def find_entity_spans(text: str, entities: List[Entity]):
    """Find non-overlapping spans for entity highlighting."""
    raw = []
    for ent in entities:
        if not ent.text:
            continue
        s = 0
        while True:
            i = text.find(ent.text, s)
            if i == -1:
                break
            raw.append((i, i + len(ent.text), ent))
            s = i + 1
    # Prefer longer spans when overlapping starts at the same index
    raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    result, cur = [], 0
    for s, e, ent in raw:
        if s >= cur:
            result.append((s, e, ent))
            cur = e
    return result

_ED_MARKER_RE = re.compile(r'~~(.+?)~~|<u>(.+?)</u>|(\w+)?\[\?\]', re.DOTALL)

def strip_editorial_markers(text: Optional[str]) -> str:
    """Strip this project's inline editorial marker syntax (``~~struck~~``,
    ``<u>underline</u>``, ``word[?]``) from *text*, keeping the underlying
    words. Fields like ``Entity.context`` are raw excerpts of transcribed
    text and can contain this markup verbatim; used as-is inside an HTML
    attribute (e.g. a tooltip's ``title="..."``), the marker syntax survives
    ``html.escape`` (it has no special characters) and is then picked up by
    :func:`postprocess_editorial`'s page-wide regex, which injects a real
    ``<del>``/``<span>`` tag *inside* the attribute value and corrupts the
    surrounding markup. Attribute/tooltip text must go through this first.
    """
    if not text:
        return ""
    def _repl(m: "re.Match[str]") -> str:
        return m.group(1) or m.group(2) or m.group(3) or ""
    return _ED_MARKER_RE.sub(_repl, text)

def _apply_editorial_markup(escaped: str) -> str:
    """Convert in-text editorial markers to HTML.

    Operates on an ALREADY-html-escaped string (so ``<u>`` shows up as
    ``&lt;u&gt;``). All regexes use ``re.DOTALL`` so markers that span
    multiple lines (an underline that runs across a line break, etc.)
    still resolve correctly.

    Marker conventions:
      * ``~~text~~``             → struck-through ``<del>``
      * ``<u>text</u>``          → underlined ``<span class="ed-underline">``
      * ``word[?]``              → uncertain reading marker
      * bare ``[?]``             → uncertain-mark only
    """
    # ~~struck~~ → del
    escaped = re.sub(
        r'~~(.+?)~~',
        r'<del class="ed-struck" title="Struck through in original">\1</del>',
        escaped, flags=re.DOTALL,
    )
    # <u>...</u> (escaped form) → underline span. DOTALL so the underline
    # can span multiple lines, which Humboldt's hand frequently does.
    escaped = re.sub(
        r'&lt;u&gt;(.+?)&lt;/u&gt;',
        r'<span class="ed-underline" title="Underlined in original">\1</span>',
        escaped, flags=re.DOTALL,
    )

    # [?] uncertain reading
    def _unc(m):
        word = m.group(1)
        if word:
            return (
                f'<span class="ed-uncertain" title="Uncertain reading">{word}'
                f'<span class="ed-uncertain-mark">[?]</span></span>'
            )
        return (
            '<span class="ed-uncertain-mark" '
            'title="Uncertain reading">[?]</span>'
        )
    escaped = re.sub(r'(\w+)?\[\?\]', _unc, escaped)
    return escaped

def render_plain(text: str) -> str:
    """Render plain text with editorial markup applied (no entity layer).

    Used by inline paths — converts newlines to ``<br>``. For ``<pre>``
    contexts (calculation/observation_table/instrument_list) use
    :func:`render_block` instead, which keeps raw newlines.
    """
    if not text:
        return ""
    escaped = html_lib.escape(text)
    escaped = _apply_editorial_markup(escaped)
    return escaped.replace("\n", "<br>\n")

def render_block(text: str) -> str:
    """Like :func:`render_plain`, but for ``<pre>`` contexts: newlines are
    preserved literally (the pre block already renders them as line breaks
    — no ``<br>`` substitution)."""
    if not text:
        return ""
    escaped = html_lib.escape(text)
    return _apply_editorial_markup(escaped)

_RE_UNDERLINE_CROSS = re.compile(
    r'&lt;u&gt;(.*?)&lt;/u&gt;', re.DOTALL
)

_RE_STRUCK_CROSS = re.compile(
    r'~~(.+?)~~', re.DOTALL
)

_RE_UNCERTAIN_AFTER_MARK = re.compile(
    r'(<mark class="ent"[^>]*>.*?</mark>)'
    r'<span class="ed-uncertain-mark"[^>]*>\[\?\]</span>',
    re.DOTALL,
)

def postprocess_editorial(html: str) -> str:
    """Repair editorial markup that crossed an entity boundary.

    The per-chunk rendering in :func:`_annotate_text` can split an
    ``<u>...</u>`` pair or leave a bare ``[?]`` next to an entity mark.
    This pass re-joins those constructs on the final HTML string.
    """
    html = _RE_UNDERLINE_CROSS.sub(
        r'<span class="ed-underline" title="Underlined in original">\1</span>',
        html,
    )
    # ~~struck~~ spans that were split across an entity <mark> never matched
    # in the per-chunk pass; re-join them on the full HTML string so the
    # Gemini view strikes them through just like the ground-truth view.
    html = _RE_STRUCK_CROSS.sub(
        r'<del class="ed-struck" title="Struck through in original">\1</del>',
        html,
    )
    html = _RE_UNCERTAIN_AFTER_MARK.sub(
        r'<span class="ed-uncertain" title="Uncertain reading">\1'
        r'<span class="ed-uncertain-mark">[?]</span></span>',
        html,
    )
    return html

def authority_source(uri: Optional[str]) -> str:
    """Short human label for an authority URI host (for entity tooltips)."""
    if not uri:
        return ""
    u = uri.lower()
    if "viaf.org" in u:
        return "VIAF"
    if "d-nb.info/gnd" in u or "/gnd/" in u:
        return "GND"
    if "geonames.org" in u:
        return "GeoNames"
    if "gbif.org" in u:
        return "GBIF"
    if "edition-humboldt.de" in u:
        return "eHD"
    return ""

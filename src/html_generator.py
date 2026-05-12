"""
HTML Generator — Humboldt Journal Digital Edition
==================================================

A scholarly digital-edition viewer that renders processed Humboldt journal
pages as a single, self-contained HTML file. The aesthetic is restrained
museum-catalogue quality: warm paper, iron-gall ink, rubric accents, with
Fraunces display, Newsreader body text, Geist UI, and JetBrains Mono data.

Centerpiece feature
-------------------
The transcription panel offers two synchronised views:

  • **Document view** (default) — every region with a bounding box is
    absolutely positioned inside an aspect-ratio container that mirrors the
    original page geometry. The result is a typeset "ghost" of the
    facsimile in which placement itself carries information. Region chrome
    is whisper-thin; the text *is* the design.

  • **Reading view** — a clean linear flow with marginal notes in a
    three-column layout, anchored to their original y-position.

Other features (all preserved)
------------------------------
- Facsimile panel with toggleable bbox overlay, smooth pan/zoom (mouse and
  trackpad), and click-to-sync highlighting between image and transcription.
- Two layout modes: Facsimile + Text  /  Text only.
- Per-page legend (entities & regions) with click-to-toggle visibility.
- Inline editorial markup:  ~~strikethrough~~ , <u>underline</u> ,
  word[?] and [?] for uncertain readings.
- Per-page Leaflet map for geocoded locations.
- Sliding table-of-contents drawer with entry previews.
- Search-as-you-type filter across the current page's regions.
- Copy-as-plain-text per page.
- Full keyboard navigation (← → arrows, Shift+← / Shift+→ for first/last,
  T to toggle TOC, R to toggle reading/document mode, F to toggle layout).
- Print-friendly stylesheet.

The Python API is preserved exactly:

    generate_html_edition(
        results, output_path,
        title=..., subtitle=...,
        entity_colors=..., entity_labels=...,
        region_colors=..., region_labels=...,
        image_folder=..., image_ref_prefix=...,
    )
"""

from __future__ import annotations

import base64
import html as html_lib
import io
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

from .models import Entity, PageResult, Region

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LANG_NAMES = {"de": "German", "fr": "French", "la": "Latin", "es": "Spanish"}
LANG_SHORT = {"de": "DE", "fr": "FR", "la": "LA", "es": "ES"}

EMBED_IMAGE_MAX_WIDTH = 1100
EMBED_IMAGE_QUALITY = 76
DEFAULT_PAGE_ASPECT = 0.72  # width / height for a typical journal page

# Sensible default colours/labels for region types — used as fallbacks only.
_REGION_FALLBACK_COLOR = "#6b5e4d"
_ENTITY_FALLBACK_COLOR = "#6b5e4d"


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _resize_image_for_embed(image_path: Path) -> Tuple[str, float]:
    """Open, downscale and base64-encode an image. Returns (data, aspect)."""
    with Image.open(image_path) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        aspect = w / h if h else DEFAULT_PAGE_ASPECT
        if w > EMBED_IMAGE_MAX_WIDTH:
            ratio = EMBED_IMAGE_MAX_WIDTH / w
            img = img.resize(
                (EMBED_IMAGE_MAX_WIDTH, int(h * ratio)), Image.LANCZOS
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=EMBED_IMAGE_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode(), aspect


def _measure_image_aspect(image_path: Path) -> float:
    """Return width / height for an image, or default if unreadable."""
    try:
        with Image.open(image_path) as img:
            w, h = img.size
            return w / h if h else DEFAULT_PAGE_ASPECT
    except Exception:  # pragma: no cover
        return DEFAULT_PAGE_ASPECT


# ---------------------------------------------------------------------------
# Inline text rendering
# ---------------------------------------------------------------------------

def _find_entity_spans(text: str, entities: List[Entity]):
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


def _render_plain(text: str) -> str:
    """Render plain text with editorial markup applied (no entity layer)."""
    escaped = html_lib.escape(text)
    # ~~strikethrough~~ → struck-through span
    escaped = re.sub(
        r'~~(.+?)~~',
        r'<del class="ed-struck" title="Struck through in original">\1</del>',
        escaped,
    )
    # <u>underline</u> → underline span (already escaped → &lt;u&gt;)
    escaped = re.sub(
        r'&lt;u&gt;(.+?)&lt;/u&gt;',
        r'<span class="ed-underline" title="Underlined in original">\1</span>',
        escaped,
    )

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
    return escaped.replace("\n", "<br>\n")


_RE_UNDERLINE_CROSS = re.compile(
    r'&lt;u&gt;(.*?)&lt;/u&gt;', re.DOTALL
)
_RE_UNCERTAIN_AFTER_MARK = re.compile(
    r'(<mark class="ent"[^>]*>.*?</mark>)'
    r'<span class="ed-uncertain-mark"[^>]*>\[\?\]</span>',
    re.DOTALL,
)


def _postprocess_editorial(html: str) -> str:
    """Repair editorial markup that crossed an entity boundary.

    The per-chunk rendering in :func:`_annotate_text` can split an
    ``<u>...</u>`` pair or leave a bare ``[?]`` next to an entity mark.
    This pass re-joins those constructs on the final HTML string.
    """
    html = _RE_UNDERLINE_CROSS.sub(
        r'<span class="ed-underline" title="Underlined in original">\1</span>',
        html,
    )
    html = _RE_UNCERTAIN_AFTER_MARK.sub(
        r'<span class="ed-uncertain" title="Uncertain reading">\1'
        r'<span class="ed-uncertain-mark">[?]</span></span>',
        html,
    )
    return html


def _annotate_text(
    text: str, entities: List[Entity], ec: Dict[str, str]
) -> str:
    """Render text with entity highlighting plus editorial markup."""
    if not text:
        return ""
    spans = _find_entity_spans(text, entities) if entities else []
    parts, cur = [], 0
    for s, e, ent in spans:
        if s > cur:
            parts.append(_render_plain(text[cur:s]))
        color = ec.get(ent.entity_type, _ENTITY_FALLBACK_COLOR)
        ctx = html_lib.escape(ent.context or "")
        norm = (
            f" → {html_lib.escape(ent.normalized_form)}"
            if ent.normalized_form else ""
        )
        parts.append(
            f'<mark class="ent" data-type="{html_lib.escape(ent.entity_type)}"'
            f' style="--ent:{color};" '
            f'title="{html_lib.escape(ent.entity_type)}: {ctx}{norm}">'
            f'{_render_plain(text[s:e])}</mark>'
        )
        cur = e
    if cur < len(text):
        parts.append(_render_plain(text[cur:]))
    return _postprocess_editorial("".join(parts))


# ---------------------------------------------------------------------------
# Region helpers
# ---------------------------------------------------------------------------

def _lang_badges(languages: List[str]) -> str:
    if not languages:
        return ""
    badges = "".join(
        f'<span class="lang-badge lang-{l}" title="{LANG_NAMES.get(l, l)}">'
        f'{LANG_SHORT.get(l, l)}</span>'
        for l in languages
    )
    return f'<span class="lang-row">{badges}</span>'


def _render_table_html(td: Dict) -> str:
    rows = "".join(
        "<tr>" + "".join(
            f"<{'th' if ri == 0 else 'td'}>"
            f"{html_lib.escape(str(c))}"
            f"</{'th' if ri == 0 else 'td'}>"
            for c in row
        ) + "</tr>"
        for ri, row in enumerate(td.get("cells", []))
    )
    cap = (
        f'<caption>{html_lib.escape(td.get("caption", ""))}</caption>'
        if td.get("caption") else ""
    )
    return f'<table class="data-table">{cap}<tbody>{rows}</tbody></table>'


def _region_classes(r: Region) -> str:
    """Return extra modifier classes for a region (slip, layer, margin-side).

    These are context-neutral state classes (``is-…``) so they read
    sensibly whether the region is rendered as a Reading-view ``.r``
    card or a Document-view ``.doc-slot``.
    """
    cls = ""
    if getattr(r, "is_pasted_slip", False) or r.region_type == "pasted_slip":
        cls += " is-pasted-slip"
    if getattr(r, "writing_layer", None) == "later_addition":
        cls += " is-later-addition"
    mp = getattr(r, "marginal_position", None)
    if mp == "left":
        cls += " is-margin-left"
    elif mp == "right":
        cls += " is-margin-right"
    elif mp == "opposite":
        cls += " is-margin-opposite"
    elif mp in ("mTop", "top"):
        cls += " is-margin-top"
    elif mp in ("mBottom", "bottom"):
        cls += " is-margin-bottom"
    return cls


def _render_region(
    region: Region,
    entities: List[Entity],
    ec: Dict[str, str],
    rc: Dict[str, str],
    rl: Dict[str, str],
    page_idx: int,
) -> str:
    """Render a single region as a card for the Reading view."""
    rtype = region.region_type
    ridx = region.region_index
    color = rc.get(rtype, _REGION_FALLBACK_COLOR)
    label = rl.get(rtype, rtype.replace("_", " ").title())

    tag = (
        f'<span class="r-tag" style="--tag-color:{color};">'
        f'<span class="r-tag-dot"></span>{label}</span>'
    )
    lang = _lang_badges(region.languages)
    pos = (
        f'<span class="r-pos">{html_lib.escape(region.position)}</span>'
        if region.position else ""
    )
    note = (
        f'<div class="r-note"><span class="r-note-mark" aria-hidden="true">'
        f'✎</span><span>{html_lib.escape(region.editorial_note)}</span></div>'
        if region.editorial_note else ""
    )
    meta = f'<div class="r-meta">{tag}{lang}{pos}</div>'
    extra_cls = _region_classes(region)

    open_tag = (
        f'<div class="r r--{rtype}{extra_cls}" '
        f'data-region-type="{rtype}" '
        f'data-region-idx="{ridx}" data-page-idx="{page_idx}" '
        f'style="--r-color:{color};" tabindex="0">'
    )
    close_tag = '</div>'

    if region.is_visual or rtype == "sketch":
        body = (
            f'<p class="r-sketch">'
            f'<span class="r-sketch-mark" aria-hidden="true">✦</span>'
            f'{html_lib.escape(region.content or "[sketch]")}</p>'
        )
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "entry_heading":
        annotated = _annotate_text(region.content, entities, ec)
        return (
            f'{open_tag}{meta}<h2 class="r-heading">{annotated}</h2>'
            f'{note}{close_tag}'
        )

    if rtype == "observation_table":
        cells = (
            (region.table_data or {}).get("cells")
            if region.table_data else None
        )
        if cells:
            body = _render_table_html(region.table_data)
        elif region.content:
            body = f'<pre class="r-data">{html_lib.escape(region.content)}</pre>'
        else:
            body = '<p class="r-text r-faint"><em>[Table not parsed]</em></p>'
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "calculation":
        body = f'<pre class="r-data">{html_lib.escape(region.content or "")}</pre>'
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "crossed_out":
        annotated = _annotate_text(region.content, entities, ec)
        repl = (
            f'<div class="r-replacement">'
            f'<span class="r-replacement-label">Replaced by</span>'
            f'<span>{html_lib.escape(region.crossed_out_text)}</span></div>'
            if region.crossed_out_text else ""
        )
        return (
            f'{open_tag}{meta}<p class="r-crossed">{annotated}</p>'
            f'{repl}{note}{close_tag}'
        )

    if rtype == "coordinates":
        body = (
            f'<p class="r-coords">{html_lib.escape(region.content or "")}</p>'
        )
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "instrument_list":
        cells = (
            (region.table_data or {}).get("cells")
            if region.table_data else None
        )
        if cells:
            body = _render_table_html(region.table_data)
        elif region.content:
            body = f'<pre class="r-data">{html_lib.escape(region.content)}</pre>'
        else:
            body = '<p class="r-text r-faint"><em>[List not parsed]</em></p>'
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "marginal_note":
        mp = getattr(region, "marginal_position", None)
        if mp == "opposite":
            return (
                f'{open_tag}{meta}'
                f'<p class="r-text r-opposite">'
                f'<em>[Bleedthrough from opposite folio — not transcribed]</em>'
                f'</p>{note}{close_tag}'
            )
        annotated = _annotate_text(region.content, entities, ec)
        return (
            f'{open_tag}{meta}<p class="r-text">{annotated}</p>'
            f'{note}{close_tag}'
        )

    # default: main_text, bibliographic_ref, page_number, catch_phrase, etc.
    annotated = _annotate_text(region.content, entities, ec)
    return f'{open_tag}{meta}<p class="r-text">{annotated}</p>{note}{close_tag}'


# ---------------------------------------------------------------------------
# Bounding-box helpers
# ---------------------------------------------------------------------------

def _sort_key(r: Region) -> Tuple[float, float, int]:
    """Natural reading-order sort: top-to-bottom, then left-to-right."""
    if r.bbox and len(r.bbox) == 4:
        return (float(r.bbox[0]), float(r.bbox[1]), r.region_index)
    return (float("inf"), float("inf"), r.region_index)


def _top_pct_from_bbox(r: Region) -> Optional[float]:
    """Return y_min as a 0–100 % value from a bbox in 0–1000 coordinates."""
    if r.bbox and len(r.bbox) == 4:
        return max(0.0, min(100.0, float(r.bbox[0]) / 10.0))
    return None


def _bbox_rect_pct(
    r: Region,
) -> Optional[Tuple[float, float, float, float]]:
    """Return (top%, left%, width%, height%) from a bbox in 0–1000 coords."""
    if not (r.bbox and len(r.bbox) == 4):
        return None
    y1, x1, y2, x2 = r.bbox
    return (
        max(0.0, min(100.0, y1 / 10.0)),
        max(0.0, min(100.0, x1 / 10.0)),
        max(0.4, min(100.0, (x2 - x1) / 10.0)),
        max(0.4, min(100.0, (y2 - y1) / 10.0)),
    )


# ---------------------------------------------------------------------------
# Document view — bbox-positioned typography (the centerpiece)
# ---------------------------------------------------------------------------

def _doc_inline_content(
    region: Region,
    entities: List[Entity],
    ec: Dict[str, str],
) -> str:
    """
    Return the textual content of a region as inline HTML suitable for
    placement inside a bbox slot in the Document view.

    No card chrome — just typography. Each region type has its own visual
    treatment (font, size, weight, colour) defined in CSS.
    """
    rtype = region.region_type

    if region.is_visual or rtype == "sketch":
        return (
            f'<span class="doc-sketch-desc">'
            f'<span class="doc-sketch-mark" aria-hidden="true">✦</span>'
            f'{html_lib.escape(region.content or "[sketch]")}</span>'
        )

    if rtype in ("observation_table", "instrument_list"):
        cells = (
            (region.table_data or {}).get("cells")
            if region.table_data else None
        )
        if cells:
            return _render_table_html(region.table_data)
        if region.content:
            return f'<pre class="doc-data">{html_lib.escape(region.content)}</pre>'
        return '<em class="doc-faint">[unparsed]</em>'

    if rtype == "calculation":
        return f'<pre class="doc-data">{html_lib.escape(region.content or "")}</pre>'

    if rtype == "coordinates":
        return (
            f'<span class="doc-coords">'
            f'{html_lib.escape(region.content or "")}</span>'
        )

    if rtype == "crossed_out":
        annotated = _annotate_text(region.content, entities, ec)
        repl = ""
        if region.crossed_out_text:
            repl = (
                f'<span class="doc-crossed-repl" '
                f'title="Replaced by">→ '
                f'{html_lib.escape(region.crossed_out_text)}</span>'
            )
        return f'<span class="doc-crossed">{annotated}</span>{repl}'

    if rtype == "marginal_note":
        mp = getattr(region, "marginal_position", None)
        if mp == "opposite":
            return '<em class="doc-faint">[opposite-folio bleedthrough]</em>'
        return _annotate_text(region.content, entities, ec)

    return _annotate_text(region.content, entities, ec)


def _build_doc_panel(
    regions: List[Region],
    entities: List[Entity],
    ec: Dict[str, str],
    rc: Dict[str, str],
    rl: Dict[str, str],
    page_idx: int,
    aspect: float,
) -> str:
    """
    Document view: every region with a bbox is placed inside an
    aspect-ratio container that mirrors the original page. Chrome is
    minimal — typography & placement do the talking.
    """
    positioned = [r for r in regions if r.bbox and len(r.bbox) == 4]
    unpositioned = [
        r for r in regions if not (r.bbox and len(r.bbox) == 4)
    ]
    positioned.sort(key=_sort_key)

    slots = []
    for r in positioned:
        rect = _bbox_rect_pct(r)
        if rect is None:
            continue
        top, left, width, height = rect
        color = rc.get(r.region_type, _REGION_FALLBACK_COLOR)
        inner = _doc_inline_content(r, entities, ec)
        extras = _region_classes(r)
        label = rl.get(
            r.region_type, r.region_type.replace("_", " ").title()
        )

        # The "chip" is a tiny region-type indicator that fades in on hover.
        chip = (
            f'<span class="doc-chip" style="--chip-color:{color};">'
            f'<span class="doc-chip-dot"></span>'
            f'<span class="doc-chip-label">{label}</span>'
            f'</span>'
        )

        slots.append(
            f'<div class="doc-slot doc-slot--{r.region_type}{extras}" '
            f'style="top:{top:.3f}%;left:{left:.3f}%;'
            f'width:{width:.3f}%;min-height:{height:.3f}%;'
            f'--slot-color:{color};" '
            f'data-region-idx="{r.region_index}" '
            f'data-region-type="{r.region_type}" '
            f'data-orig-top="{top:.3f}" '
            f'data-orig-h="{height:.3f}" '
            f'tabindex="0">'
            f'{chip}'
            f'<div class="doc-slot-body">{inner}</div>'
            f'</div>'
        )

    overlay_html = "".join(slots)
    aspect_css = f"{aspect:.4f} / 1"

    unp_html = ""
    if unpositioned:
        unp_inner = "".join(
            _render_region(r, entities, ec, rc, rl, page_idx)
            for r in unpositioned
        )
        unp_html = (
            '<details class="doc-unplaced">'
            '<summary>'
            '<svg viewBox="0 0 12 12" class="doc-unplaced-chev" '
            'aria-hidden="true">'
            '<path d="M4 3l4 3-4 3" fill="none" stroke="currentColor" '
            'stroke-width="1.5" stroke-linecap="round" '
            'stroke-linejoin="round"/>'
            '</svg>'
            f'<span>Without coordinates ({len(unpositioned)})</span>'
            '</summary>'
            f'<div class="doc-unplaced-body">{unp_inner}</div>'
            '</details>'
        )

    return (
        '<div class="trans-mode trans-mode--document" data-mode="document">'
        '<div class="doc-canvas-wrap">'
        f'<div class="doc-canvas" style="aspect-ratio:{aspect_css};">'
        '<div class="doc-grain" aria-hidden="true"></div>'
        '<div class="doc-rule doc-rule--top" aria-hidden="true"></div>'
        '<div class="doc-rule doc-rule--bottom" aria-hidden="true"></div>'
        f'{overlay_html}'
        '</div>'
        '</div>'
        f'{unp_html}'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Reading view — linear flow with marginal-note columns
# ---------------------------------------------------------------------------

def _build_reading_panel(
    regions: List[Region],
    entities: List[Entity],
    ec: Dict[str, str],
    rc: Dict[str, str],
    rl: Dict[str, str],
    page_idx: int,
) -> str:
    """Linear reading flow with three columns when marginal notes exist."""

    def _mp(r):
        return getattr(r, "marginal_position", None)

    def _mp_is(r, p):
        return r.region_type == "marginal_note" and _mp(r) == p

    left_notes  = [r for r in regions if _mp_is(r, "left")]
    right_notes = [r for r in regions if _mp_is(r, "right")]
    top_notes   = [r for r in regions if _mp_is(r, "mTop")]
    bot_notes   = [r for r in regions if _mp_is(r, "mBottom")]
    opp_notes   = [r for r in regions if _mp_is(r, "opposite")]

    placed = {
        id(r) for r in left_notes + right_notes + top_notes + bot_notes + opp_notes
    }
    main_regions = [r for r in regions if id(r) not in placed]

    main_regions.sort(key=_sort_key)
    for lst in (top_notes, bot_notes, left_notes, right_notes):
        lst.sort(key=_sort_key)

    def _html(rs):
        return "".join(
            _render_region(r, entities, ec, rc, rl, page_idx) for r in rs
        )

    def _html_margin_absolute(rs):
        if not rs:
            return ""
        parts = []
        for i, r in enumerate(rs):
            top = _top_pct_from_bbox(r)
            if top is None:
                top = 8.0 + (i * 16.0)
            parts.append(
                f'<div class="m-anchor" style="top:{top:.2f}%;">'
                f'{_render_region(r, entities, ec, rc, rl, page_idx)}'
                '</div>'
            )
        return "".join(parts)

    top_html = (
        '<div class="m-strip m-strip--top">'
        '<span class="m-strip-label">Top margin</span>'
        f'{_html(top_notes)}</div>'
        if top_notes else ""
    )
    bot_html = (
        '<div class="m-strip m-strip--bottom">'
        '<span class="m-strip-label">Bottom margin</span>'
        f'{_html(bot_notes)}</div>'
        if bot_notes else ""
    )
    opp_html = (
        '<div class="m-strip m-strip--opposite">'
        '<span class="m-strip-label">Opposite folio</span>'
        f'{_html(opp_notes)}</div>'
        if opp_notes else ""
    )

    if left_notes or right_notes:
        body_html = (
            '<div class="r-body r-body--three-col">'
            '<div class="m-col m-col--left">'
            f'{_html_margin_absolute(left_notes)}</div>'
            f'<div class="r-main">{_html(main_regions)}</div>'
            '<div class="m-col m-col--right">'
            f'{_html_margin_absolute(right_notes)}</div>'
            '</div>'
        )
    else:
        body_html = (
            '<div class="r-body">'
            f'<div class="r-main">{_html(main_regions)}</div>'
            '</div>'
        )

    return (
        '<div class="trans-mode trans-mode--reading" data-mode="reading">'
        f'{top_html}{body_html}{bot_html}{opp_html}'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Facsimile overlay
# ---------------------------------------------------------------------------

def _build_overlay(
    regions: List[Region], rc: Dict[str, str], rl: Dict[str, str]
) -> str:
    boxes = []
    for region in regions:
        if not region.bbox or len(region.bbox) != 4:
            continue
        y1, x1, y2, x2 = region.bbox
        top = y1 / 10.0
        left = x1 / 10.0
        width = (x2 - x1) / 10.0
        height = (y2 - y1) / 10.0
        color = rc.get(region.region_type, _REGION_FALLBACK_COLOR)
        label = rl.get(
            region.region_type,
            region.region_type.replace("_", " ").title(),
        )
        boxes.append(
            f'<div class="ov-box" style="top:{top:.2f}%;left:{left:.2f}%;'
            f'width:{width:.2f}%;height:{height:.2f}%;'
            f'--ov-color:{color};" '
            f'data-region-idx="{region.region_index}" '
            f'data-region-type="{region.region_type}" '
            f'tabindex="0">'
            f'<span class="ov-label">{label}</span>'
            f'</div>'
        )
    if not boxes:
        return ""
    return '<div class="region-overlay is-hidden">' + "".join(boxes) + '</div>'


# ---------------------------------------------------------------------------
# Plain-text export
# ---------------------------------------------------------------------------

def _plain_text_from_regions(regions: List[Region]) -> str:
    """Return a clean plain-text rendering for the Copy button."""
    out = []
    for r in sorted(regions, key=_sort_key):
        text = (r.content or "").strip()
        if not text:
            continue
        if r.region_type == "entry_heading":
            out.append(f"\n=== {text} ===\n")
        elif r.region_type == "marginal_note":
            mp = getattr(r, "marginal_position", "") or ""
            out.append(f"[margin {mp}] {text}")
        elif r.region_type in ("page_number", "catch_phrase"):
            out.append(f"[{r.region_type}] {text}")
        else:
            out.append(text)
    return "\n\n".join(out).strip()


# ---------------------------------------------------------------------------
# Inline SVG icons
# ---------------------------------------------------------------------------

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
}


def _icon(name: str, size: int = 14) -> str:
    path = _ICON_PATHS.get(name, "")
    return (
        f'<svg viewBox="0 0 20 20" width="{size}" height="{size}" '
        f'class="i-{name}" aria-hidden="true">{path}</svg>'
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_html_edition(
    results: List[PageResult],
    output_path,
    title: str = "Humboldt — The Inaccurate Edition",
    subtitle: str = "",
    entity_colors: Optional[Dict[str, str]] = None,
    entity_labels: Optional[Dict[str, str]] = None,
    region_colors: Optional[Dict[str, str]] = None,
    region_labels: Optional[Dict[str, str]] = None,
    image_folder=None,
    image_ref_prefix: Optional[str] = None,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ec = entity_colors or {}
    el = entity_labels or {}
    rc = region_colors or {}
    rl = region_labels or {}

    # ── Legend chips (entities + regions in a unified strip) ────────────
    ent_chips = "".join(
        f'<button type="button" class="chip chip--ent" data-type="{et}" '
        f'data-scope="entity" '
        f'title="Toggle visibility of {html_lib.escape(el.get(et, et))}">'
        f'<span class="chip-swatch" style="background:{co};"></span>'
        f'<span class="chip-label">{html_lib.escape(el.get(et, et))}'
        f'</span></button>'
        for et, co in ec.items()
    )
    used_rt = set(reg.region_type for r in results for reg in r.regions)
    rt_order = [
        "entry_heading", "main_text", "marginal_note", "pasted_slip",
        "calculation", "observation_table", "instrument_list",
        "coordinates", "sketch", "crossed_out", "bibliographic_ref",
        "page_number", "catch_phrase",
    ]
    sorted_rt = sorted(
        used_rt,
        key=lambda t: (rt_order.index(t) if t in rt_order else 999, t),
    )
    reg_chips = "".join(
        f'<button type="button" class="chip chip--reg" data-type="{rt}" '
        f'data-scope="region" '
        f'title="Toggle visibility of {html_lib.escape(rl.get(rt, rt))}">'
        f'<span class="chip-swatch" '
        f'style="background:{rc.get(rt, _REGION_FALLBACK_COLOR)};"></span>'
        f'<span class="chip-label">'
        f'{html_lib.escape(rl.get(rt, rt.replace("_", " ").title()))}'
        f'</span></button>'
        for rt in sorted_rt
    )

    # ── Page selector ────────────────────────────────────────────────────
    options = "".join(
        f'<option value="{i}">Fol. {html_lib.escape(r.folio_label)}'
        f'{" — Entries " + html_lib.escape(", ".join(r.entry_numbers)) if r.entry_numbers else ""}'
        f'</option>'
        for i, r in enumerate(results)
    )

    # ── Table-of-Contents drawer ─────────────────────────────────────────
    toc_items = []
    for i, r in enumerate(results):
        label = f"Fol. {r.folio_label}"
        sub = (
            f" · Entries {', '.join(r.entry_numbers)}"
            if r.entry_numbers else ""
        )
        preview = ""
        for reg in r.regions:
            if reg.region_type == "entry_heading" and reg.content:
                preview = reg.content[:72].strip()
                break
        if not preview:
            for reg in r.regions:
                if reg.region_type == "main_text" and reg.content:
                    preview = reg.content[:72].strip().replace("\n", " ")
                    break
        preview_html = (
            f'<span class="toc-preview">{html_lib.escape(preview)}</span>'
            if preview else ""
        )
        toc_items.append(
            f'<button type="button" class="toc-item" data-jump="{i}">'
            f'<span class="toc-num">{i+1:02d}</span>'
            f'<span class="toc-main">'
            f'<span class="toc-label">'
            f'{html_lib.escape(label)}{html_lib.escape(sub)}</span>'
            f'{preview_html}'
            f'</span></button>'
        )
    toc_html = "".join(toc_items)

    # ── Per-page rendering ───────────────────────────────────────────────
    page_divs = []
    for idx, result in enumerate(results):
        facs_img = ""
        page_aspect = DEFAULT_PAGE_ASPECT

        if image_ref_prefix is not None:
            pfx = (image_ref_prefix.rstrip("/") + "/") if image_ref_prefix else ""
            facs_img = (
                f'<img src="{pfx}{html_lib.escape(result.image_filename)}" '
                f'alt="Fol. {html_lib.escape(result.folio_label)}" '
                f'loading="lazy" '
                f'class="facs-img" draggable="false">'
            )
            if image_folder:
                ip = Path(image_folder) / result.image_filename
                if ip.exists():
                    page_aspect = _measure_image_aspect(ip)
        elif image_folder:
            ip = Path(image_folder) / result.image_filename
            if ip.exists():
                b64, page_aspect = _resize_image_for_embed(ip)
                facs_img = (
                    f'<img src="data:image/jpeg;base64,{b64}" '
                    f'alt="Fol. {html_lib.escape(result.folio_label)}" '
                    f'class="facs-img" draggable="false">'
                )

        overlay = _build_overlay(result.regions, rc, rl)
        facs_panel = ""
        if facs_img:
            facs_panel = (
                '<div class="facs-panel">'
                '  <div class="facs-toolbar">'
                '    <button type="button" '
                '            class="facs-tool facs-tool--overlay" '
                '            title="Toggle region overlay (B)">'
                f'      {_icon("boxes", 13)}<span>Regions</span>'
                '    </button>'
                '    <div class="facs-toolbar-spacer"></div>'
                '    <button type="button" '
                '            class="facs-tool facs-tool--zout" '
                '            title="Zoom out" aria-label="Zoom out">'
                f'      {_icon("zoom-out", 13)}</button>'
                '    <span class="facs-zoom-readout" '
                '          data-readout="zoom">100%</span>'
                '    <button type="button" '
                '            class="facs-tool facs-tool--zin" '
                '            title="Zoom in" aria-label="Zoom in">'
                f'      {_icon("zoom-in", 13)}</button>'
                '    <button type="button" '
                '            class="facs-tool facs-tool--zreset" '
                '            title="Fit to frame">'
                f'      {_icon("fit", 13)}</button>'
                '    <button type="button" '
                '            class="facs-tool facs-tool--fullscreen" '
                '            title="Toggle fullscreen" '
                '            aria-label="Toggle fullscreen">'
                f'      {_icon("fullscreen", 13)}</button>'
                '  </div>'
                '  <div class="facs-stage">'
                '    <div class="facs-frame" data-zoom="1">'
                f'      <div class="facs-canvas" '
                f'style="--page-aspect:{page_aspect:.4f};">'
                f'{facs_img}{overlay}</div>'
                '    </div>'
                '    <div class="facs-hint">'
                '      <span>Drag · wheel · double-click to fit</span>'
                '    </div>'
                '  </div>'
                '</div>'
            )

        # ── Map ──
        map_html = ""
        if result.locations:
            locs_data = {
                "locations": [
                    {"name": l.name, "lat": l.lat, "lon": l.lon,
                     "display": l.display_name}
                    for l in result.locations
                ],
                "center": [
                    sum(l.lat for l in result.locations) / len(result.locations),
                    sum(l.lon for l in result.locations) / len(result.locations),
                ],
            }
            map_html = (
                f'<button type="button" class="tool-btn tool-btn--map" '
                f'data-toggle="map-{idx}" '
                f'title="Show map of geolocated places">'
                f'{_icon("map", 13)}<span>Map</span>'
                f'<span class="tool-count">{len(result.locations)}</span>'
                f'</button>'
                f'<div class="map-wrap" id="map-{idx}" '
                f'data-locations="{html_lib.escape(json.dumps(locs_data))}">'
                f'</div>'
            )

        # ── Entity stats pills ──
        counts: Dict[str, int] = {}
        for e in result.entities:
            counts[e.entity_type] = counts.get(e.entity_type, 0) + 1
        stats_html = ""
        if counts:
            stats_html = '<div class="page-stats">' + "".join(
                f'<span class="stat-pill" '
                f'style="--pill-color:{ec.get(t, _ENTITY_FALLBACK_COLOR)};">'
                f'<span class="stat-dot"></span>'
                f'<span class="stat-label">'
                f'{html_lib.escape(el.get(t, t))}</span>'
                f'<span class="stat-num">{c}</span></span>'
                for t, c in sorted(counts.items(), key=lambda x: -x[1])
            ) + '</div>'

        # ── Page meta line ──
        entry_html = (
            f'<span class="meta-entries">Entries '
            f'{html_lib.escape(", ".join(result.entry_numbers))}</span>'
            if result.entry_numbers else ""
        )
        lang_html = (
            '<span class="meta-langs">'
            + " · ".join(
                html_lib.escape(LANG_NAMES.get(l, l))
                for l in result.page_languages
            )
            + '</span>'
            if result.page_languages else ""
        )

        # ── Tools toolbar ──
        plain_text = _plain_text_from_regions(result.regions)
        plain_text_attr = html_lib.escape(plain_text, quote=True)
        tools_html = (
            '<div class="page-tools">'
            '  <div class="trans-toggle" role="tablist" '
            '       aria-label="Transcription mode">'
            '    <button type="button" data-trans-mode="document" '
            '            class="active" role="tab" '
            '            title="Document view — mirrors page layout (R)">'
            f'      {_icon("document", 13)}<span>Document</span></button>'
            '    <button type="button" data-trans-mode="reading" role="tab" '
            '            title="Linear reading flow (R)">'
            f'      {_icon("reading", 13)}<span>Reading</span></button>'
            '  </div>'
            '  <div class="page-tools-spacer"></div>'
            f'  {map_html}'
            '  <button type="button" class="tool-btn tool-btn--copy" '
            f'          data-copy="{plain_text_attr}" '
            f'          title="Copy plain text of this page">'
            f'    {_icon("copy", 13)}<span class="tool-label">Copy</span>'
            f'  </button>'
            '  <div class="search-wrap">'
            f'    {_icon("search", 13)}'
            '    <input type="search" class="search-input" '
            '           placeholder="Search this page…" '
            '           aria-label="Search in this page">'
            '  </div>'
            '</div>'
        )

        # ── Transcription panels (document = default) ──
        doc_html = _build_doc_panel(
            result.regions, result.entities, ec, rc, rl, idx, page_aspect
        )
        reading_html = _build_reading_panel(
            result.regions, result.entities, ec, rc, rl, idx
        )

        cols_cls = "page-cols" if facs_panel else "page-cols is-text-only"

        page_divs.append(
            f'<article class="page" id="page-{idx}" data-page-idx="{idx}">'
            '  <header class="page-header">'
            '    <div class="page-header-main">'
            f'      <span class="folio">Fol. '
            f'{html_lib.escape(result.folio_label)}</span>'
            f'      {entry_html}'
            '    </div>'
            '    <div class="page-header-aside">'
            f'      <span class="meta-info">'
            f'{len(result.regions)} regions · '
            f'{len(result.entities)} entities</span>'
            f'      {lang_html}'
            '    </div>'
            '  </header>'
            f'  {stats_html}'
            f'  {tools_html}'
            f'  <div class="{cols_cls}">'
            f'    {facs_panel}'
            '    <section class="trans-panel" data-mode="document">'
            f'      {doc_html}{reading_html}'
            '    </section>'
            '  </div>'
            '</article>'
        )

    # ── Leaflet inclusion only if maps are present ──
    has_maps = any(r.locations for r in results)
    lh = (
        '<link rel="stylesheet" '
        'href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
        if has_maps else ""
    )
    lf = (
        '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" '
        'defer></script>'
        if has_maps else ""
    )

    CSS = _CSS
    JS = _JS

    final = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{html_lib.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?\
family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,300..900&\
family=Newsreader:ital,opsz,wght@0,6..72,300..800;1,6..72,300..800&\
family=Geist:wght@300..800&\
family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
{lh}<style>{CSS}</style>
</head>
<body>

<header class="masthead" role="banner">
  <div class="masthead-inner">
    <button type="button" class="masthead-menu" id="btn-toc"
            aria-label="Open table of contents (T)" title="Table of contents (T)">
      {_icon("menu", 16)}
    </button>
    <div class="masthead-brand">
      <span class="masthead-mark" aria-hidden="true">❦</span>
      <div class="masthead-titles">
        <span class="masthead-title">{html_lib.escape(title)}</span>
        <span class="masthead-subtitle">{html_lib.escape(subtitle)}</span>
      </div>
    </div>
    <div class="masthead-controls">
      <div class="view-toggle" role="group" aria-label="Layout (F)">
        <button type="button" data-view="dual" class="active"
                title="Show facsimile and text (F)">
          <span class="vt-glyph vt-glyph--dual" aria-hidden="true"></span>
          <span>Facsimile · Text</span>
        </button>
        <button type="button" data-view="text" title="Text-only (F)">
          <span class="vt-glyph vt-glyph--text" aria-hidden="true"></span>
          <span>Text only</span>
        </button>
      </div>
      <div class="page-nav">
        <button type="button" class="nav-btn" id="btn-prev"
                title="Previous page (←)" aria-label="Previous page">
          {_icon("prev", 14)}
        </button>
        <select id="page-select" aria-label="Jump to page">{options}</select>
        <button type="button" class="nav-btn" id="btn-next"
                title="Next page (→)" aria-label="Next page">
          {_icon("next", 14)}
        </button>
      </div>
      <span class="page-counter" id="page-counter">1 / {len(results)}</span>
    </div>
  </div>
  <div class="masthead-progress" aria-hidden="true">
    <div class="masthead-progress-bar" id="progress-bar"></div>
  </div>
</header>

<aside class="toc-drawer" id="toc-drawer" aria-hidden="true">
  <div class="toc-head">
    <span class="toc-title">Folios</span>
    <button type="button" class="toc-close" id="btn-toc-close"
            aria-label="Close table of contents">{_icon("close", 14)}</button>
  </div>
  <div class="toc-list">{toc_html}</div>
</aside>
<div class="toc-scrim" id="toc-scrim" aria-hidden="true"></div>

<div class="legend">
  <div class="legend-inner">
    <span class="legend-group">
      <span class="legend-heading">Entities</span>
      <span class="legend-chips">{ent_chips}</span>
    </span>
    <span class="legend-rule" aria-hidden="true"></span>
    <span class="legend-group">
      <span class="legend-heading">Regions</span>
      <span class="legend-chips">{reg_chips}</span>
    </span>
  </div>
</div>

<main id="pages-wrap" role="main">
{"".join(page_divs)}
</main>

<footer class="site-foot">
  <span class="foot-mark" aria-hidden="true">· · · ❦ · · ·</span>
  <span class="foot-text">Humboldt — The Inaccurate Edition</span>
  <span class="foot-hint">
    {_icon("kbd", 12)}
    <span>← → navigate · Shift to jump · T toc · R mode · F layout</span>
  </span>
</footer>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

{lf}<script>{JS}</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(final)
    size_mb = output_path.stat().st_size / 1e6
    logger.info("HTML edition written to %s (%.1f MB)", output_path, size_mb)
    return output_path


# ===========================================================================
# CSS  —  scholarly-edition stylesheet (placeholder filled at module bottom)
# ===========================================================================

_CSS = ""  # filled in below


# ===========================================================================
# JS  —  navigation, zoom, highlighting, search, copy, map lazy-init
# ===========================================================================

_JS = ""  # filled in below


# ===========================================================================
# Stylesheet assignment
# ===========================================================================

_CSS = r"""
/* =========================================================================
   Humboldt — The Inaccurate Edition · scholarly stylesheet
   -------------------------------------------------------------------------
   Aesthetic: warm paper, iron-gall ink, rubric accents, hairline rules.
   Typography: Fraunces (display), Newsreader (body), Geist (UI),
               JetBrains Mono (data).
   ========================================================================= */

/* ---- Reset --------------------------------------------------------------- */
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;
  padding:0;text-align:left}
input,select{font:inherit;color:inherit}
img{display:block;max-width:100%}
table{border-collapse:collapse}
:focus{outline:none}
:focus-visible{outline:2px solid var(--rubric);outline-offset:2px;
  border-radius:3px}

/* ---- Design tokens ------------------------------------------------------- */
:root{
  /* Paper & surfaces */
  --paper:        #f4ead4;
  --paper-deep:   #ece1c5;
  --paper-light:  #faf3e0;
  --paper-edge:   #e2d4b0;
  --paper-shadow: rgba(80, 56, 24, .10);
  --paper-soft:   #f9f2dd;
  --surface:      #fdf8e8;
  --surface-2:    #f6ecce;

  /* Iron-gall ink (slightly desaturated, scholarly) */
  --ink:        #1b2440;
  --ink-soft:   #2c3556;
  --ink-mid:    #4b557a;
  --ink-faint:  #7a82a0;

  /* Body text — warm dark brown-black for readability on paper */
  --text:       #2b2419;
  --text-mid:   #574c3d;
  --text-mute:  #8a7d6a;
  --text-faint: #b0a48c;

  /* Accents */
  --rubric:     #91361f;
  --rubric-deep:#6e2615;
  --gilt:       #b08a35;
  --moss:       #4f6b3e;

  /* Rules and hairlines */
  --rule:       rgba(95, 75, 40, .22);
  --rule-soft:  rgba(95, 75, 40, .10);
  --rule-strong:rgba(95, 75, 40, .36);

  /* Highlights & focus */
  --hl:         rgba(176, 137, 53, .22);
  --hl-strong:  rgba(176, 137, 53, .42);
  --sync:       rgba(145, 54, 31, .15);
  --sync-ring:  rgba(145, 54, 31, .55);

  /* Fonts */
  --f-display: 'Fraunces', 'Garamond', 'EB Garamond', Georgia, serif;
  --f-body:    'Newsreader', 'EB Garamond', Georgia, 'Times New Roman', serif;
  --f-ui:      'Geist', system-ui, -apple-system, 'Segoe UI', sans-serif;
  --f-mono:    'JetBrains Mono', ui-monospace, 'SFMono-Regular',
               Menlo, Consolas, monospace;

  /* Sizing */
  --container:    1660px;
  --gutter:       clamp(0.85rem, 2vw, 1.75rem);
  --masthead-h:   62px;
  --legend-h:     46px;

  /* Radii */
  --r-xs: 3px;
  --r-sm: 5px;
  --r-md: 8px;
  --r-lg: 14px;
  --r-xl: 22px;

  /* Motion */
  --ease:        cubic-bezier(.4, 0, .2, 1);
  --ease-out:    cubic-bezier(.16, 1, .3, 1);
  --ease-spring: cubic-bezier(.34, 1.56, .64, 1);

  /* Shadows — paper-like, never electronic */
  --sh-1: 0 1px 0 rgba(95, 75, 40, .04),
          0 1px 2px rgba(95, 75, 40, .06);
  --sh-2: 0 1px 0 rgba(95, 75, 40, .06),
          0 4px 14px rgba(80, 56, 24, .08);
  --sh-3: 0 2px 0 rgba(95, 75, 40, .05),
          0 8px 28px rgba(80, 56, 24, .12);
  --sh-deep: 0 1px 0 rgba(95, 75, 40, .12),
             0 14px 40px rgba(80, 56, 24, .18);
}

/* ---- Body ---------------------------------------------------------------- */
html{scroll-behavior:smooth}
body{
  background:
    radial-gradient(ellipse 1200px 600px at 20% 0%,
      rgba(255, 246, 220, .65), transparent 70%),
    radial-gradient(ellipse 1000px 800px at 100% 30%,
      rgba(238, 220, 174, .35), transparent 60%),
    linear-gradient(180deg, var(--paper-light) 0%, var(--paper) 60%);
  color:var(--text);
  font-family:var(--f-body);
  font-feature-settings: "kern", "liga", "onum", "tnum";
  font-optical-sizing:auto;
  font-size:16px;
  line-height:1.6;
  min-height:100vh;
  padding-top:calc(var(--masthead-h) + var(--legend-h));
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
}

/* Subtle paper grain (CSS only) */
body::before{
  content:"";
  position:fixed;inset:0;
  background-image:
    radial-gradient(circle at 25% 25%, rgba(120, 90, 50, .025) 0 .5px,
      transparent 1px),
    radial-gradient(circle at 75% 75%, rgba(120, 90, 50, .020) 0 .5px,
      transparent 1px),
    radial-gradient(circle at 50% 80%, rgba(120, 90, 50, .015) 0 .5px,
      transparent 1px);
  background-size:7px 7px, 11px 11px, 13px 13px;
  pointer-events:none;
  z-index:1;
  opacity:.85;
  mix-blend-mode:multiply;
}

/* ---- Masthead ------------------------------------------------------------ */
.masthead{
  position:fixed;top:0;left:0;right:0;
  height:var(--masthead-h);
  z-index:50;
  background:
    linear-gradient(180deg,
      rgba(250, 243, 224, .96) 0%,
      rgba(244, 234, 212, .92) 100%);
  backdrop-filter:saturate(140%) blur(14px);
  -webkit-backdrop-filter:saturate(140%) blur(14px);
  border-bottom:1px solid var(--rule);
  box-shadow:0 1px 0 rgba(255, 255, 255, .5) inset, var(--sh-1);
}
.masthead-inner{
  height:100%;
  max-width:var(--container);
  margin:0 auto;
  padding:0 var(--gutter);
  display:flex;
  align-items:center;
  gap:1rem;
}
.masthead-menu{
  display:inline-flex;align-items:center;justify-content:center;
  width:34px;height:34px;
  border-radius:var(--r-sm);
  color:var(--ink-soft);
  transition:background .18s var(--ease), color .18s var(--ease);
}
.masthead-menu:hover{background:rgba(120,90,50,.10);color:var(--ink)}
.masthead-brand{display:flex;align-items:center;gap:.7rem;min-width:0}
.masthead-mark{
  font-family:var(--f-display);
  color:var(--rubric);
  font-size:1.4rem;line-height:1;
  text-shadow:0 1px 0 rgba(255,255,255,.4);
  flex-shrink:0;
}
.masthead-titles{display:flex;flex-direction:column;line-height:1.15;min-width:0}
.masthead-title{
  font-family:var(--f-display);
  font-size:1.04rem;
  font-weight:500;
  color:var(--ink);
  letter-spacing:.005em;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
  font-feature-settings:"kern","liga","calt";
}
.masthead-subtitle{
  font-family:var(--f-body);
  font-style:italic;
  font-size:.78rem;
  color:var(--text-mute);
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
}
.masthead-controls{
  margin-left:auto;
  display:flex;align-items:center;gap:.85rem;
}

/* View toggle (Facsimile + Text / Text only) */
.view-toggle{
  display:inline-flex;
  background:var(--surface);
  border:1px solid var(--rule);
  border-radius:var(--r-sm);
  padding:2px;
  box-shadow:var(--sh-1);
}
.view-toggle button{
  display:inline-flex;align-items:center;gap:.45rem;
  padding:.36rem .72rem;
  border-radius:3px;
  font-family:var(--f-ui);
  font-size:.74rem;
  font-weight:500;
  letter-spacing:.01em;
  color:var(--text-mid);
  transition:background .18s var(--ease), color .18s var(--ease);
}
.view-toggle button:hover{color:var(--ink)}
.view-toggle button.active{
  background:var(--ink);
  color:var(--paper-light);
  box-shadow:0 1px 2px rgba(27, 36, 64, .25);
}
.vt-glyph{
  width:14px;height:10px;
  border:1.2px solid currentColor;
  border-radius:1px;
  position:relative;
  flex-shrink:0;
  opacity:.85;
}
.vt-glyph--dual::before{
  content:"";position:absolute;
  left:50%;top:1px;bottom:1px;
  border-left:1.2px solid currentColor;
}
.vt-glyph--text::after{
  content:"";position:absolute;
  inset:2px;
  background:
    linear-gradient(currentColor, currentColor) 0 0/100% 1px,
    linear-gradient(currentColor, currentColor) 0 50%/100% 1px,
    linear-gradient(currentColor, currentColor) 0 100%/60% 1px;
  background-repeat:no-repeat;
}

/* Page navigation */
.page-nav{
  display:inline-flex;align-items:center;
  gap:.3rem;
  background:var(--surface);
  border:1px solid var(--rule);
  border-radius:var(--r-sm);
  padding:2px;
  box-shadow:var(--sh-1);
}
.nav-btn{
  width:28px;height:28px;
  display:inline-flex;align-items:center;justify-content:center;
  border-radius:3px;
  color:var(--ink-soft);
  transition:background .15s var(--ease), color .15s var(--ease);
}
.nav-btn:hover{background:rgba(120,90,50,.10);color:var(--ink)}
.nav-btn:disabled{opacity:.35;cursor:not-allowed}
#page-select{
  appearance:none;-webkit-appearance:none;
  background:transparent;
  border:0;
  padding:.32rem .6rem;
  font-family:var(--f-ui);
  font-size:.78rem;
  font-weight:500;
  color:var(--ink);
  max-width:230px;
  cursor:pointer;
  border-radius:3px;
  transition:background .15s var(--ease);
}
#page-select:hover{background:rgba(120,90,50,.08)}
.page-counter{
  font-family:var(--f-mono);
  font-size:.72rem;
  color:var(--text-mute);
  letter-spacing:.02em;
  font-variant-numeric:tabular-nums;
  white-space:nowrap;
}

/* Progress bar */
.masthead-progress{
  position:absolute;
  left:0;right:0;bottom:-1px;
  height:2px;
  pointer-events:none;
}
.masthead-progress-bar{
  height:100%;
  width:0%;
  background:linear-gradient(90deg, var(--rubric), var(--gilt));
  transition:width .35s var(--ease-out);
}

/* ---- TOC drawer ---------------------------------------------------------- */
.toc-drawer{
  position:fixed;
  top:0;bottom:0;left:0;
  width:min(360px, 80vw);
  background:linear-gradient(180deg, var(--surface) 0%, var(--paper-light) 100%);
  border-right:1px solid var(--rule);
  z-index:60;
  transform:translateX(-100%);
  transition:transform .35s var(--ease-out);
  display:flex;flex-direction:column;
  box-shadow:var(--sh-deep);
}
.toc-drawer.is-open{transform:translateX(0)}
.toc-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:1.25rem 1.4rem 1rem;
  border-bottom:1px solid var(--rule);
}
.toc-title{
  font-family:var(--f-display);
  font-size:1.4rem;
  font-weight:400;
  color:var(--ink);
  letter-spacing:.005em;
}
.toc-close{
  width:30px;height:30px;
  display:inline-flex;align-items:center;justify-content:center;
  border-radius:var(--r-sm);
  color:var(--ink-soft);
  transition:background .15s var(--ease);
}
.toc-close:hover{background:rgba(120,90,50,.10)}
.toc-list{
  flex:1;
  overflow-y:auto;
  padding:.6rem .55rem 1rem;
  scrollbar-width:thin;
  scrollbar-color:var(--rule) transparent;
}
.toc-list::-webkit-scrollbar{width:6px}
.toc-list::-webkit-scrollbar-thumb{background:var(--rule);border-radius:3px}
.toc-item{
  width:100%;
  display:flex;align-items:flex-start;gap:.85rem;
  padding:.65rem .85rem;
  border-radius:var(--r-sm);
  transition:background .14s var(--ease);
}
.toc-item:hover{background:rgba(120,90,50,.08)}
.toc-item.is-active{
  background:rgba(176, 137, 53, .14);
  box-shadow:inset 2px 0 0 var(--rubric);
}
.toc-num{
  font-family:var(--f-mono);
  font-size:.7rem;
  color:var(--text-mute);
  letter-spacing:.04em;
  padding-top:.18rem;
  flex-shrink:0;
  font-variant-numeric:tabular-nums;
}
.toc-main{display:flex;flex-direction:column;gap:.12rem;min-width:0;flex:1}
.toc-label{
  font-family:var(--f-display);
  font-size:.96rem;
  color:var(--ink);
  font-weight:450;
  line-height:1.25;
}
.toc-preview{
  font-family:var(--f-body);
  font-style:italic;
  font-size:.78rem;
  color:var(--text-mute);
  line-height:1.4;
  overflow:hidden;
  display:-webkit-box;
  -webkit-line-clamp:2;
  -webkit-box-orient:vertical;
}
.toc-scrim{
  position:fixed;inset:0;z-index:55;
  background:rgba(40, 28, 8, .35);
  backdrop-filter:blur(2px);
  opacity:0;pointer-events:none;
  transition:opacity .25s var(--ease);
}
.toc-scrim.is-visible{opacity:1;pointer-events:auto}

/* ---- Legend bar ---------------------------------------------------------- */
.legend{
  position:fixed;
  top:var(--masthead-h);
  left:0;right:0;
  z-index:40;
  height:var(--legend-h);
  background:
    linear-gradient(180deg,
      rgba(250, 243, 224, .94) 0%,
      rgba(246, 236, 206, .90) 100%);
  backdrop-filter:saturate(140%) blur(10px);
  -webkit-backdrop-filter:saturate(140%) blur(10px);
  border-bottom:1px solid var(--rule);
  overflow-x:auto;
  scrollbar-width:none;
}
.legend::-webkit-scrollbar{display:none}
.legend-inner{
  max-width:var(--container);
  margin:0 auto;
  padding:0 var(--gutter);
  height:100%;
  display:flex;align-items:center;
  gap:1.2rem;
  white-space:nowrap;
}
.legend-group{display:inline-flex;align-items:center;gap:.7rem}
.legend-heading{
  font-family:var(--f-ui);
  font-size:.66rem;
  font-weight:500;
  letter-spacing:.12em;
  text-transform:uppercase;
  color:var(--text-mute);
}
.legend-chips{display:inline-flex;gap:.32rem;flex-wrap:nowrap}
.legend-rule{
  width:1px;height:22px;
  background:var(--rule);
}
.chip{
  display:inline-flex;align-items:center;gap:.4rem;
  padding:.26rem .55rem;
  border-radius:var(--r-xl);
  background:rgba(253, 248, 232, .65);
  border:1px solid var(--rule);
  font-family:var(--f-ui);
  font-size:.72rem;
  color:var(--text-mid);
  transition:all .18s var(--ease);
  white-space:nowrap;
  position:relative;
}
.chip:hover{
  background:var(--paper-light);
  border-color:var(--rule-strong);
  transform:translateY(-1px);
}
.chip.is-off{
  opacity:.42;
  background:transparent;
}
.chip.is-off .chip-swatch{
  background:transparent !important;
  border:1px dashed var(--text-faint);
}
.chip-swatch{
  width:9px;height:9px;
  border-radius:50%;
  flex-shrink:0;
  box-shadow:inset 0 0 0 1px rgba(255, 255, 255, .25),
             0 1px 2px rgba(0, 0, 0, .15);
}
.chip-label{font-weight:500}

/* ---- Page container ------------------------------------------------------ */
#pages-wrap{
  max-width:var(--container);
  margin:0 auto;
  padding:1.4rem var(--gutter) 3rem;
  position:relative;
  z-index:2;
}
.page{
  display:none;
  animation:pageFade .35s var(--ease-out);
}
.page.is-active{display:block}
@keyframes pageFade{
  from{opacity:0;transform:translateY(6px)}
  to{opacity:1;transform:translateY(0)}
}

/* Page header */
.page-header{
  display:flex;
  align-items:baseline;
  justify-content:space-between;
  gap:1rem;
  flex-wrap:wrap;
  padding-bottom:.75rem;
  margin-bottom:.65rem;
  border-bottom:1px solid var(--rule);
  position:relative;
}
.page-header::after{
  content:"";
  position:absolute;
  left:0;right:0;bottom:-4px;
  height:1px;
  background:var(--rule-soft);
}
.page-header-main{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap}
.folio{
  font-family:var(--f-display);
  font-size:1.9rem;
  font-weight:400;
  color:var(--ink);
  letter-spacing:.005em;
  line-height:1;
  font-feature-settings:"kern","liga","calt","onum";
}
.meta-entries{
  font-family:var(--f-display);
  font-style:italic;
  font-size:1rem;
  color:var(--rubric);
  letter-spacing:.005em;
}
.page-header-aside{
  display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;
}
.meta-info{
  font-family:var(--f-ui);
  font-size:.68rem;
  color:var(--text-mute);
  letter-spacing:.07em;
  text-transform:uppercase;
}
.meta-langs{
  font-family:var(--f-body);
  font-style:italic;
  font-size:.84rem;
  color:var(--text-mid);
}

/* Stats pills */
.page-stats{
  display:flex;flex-wrap:wrap;gap:.32rem;
  margin-bottom:.75rem;
}
.stat-pill{
  display:inline-flex;align-items:center;gap:.42rem;
  padding:.22rem .58rem;
  border-radius:var(--r-xl);
  background:rgba(253, 248, 232, .7);
  border:1px solid var(--rule-soft);
  font-family:var(--f-ui);
  font-size:.72rem;
  color:var(--text-mid);
  transition:all .18s var(--ease);
}
.stat-pill:hover{
  background:var(--paper-light);
  border-color:var(--rule);
}
.stat-dot{
  width:7px;height:7px;
  border-radius:50%;
  background:var(--pill-color, var(--ink-soft));
  flex-shrink:0;
  box-shadow:inset 0 0 0 1px rgba(255, 255, 255, .25);
}
.stat-label{font-weight:500}
.stat-num{
  font-family:var(--f-mono);
  font-size:.66rem;
  color:var(--text-mute);
  margin-left:.15rem;
  padding:1px 5px;
  background:rgba(120, 90, 50, .08);
  border-radius:9px;
  font-variant-numeric:tabular-nums;
}

/* Tools toolbar */
.page-tools{
  display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;
  padding:.45rem .5rem;
  margin-bottom:.85rem;
  background:rgba(253, 248, 232, .55);
  border:1px solid var(--rule-soft);
  border-radius:var(--r-md);
  box-shadow:var(--sh-1);
}
.page-tools-spacer{flex:1;min-width:.5rem}

.trans-toggle{
  display:inline-flex;
  background:var(--paper-light);
  border:1px solid var(--rule);
  border-radius:var(--r-sm);
  padding:2px;
  box-shadow:inset 0 1px 0 rgba(255, 255, 255, .4);
}
.trans-toggle button{
  display:inline-flex;align-items:center;gap:.4rem;
  padding:.32rem .65rem;
  border-radius:3px;
  font-family:var(--f-ui);
  font-size:.74rem;
  font-weight:500;
  color:var(--text-mid);
  transition:background .18s var(--ease), color .18s var(--ease);
}
.trans-toggle button:hover{color:var(--ink)}
.trans-toggle button.active{
  background:var(--ink);
  color:var(--paper-light);
  box-shadow:0 1px 2px rgba(27, 36, 64, .2);
}

.tool-btn{
  display:inline-flex;align-items:center;gap:.42rem;
  padding:.36rem .68rem;
  background:var(--paper-light);
  border:1px solid var(--rule);
  border-radius:var(--r-sm);
  font-family:var(--f-ui);
  font-size:.74rem;
  font-weight:500;
  color:var(--text-mid);
  transition:all .15s var(--ease);
  box-shadow:var(--sh-1);
}
.tool-btn:hover{
  color:var(--ink);
  border-color:var(--rule-strong);
  transform:translateY(-1px);
  box-shadow:var(--sh-2);
}
.tool-btn:active{transform:translateY(0)}
.tool-btn.is-on{
  background:var(--ink);
  color:var(--paper-light);
  border-color:var(--ink);
}
.tool-count{
  font-family:var(--f-mono);
  font-size:.62rem;
  padding:1px 5px;
  background:rgba(120, 90, 50, .12);
  border-radius:9px;
  margin-left:.05rem;
  font-variant-numeric:tabular-nums;
}
.tool-btn.is-on .tool-count{background:rgba(255, 255, 255, .18)}

.search-wrap{
  display:inline-flex;align-items:center;gap:.4rem;
  padding:.18rem .55rem;
  background:var(--paper-light);
  border:1px solid var(--rule);
  border-radius:var(--r-sm);
  min-width:180px;
  color:var(--text-mute);
  transition:border-color .15s var(--ease), box-shadow .15s var(--ease);
  box-shadow:var(--sh-1);
}
.search-wrap:focus-within{
  border-color:var(--ink-mid);
  box-shadow:0 0 0 3px rgba(76, 85, 122, .12);
}
.search-input{
  background:transparent;border:0;
  padding:.22rem 0;
  font-family:var(--f-ui);
  font-size:.78rem;
  color:var(--ink);
  width:100%;
  min-width:0;
}
.search-input::placeholder{color:var(--text-mute)}

/* ---- Two-column layout --------------------------------------------------- */
.page-cols{
  display:grid;
  grid-template-columns:minmax(0, 1fr) minmax(0, 1.05fr);
  gap:1.4rem;
  align-items:flex-start;
}
.page-cols.is-text-only{grid-template-columns:minmax(0, 1fr)}
body.view-text .page-cols{grid-template-columns:minmax(0, 1fr)}
body.view-text .facs-panel{display:none}

/* ---- Facsimile panel ----------------------------------------------------- */
.facs-panel{
  position:sticky;
  top:calc(var(--masthead-h) + var(--legend-h) + .7rem);
  display:flex;flex-direction:column;
  background:var(--surface-2);
  border:1px solid var(--rule);
  border-radius:var(--r-md);
  box-shadow:var(--sh-2);
  /* Use *height* (not max-height) so the sticky panel actually fills the
     viewport and the image gets the room it needs. */
  height:calc(100vh - var(--masthead-h) - var(--legend-h) - 1.5rem);
  min-height:480px;
  overflow:hidden;
}
.facs-toolbar{
  display:flex;align-items:center;gap:.4rem;
  padding:.5rem .6rem;
  border-bottom:1px solid var(--rule);
  background:linear-gradient(180deg,
    rgba(255, 248, 222, .8),
    rgba(246, 236, 206, .8));
  flex-shrink:0;
}
.facs-toolbar-spacer{flex:1}
.facs-tool{
  display:inline-flex;align-items:center;gap:.36rem;
  padding:.3rem .58rem;
  border-radius:var(--r-sm);
  background:var(--paper-light);
  border:1px solid var(--rule);
  font-family:var(--f-ui);
  font-size:.72rem;
  font-weight:500;
  color:var(--text-mid);
  transition:all .15s var(--ease);
}
.facs-tool:hover{
  color:var(--ink);
  border-color:var(--rule-strong);
  background:var(--paper-soft);
}
.facs-tool.is-on{
  background:var(--ink);
  color:var(--paper-light);
  border-color:var(--ink);
}
.facs-tool--zin, .facs-tool--zout, .facs-tool--zreset,
.facs-tool--fullscreen{
  width:30px;height:30px;
  padding:0;
  justify-content:center;
}
.facs-zoom-readout{
  font-family:var(--f-mono);
  font-size:.66rem;
  color:var(--text-mute);
  letter-spacing:.02em;
  min-width:34px;
  text-align:center;
  font-variant-numeric:tabular-nums;
}
.facs-stage{
  position:relative;
  flex:1;
  min-height:0;            /* lets the flex item shrink to share height */
  overflow:hidden;
  background:
    radial-gradient(ellipse at center, #c9b993 0%, #a89373 100%);
  cursor:grab;
}
.facs-stage.is-grabbing{cursor:grabbing}
.facs-frame{
  position:absolute;
  inset:0;
  display:flex;align-items:center;justify-content:center;
  padding:.4rem;
  transform-origin:center center;
  transition:transform .25s var(--ease-out);
  will-change:transform;
}
.facs-frame.is-moving{transition:none}
/* The canvas uses aspect-ratio so it always shows the *full* image,
   shrinking to fit whichever stage dimension is the binding constraint. */
.facs-canvas{
  position:relative;
  aspect-ratio: var(--page-aspect, 0.72) / 1;
  max-width:100%;
  max-height:100%;
  width:auto;
  height:auto;
  box-shadow:
    0 4px 14px rgba(0, 0, 0, .25),
    0 14px 50px rgba(0, 0, 0, .3);
  border-radius:2px;
  overflow:hidden;
  background:var(--paper-light);
}
.facs-img{
  display:block;
  width:100%;
  height:100%;
  object-fit:contain;
  user-select:none;
  -webkit-user-drag:none;
}
.facs-hint{
  position:absolute;
  left:50%;bottom:.6rem;
  transform:translateX(-50%);
  padding:.22rem .65rem;
  background:rgba(20, 14, 4, .55);
  color:rgba(255, 246, 220, .82);
  font-family:var(--f-ui);
  font-size:.66rem;
  letter-spacing:.04em;
  border-radius:var(--r-xl);
  pointer-events:none;
  backdrop-filter:blur(6px);
  -webkit-backdrop-filter:blur(6px);
  transition:opacity .3s var(--ease);
  opacity:.85;
}
.facs-stage:hover .facs-hint{opacity:.55}

/* Fullscreen presentation — the panel grows to fill the viewport. */
.facs-panel:fullscreen{
  height:100vh;
  max-height:100vh;
  width:100vw;
  border-radius:0;
  border:0;
  background:#1a1410;
}
.facs-panel:fullscreen .facs-stage{
  background:radial-gradient(ellipse at center, #2a221a 0%, #0e0a06 100%);
}
.facs-panel:fullscreen .facs-toolbar{
  background:rgba(30, 22, 14, .85);
  border-bottom-color:rgba(255, 246, 220, .12);
  color:var(--paper-light);
}
.facs-panel:fullscreen .facs-tool{
  background:rgba(255, 246, 220, .08);
  border-color:rgba(255, 246, 220, .18);
  color:rgba(255, 246, 220, .85);
}
.facs-panel:fullscreen .facs-tool:hover{
  background:rgba(255, 246, 220, .14);
  color:var(--paper-light);
}
.facs-panel:fullscreen .facs-tool.is-on{
  background:var(--paper-light);
  color:var(--ink);
  border-color:var(--paper-light);
}
.facs-panel:fullscreen .facs-zoom-readout{
  color:rgba(255, 246, 220, .65);
}

/* Region overlay on facsimile */
.region-overlay{
  position:absolute;inset:0;
  pointer-events:none;
  transition:opacity .3s var(--ease);
}
.region-overlay.is-hidden{
  opacity:0;visibility:hidden;
}
.ov-box{
  position:absolute;
  border:1.5px solid var(--ov-color);
  border-radius:2px;
  background:color-mix(in srgb, var(--ov-color) 8%, transparent);
  pointer-events:auto;
  cursor:pointer;
  transition:background .2s var(--ease), border-width .15s var(--ease),
             box-shadow .2s var(--ease);
  box-shadow:0 0 0 0 transparent;
}
.ov-box:hover, .ov-box:focus-visible{
  background:color-mix(in srgb, var(--ov-color) 22%, transparent);
  border-width:2px;
  box-shadow:0 0 0 3px color-mix(in srgb, var(--ov-color) 28%, transparent);
  z-index:2;
}
.ov-box.is-sync{
  background:color-mix(in srgb, var(--ov-color) 28%, transparent);
  border-width:2.5px;
  box-shadow:0 0 0 4px color-mix(in srgb, var(--ov-color) 35%, transparent),
             0 6px 18px rgba(0, 0, 0, .2);
  animation:syncPulse 1.2s var(--ease-out);
}
@keyframes syncPulse{
  0%{box-shadow:0 0 0 0 color-mix(in srgb, var(--ov-color) 60%, transparent)}
  100%{box-shadow:0 0 0 16px transparent}
}
.ov-label{
  position:absolute;
  top:-22px;
  left:-1.5px;
  padding:1px 7px;
  background:var(--ov-color);
  color:#fff;
  font-family:var(--f-ui);
  font-size:.62rem;
  font-weight:500;
  letter-spacing:.04em;
  border-radius:2px 2px 0 0;
  white-space:nowrap;
  opacity:0;
  transform:translateY(2px);
  transition:opacity .15s var(--ease), transform .15s var(--ease);
  pointer-events:none;
}
.ov-box:hover .ov-label,
.ov-box:focus-visible .ov-label,
.ov-box.is-sync .ov-label{
  opacity:1;transform:translateY(0);
}
.region-overlay.hide-type-page_number .ov-box[data-region-type="page_number"],
.region-overlay.hide-type-main_text .ov-box[data-region-type="main_text"],
.region-overlay.hide-type-marginal_note .ov-box[data-region-type="marginal_note"],
.region-overlay.hide-type-calculation .ov-box[data-region-type="calculation"],
.region-overlay.hide-type-observation_table .ov-box[data-region-type="observation_table"],
.region-overlay.hide-type-sketch .ov-box[data-region-type="sketch"],
.region-overlay.hide-type-crossed_out .ov-box[data-region-type="crossed_out"],
.region-overlay.hide-type-bibliographic_ref .ov-box[data-region-type="bibliographic_ref"],
.region-overlay.hide-type-coordinates .ov-box[data-region-type="coordinates"],
.region-overlay.hide-type-instrument_list .ov-box[data-region-type="instrument_list"],
.region-overlay.hide-type-entry_heading .ov-box[data-region-type="entry_heading"],
.region-overlay.hide-type-catch_phrase .ov-box[data-region-type="catch_phrase"],
.region-overlay.hide-type-pasted_slip .ov-box[data-region-type="pasted_slip"]{
  display:none;
}

/* ---- Transcription panel ------------------------------------------------- */
.trans-panel{
  background:var(--surface);
  border:1px solid var(--rule);
  border-radius:var(--r-md);
  box-shadow:var(--sh-2);
  position:relative;
  overflow:hidden;
}
.trans-mode{display:none}
.trans-panel[data-mode="document"] .trans-mode--document{display:block}
.trans-panel[data-mode="reading"] .trans-mode--reading{display:block}

/* ===== DOCUMENT VIEW (centerpiece) ======================================= */
.doc-canvas-wrap{
  padding:1.4rem 1.4rem 1rem;
  background:linear-gradient(180deg,
    rgba(255, 248, 222, .25),
    transparent 30%);
}
.doc-canvas{
  position:relative;
  width:100%;
  background:
    radial-gradient(ellipse at center, var(--paper-light) 0%, var(--paper) 100%);
  border:1px solid var(--rule);
  border-radius:var(--r-sm);
  box-shadow:
    inset 0 0 0 1px rgba(255, 255, 255, .35),
    inset 0 -20px 50px rgba(140, 100, 50, .04),
    var(--sh-2);
  /* visible (not hidden) — when JS reflow pushes overlapping slots down,
     the canvas grows via min-height to swallow them rather than clipping. */
  overflow:visible;
}

/* Subtle grain on the document canvas */
.doc-grain{
  position:absolute;inset:0;
  background-image:
    radial-gradient(circle at 20% 30%, rgba(120, 90, 50, .02) 0 .5px, transparent 1px),
    radial-gradient(circle at 80% 70%, rgba(120, 90, 50, .02) 0 .5px, transparent 1px),
    radial-gradient(circle at 50% 50%, rgba(120, 90, 50, .015) 0 .5px, transparent 1px);
  background-size:9px 9px, 11px 11px, 13px 13px;
  pointer-events:none;
  opacity:.6;
}

/* Top/bottom margin rules — visual cue for page edge */
.doc-rule{
  position:absolute;
  left:4%;right:4%;
  height:1px;
  background:linear-gradient(90deg,
    transparent, var(--rule), transparent);
  pointer-events:none;
}
.doc-rule--top{top:4%}
.doc-rule--bottom{bottom:4%}

/* Region slots — minimal chrome, typography is the design.
   The slot is positioned by bbox and uses *min-height* (set inline) so it
   can grow to fit content that's taller than the original ink extent —
   important for tables and dense main_text passages. Overflow is visible;
   overlaps are then prevented by a JS reflow pass at render time. */
.doc-slot{
  position:absolute;
  padding:.18rem .28rem;
  overflow:visible;
  cursor:pointer;
  transition:background .2s var(--ease),
             box-shadow .2s var(--ease),
             outline .15s var(--ease);
  border-radius:1px;
  outline:1px solid transparent;
  outline-offset:-1px;
  /* Faint paper-tinted background so overlapping regions are still legible
     in the rare case the JS reflow can't fully resolve them. */
  background:rgba(250, 243, 224, .35);
}
.doc-slot:hover, .doc-slot:focus-visible{
  background:var(--paper-light);
  outline:1px solid color-mix(in srgb, var(--slot-color) 35%, transparent);
  box-shadow:0 2px 10px rgba(80, 56, 24, .12);
  z-index:40;
}
.doc-slot.is-sync{
  background:color-mix(in srgb, var(--slot-color) 8%, var(--paper-light));
  outline:1.5px solid color-mix(in srgb, var(--slot-color) 60%, transparent);
  box-shadow:0 0 0 3px color-mix(in srgb, var(--slot-color) 18%, transparent),
             var(--sh-2);
  z-index:50;
}

/* The tiny region-type indicator chip */
.doc-chip{
  position:absolute;
  top:-1px;left:-1px;
  display:inline-flex;align-items:center;gap:.28rem;
  padding:1px 6px 1px 4px;
  background:var(--paper-light);
  border:1px solid color-mix(in srgb, var(--chip-color) 55%, var(--rule));
  border-radius:0 0 var(--r-xs) 0;
  font-family:var(--f-ui);
  font-size:.6rem;
  color:var(--chip-color);
  font-weight:500;
  letter-spacing:.04em;
  opacity:0;
  pointer-events:none;
  transition:opacity .15s var(--ease);
  white-space:nowrap;
  z-index:1;
}
.doc-slot:hover .doc-chip,
.doc-slot:focus-visible .doc-chip,
.doc-slot.is-sync .doc-chip{opacity:1}
.doc-chip-dot{
  width:5px;height:5px;border-radius:50%;
  background:var(--chip-color);
}

.doc-slot-body{
  height:auto;
  min-height:100%;
  overflow:visible;
  font-family:var(--f-body);
  color:var(--text);
  line-height:1.45;
  /* Font sizes multiply by --slot-scale (default 1). A JS auto-fit pass
     sets this per slot so under-filled bboxes get a larger font, while
     overflowing slots stay at scale 1 (and are handled by the reflow). */
  font-size:calc(clamp(.62rem, 0.95cqi, .92rem) * var(--slot-scale, 1));
}
.doc-canvas{container-type:inline-size}

/* Per-region typographic treatments */
.doc-slot--entry_heading .doc-slot-body{
  font-family:var(--f-display);
  font-size:calc(clamp(.85rem, 1.4cqi, 1.25rem) * var(--slot-scale, 1));
  font-weight:500;
  color:var(--ink);
  letter-spacing:.005em;
  line-height:1.25;
}
.doc-slot--entry_heading{
  border-top:1px solid var(--rubric);
  padding-top:.32rem;
}

.doc-slot--main_text .doc-slot-body{
  font-family:var(--f-body);
  color:var(--text);
}

.doc-slot--marginal_note .doc-slot-body{
  font-family:var(--f-body);
  font-style:italic;
  font-size:calc(clamp(.55rem, 0.82cqi, .78rem) * var(--slot-scale, 1));
  color:var(--text-mid);
}
.doc-slot--marginal_note{
  background:rgba(180, 120, 200, .04);
}

.doc-slot--coordinates .doc-slot-body{
  font-family:var(--f-mono);
  font-size:calc(clamp(.56rem, 0.82cqi, .78rem) * var(--slot-scale, 1));
  color:#0d3a78;
  font-variant-numeric:tabular-nums;
}
.doc-slot--coordinates{
  background:rgba(13, 71, 161, .03);
}
.doc-coords{display:inline}

.doc-slot--calculation .doc-slot-body,
.doc-slot--observation_table .doc-slot-body,
.doc-slot--instrument_list .doc-slot-body{
  font-family:var(--f-mono);
  font-size:calc(clamp(.52rem, 0.74cqi, .68rem) * var(--slot-scale, 1));
  color:var(--text);
}
.doc-data{
  font-family:var(--f-mono);
  white-space:pre-wrap;
  margin:0;
  font-size:inherit;
  line-height:1.35;
}
.doc-slot--observation_table .data-table,
.doc-slot--instrument_list .data-table{
  width:100%;
  font-size:.95em;
  margin:0;
  box-shadow:none;
  background:transparent;
}
.doc-slot--observation_table table,
.doc-slot--instrument_list table{
  border-collapse:collapse;
}
.doc-slot--observation_table caption,
.doc-slot--instrument_list caption{
  font-size:.85em;
  padding:.12rem .25rem;
  background:transparent;
}
.doc-slot--observation_table th,
.doc-slot--observation_table td,
.doc-slot--instrument_list th,
.doc-slot--instrument_list td{
  padding:.05rem .3rem;
  border-bottom:1px solid var(--rule-soft);
  text-align:left;
  font-variant-numeric:tabular-nums;
  line-height:1.25;
}
.doc-slot--observation_table th,
.doc-slot--instrument_list th{
  font-weight:600;
  color:var(--ink);
  border-bottom-color:var(--rule);
  background:rgba(120, 90, 50, .05);
}

.doc-slot--sketch{
  border:1px dashed color-mix(in srgb, var(--slot-color) 50%, var(--rule));
  background:rgba(78, 52, 36, .02);
}
.doc-slot--sketch .doc-slot-body{
  display:flex;
  align-items:center;
  justify-content:center;
  font-family:var(--f-body);
  font-style:italic;
  color:var(--text-mid);
  text-align:center;
  font-size:calc(clamp(.55rem, 0.82cqi, .78rem) * var(--slot-scale, 1));
}
.doc-sketch-desc{display:inline-flex;align-items:center;gap:.3rem;padding:.15rem .3rem}
.doc-sketch-mark{color:var(--text-mute);font-size:.85em}

.doc-slot--crossed_out .doc-slot-body{
  font-family:var(--f-body);
  color:var(--text-mute);
}
.doc-crossed{
  text-decoration:line-through;
  text-decoration-color:rgba(183, 28, 28, .65);
  text-decoration-thickness:1.5px;
}
.doc-crossed-repl{
  display:inline;
  margin-left:.4em;
  font-style:italic;
  color:var(--rubric);
  font-size:.85em;
}

.doc-slot--page_number .doc-slot-body,
.doc-slot--catch_phrase .doc-slot-body{
  font-family:var(--f-display);
  font-style:italic;
  font-size:calc(clamp(.6rem, 0.88cqi, .82rem) * var(--slot-scale, 1));
  color:var(--text-mute);
  text-align:center;
}

.doc-slot--bibliographic_ref .doc-slot-body{
  font-family:var(--f-body);
  font-style:italic;
  color:var(--text-mid);
  border-left:1.5px solid var(--slot-color);
  padding-left:.32rem;
}

.doc-slot.is-pasted-slip{
  background:rgba(245, 127, 23, .05);
  border:1px solid color-mix(in srgb, var(--slot-color) 35%, var(--rule));
  box-shadow:1px 1px 0 0 rgba(140, 100, 50, .15),
             2px 2px 4px rgba(140, 100, 50, .08);
  transform:rotate(-.15deg);
}
.doc-slot.is-later-addition{
  background:repeating-linear-gradient(
    -45deg,
    rgba(176, 137, 53, .03) 0 6px,
    transparent 6px 12px
  ),
  rgba(176, 137, 53, .04);
}

/* Unplaced regions accordion */
.doc-unplaced{
  margin:.85rem 1.4rem 1.2rem;
  border:1px solid var(--rule);
  border-radius:var(--r-sm);
  background:rgba(253, 248, 232, .65);
}
.doc-unplaced summary{
  display:flex;align-items:center;gap:.5rem;
  padding:.55rem .8rem;
  font-family:var(--f-ui);
  font-size:.74rem;
  font-weight:500;
  color:var(--text-mid);
  cursor:pointer;
  list-style:none;
}
.doc-unplaced summary::-webkit-details-marker{display:none}
.doc-unplaced summary::marker{content:""}
.doc-unplaced summary:hover{color:var(--ink)}
.doc-unplaced-chev{
  width:10px;height:10px;
  transition:transform .2s var(--ease);
}
.doc-unplaced[open] .doc-unplaced-chev{
  transform:rotate(90deg);
}
.doc-unplaced-body{
  padding:.3rem .8rem .8rem;
  border-top:1px solid var(--rule-soft);
  display:flex;flex-direction:column;gap:.65rem;
}

.doc-faint{
  color:var(--text-faint);
  font-style:italic;
  font-size:.92em;
}

/* ===== READING VIEW ====================================================== */
.trans-mode--reading{
  padding:1.5rem 1.6rem 1.8rem;
}
.r-body{display:flex;flex-direction:column;gap:1rem}
.r-body--three-col{
  display:grid;
  grid-template-columns: minmax(80px, 1fr) minmax(0, 3fr) minmax(80px, 1fr);
  gap:1rem;
  align-items:start;
  position:relative;
}
.r-main{
  display:flex;flex-direction:column;
  gap:1rem;
  min-width:0;
}
.m-col{
  position:relative;
  min-height:200px;
}
.m-col--left{border-right:1px dashed var(--rule)}
.m-col--right{border-left:1px dashed var(--rule)}
.m-anchor{
  position:absolute;
  left:0;right:0;
  padding:0 .35rem;
}
.m-strip{
  margin:.5rem 0;
  padding:.6rem .85rem;
  background:rgba(120, 90, 200, .05);
  border-left:2px solid #7b1fa2;
  border-radius:0 var(--r-sm) var(--r-sm) 0;
}
.m-strip--bottom{border-left-color:#7b1fa2}
.m-strip--opposite{
  background:rgba(120, 90, 50, .05);
  border-left-color:var(--text-mute);
  font-style:italic;
}
.m-strip-label{
  display:block;
  font-family:var(--f-ui);
  font-size:.62rem;
  text-transform:uppercase;
  letter-spacing:.12em;
  color:var(--text-mute);
  margin-bottom:.3rem;
}

/* ===== Region cards (Reading view) ======================================= */
.r{
  background:var(--paper-light);
  border:1px solid var(--rule);
  border-left:3px solid var(--r-color, var(--text-mute));
  border-radius:var(--r-sm);
  padding:.7rem .9rem;
  transition:background .2s var(--ease),
             box-shadow .2s var(--ease),
             border-color .2s var(--ease);
  position:relative;
  cursor:default;
}
.r:hover{
  background:var(--paper-soft);
  box-shadow:var(--sh-1);
}
.r.is-sync{
  background:color-mix(in srgb, var(--r-color) 6%, var(--paper-light));
  box-shadow:0 0 0 3px color-mix(in srgb, var(--r-color) 22%, transparent),
             var(--sh-2);
}
.r-meta{
  display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;
  margin-bottom:.35rem;
}
.r-tag{
  display:inline-flex;align-items:center;gap:.32rem;
  padding:.1rem .42rem;
  font-family:var(--f-ui);
  font-size:.62rem;
  font-weight:500;
  letter-spacing:.04em;
  color:var(--tag-color);
  background:color-mix(in srgb, var(--tag-color) 7%, var(--paper-light));
  border:1px solid color-mix(in srgb, var(--tag-color) 25%, var(--rule));
  border-radius:var(--r-xs);
  text-transform:uppercase;
}
.r-tag-dot{
  width:5px;height:5px;
  border-radius:50%;
  background:var(--tag-color);
}
.r-pos{
  font-family:var(--f-body);
  font-style:italic;
  font-size:.74rem;
  color:var(--text-mute);
}
.r-text{
  margin:0;
  font-family:var(--f-body);
  color:var(--text);
  font-size:1.02rem;
  line-height:1.65;
}
.r-text.r-faint{color:var(--text-mute);font-style:italic}
.r-text.r-opposite{color:var(--text-mute);font-style:italic}
.r-heading{
  margin:.1rem 0;
  font-family:var(--f-display);
  font-size:1.4rem;
  font-weight:500;
  color:var(--ink);
  letter-spacing:.005em;
  line-height:1.25;
  position:relative;
  padding-bottom:.3rem;
}
.r-heading::after{
  content:"";
  display:block;
  margin-top:.4rem;
  width:42px;
  height:1px;
  background:var(--rubric);
}
.r-data{
  margin:.2rem 0 0;
  padding:.6rem .8rem;
  font-family:var(--f-mono);
  font-size:.86rem;
  color:var(--text);
  background:rgba(120, 90, 50, .06);
  border-radius:var(--r-sm);
  white-space:pre-wrap;
  line-height:1.5;
  overflow-x:auto;
}
.r-coords{
  margin:.2rem 0 0;
  padding:.5rem .75rem;
  font-family:var(--f-mono);
  font-size:.88rem;
  color:#0d3a78;
  background:rgba(13, 71, 161, .04);
  border-radius:var(--r-sm);
  font-variant-numeric:tabular-nums;
}
.r-sketch{
  display:flex;
  align-items:center;
  gap:.5rem;
  margin:.3rem 0 0;
  padding:.6rem .85rem;
  font-family:var(--f-body);
  font-style:italic;
  color:var(--text-mid);
  background:rgba(78, 52, 36, .03);
  border:1px dashed var(--rule);
  border-radius:var(--r-sm);
}
.r-sketch-mark{color:var(--text-mute);font-size:1.1em}
.r-crossed{
  margin:.1rem 0;
  text-decoration:line-through;
  text-decoration-color:rgba(183, 28, 28, .55);
  text-decoration-thickness:1.5px;
  color:var(--text-mute);
  font-family:var(--f-body);
}
.r-replacement{
  display:flex;align-items:center;gap:.5rem;
  margin-top:.4rem;
  padding:.3rem .6rem;
  border-radius:var(--r-xs);
  background:rgba(176, 137, 53, .08);
  font-family:var(--f-body);
  font-size:.92rem;
  font-style:italic;
  color:var(--ink);
}
.r-replacement-label{
  font-family:var(--f-ui);
  font-size:.62rem;
  font-weight:500;
  letter-spacing:.08em;
  text-transform:uppercase;
  color:var(--text-mute);
  font-style:normal;
}
.r-note{
  display:flex;align-items:flex-start;gap:.4rem;
  margin-top:.45rem;
  padding:.36rem .55rem;
  background:rgba(91, 54, 31, .04);
  border-left:2px solid var(--rubric);
  border-radius:0 var(--r-xs) var(--r-xs) 0;
  font-family:var(--f-body);
  font-style:italic;
  font-size:.84rem;
  color:var(--text-mid);
}
.r-note-mark{
  color:var(--rubric);
  font-size:.95em;
  line-height:1.4;
  flex-shrink:0;
}

/* Special region modifiers */
.r.is-pasted-slip{
  background:linear-gradient(180deg,
    rgba(255, 245, 215, .9),
    rgba(245, 235, 205, .9));
  border-color:rgba(245, 127, 23, .35);
  border-left-color:#f57f17;
  box-shadow:
    1px 1px 0 rgba(140, 100, 50, .12),
    2px 3px 8px rgba(140, 100, 50, .12);
  transform:rotate(-.18deg);
  position:relative;
}
.r.is-pasted-slip::before{
  content:"PASTED SLIP";
  position:absolute;
  top:-7px;right:8px;
  background:#f57f17;
  color:#fff;
  font-family:var(--f-ui);
  font-size:.55rem;
  font-weight:600;
  letter-spacing:.08em;
  padding:1px 6px;
  border-radius:2px;
}
.r.is-later-addition{
  background:linear-gradient(180deg,
    rgba(255, 251, 230, .9),
    rgba(245, 240, 220, .9));
  border-style:dashed;
  border-left-style:solid;
}
.r.is-later-addition::before{
  content:"LATER ADDITION";
  position:absolute;
  top:-7px;right:8px;
  background:var(--gilt);
  color:#fff;
  font-family:var(--f-ui);
  font-size:.55rem;
  font-weight:600;
  letter-spacing:.08em;
  padding:1px 6px;
  border-radius:2px;
}

/* Hide regions via legend toggle */
.r.hide-type, .doc-slot.hide-type{
  display:none !important;
}

/* Language badges */
.lang-row{display:inline-flex;gap:.22rem}
.lang-badge{
  display:inline-block;
  padding:0 .3rem;
  font-family:var(--f-ui);
  font-size:.56rem;
  font-weight:600;
  letter-spacing:.06em;
  background:rgba(120, 90, 50, .12);
  color:var(--text-mid);
  border-radius:2px;
  text-transform:uppercase;
}
.lang-de{background:rgba(27, 32, 50, .12);color:#1b2032}
.lang-fr{background:rgba(13, 71, 161, .12);color:#0d47a1}
.lang-la{background:rgba(78, 52, 36, .12);color:#4e3424}
.lang-es{background:rgba(176, 56, 32, .12);color:#b03820}

/* ===== Entity highlighting & editorial markup ============================ */
.ent{
  background:transparent;
  color:inherit;
  text-decoration:underline;
  text-decoration-color:color-mix(in srgb, var(--ent) 60%, transparent);
  text-decoration-thickness:2px;
  text-underline-offset:3px;
  border-radius:1px;
  padding:0 .04em;
  transition:background .15s var(--ease),
             text-decoration-thickness .15s var(--ease);
}
.ent:hover{
  background:color-mix(in srgb, var(--ent) 10%, transparent);
  text-decoration-thickness:3px;
}
.ent.hide-type{
  text-decoration:none;
  background:transparent;
}

/* Editorial markup */
.ed-struck{
  text-decoration:line-through;
  text-decoration-color:rgba(183, 28, 28, .55);
  text-decoration-thickness:1.5px;
  color:var(--text-mute);
}
.ed-underline{
  text-decoration:underline;
  text-decoration-thickness:1.2px;
  text-underline-offset:2px;
}
.ed-uncertain{
  background:rgba(176, 137, 53, .12);
  border-radius:2px;
  padding:0 .12em;
}
.ed-uncertain-mark{
  color:var(--rubric);
  font-size:.78em;
  font-weight:600;
  vertical-align:super;
  font-family:var(--f-ui);
  margin-left:.06em;
}

/* Search highlights */
.search-hit{
  background:rgba(176, 137, 53, .35);
  outline:1px solid rgba(176, 137, 53, .55);
  border-radius:1px;
  padding:0 .04em;
}
.r.search-no-match, .doc-slot.search-no-match{
  opacity:.18;
  transition:opacity .25s var(--ease);
}
.r.search-match, .doc-slot.search-match{
  outline:2px solid var(--gilt);
  outline-offset:1px;
}

/* ===== Tables (general) ================================================== */
.data-table{
  width:100%;
  font-family:var(--f-mono);
  font-size:.84rem;
  border-collapse:collapse;
  margin:.3rem 0;
  background:var(--paper-light);
  border-radius:var(--r-sm);
  overflow:hidden;
  box-shadow:var(--sh-1);
}
.data-table caption{
  caption-side:top;
  padding:.45rem .75rem;
  text-align:left;
  font-family:var(--f-body);
  font-style:italic;
  font-size:.88rem;
  color:var(--text-mid);
  background:rgba(120, 90, 50, .06);
}
.data-table th, .data-table td{
  padding:.32rem .65rem;
  text-align:left;
  border-bottom:1px solid var(--rule-soft);
  font-variant-numeric:tabular-nums;
}
.data-table th{
  background:rgba(120, 90, 50, .08);
  font-weight:600;
  color:var(--ink);
  border-bottom:1px solid var(--rule);
}
.data-table tr:last-child td{border-bottom:none}

/* ===== Map =============================================================== */
.map-wrap{
  display:none;
  width:100%;
  height:340px;
  margin-top:.6rem;
  background:var(--surface);
  border:1px solid var(--rule);
  border-radius:var(--r-md);
  overflow:hidden;
  box-shadow:var(--sh-1);
}
.map-wrap.is-open{display:block;flex-basis:100%}
.leaflet-container{
  font-family:var(--f-ui);
  background:#ede4d0;
}

/* ===== Toast ============================================================= */
.toast{
  position:fixed;
  bottom:1.4rem;left:50%;
  transform:translateX(-50%) translateY(120%);
  padding:.55rem 1rem;
  background:var(--ink);
  color:var(--paper-light);
  font-family:var(--f-ui);
  font-size:.78rem;
  font-weight:500;
  border-radius:var(--r-xl);
  box-shadow:var(--sh-deep);
  z-index:100;
  transition:transform .35s var(--ease-spring), opacity .25s var(--ease);
  opacity:0;
  display:inline-flex;align-items:center;gap:.45rem;
}
.toast.is-visible{
  transform:translateX(-50%) translateY(0);
  opacity:1;
}
.toast .i-check{color:#7fc97f}

/* ===== Footer ============================================================ */
.site-foot{
  max-width:var(--container);
  margin:2.5rem auto 1.2rem;
  padding:1.5rem var(--gutter) 1rem;
  border-top:1px solid var(--rule);
  display:flex;
  flex-direction:column;
  align-items:center;
  gap:.45rem;
  position:relative;
  z-index:2;
}
.foot-mark{
  font-family:var(--f-display);
  color:var(--text-mute);
  font-size:1.1rem;
  letter-spacing:.4em;
}
.foot-text{
  font-family:var(--f-display);
  font-style:italic;
  font-size:.95rem;
  color:var(--text-mid);
}
.foot-hint{
  font-family:var(--f-ui);
  font-size:.7rem;
  color:var(--text-mute);
  letter-spacing:.04em;
  display:inline-flex;align-items:center;gap:.4rem;
}
.foot-hint .i-kbd{opacity:.7}

/* ===== Responsive ======================================================== */
@media (max-width: 1100px){
  .page-cols{
    grid-template-columns:minmax(0, 1fr);
  }
  .facs-panel{
    position:static;
    height:auto;
    max-height:none;
    min-height:0;
  }
  .facs-img{max-height:80vh}
  .facs-canvas{max-height:80vh}
  .r-body--three-col{
    grid-template-columns:1fr;
  }
  .m-col{border:none !important;min-height:0}
  .m-col--left, .m-col--right{
    border-top:1px dashed var(--rule) !important;
    padding-top:.5rem;
  }
  .m-anchor{position:relative;top:auto !important}
}
@media (max-width: 720px){
  :root{
    --masthead-h: 56px;
    --legend-h: 42px;
  }
  .masthead-titles{display:none}
  .masthead-brand{flex-shrink:0}
  .view-toggle button span:last-child{display:none}
  .view-toggle button{padding:.36rem .55rem}
  .page-counter{display:none}
  #page-select{max-width:120px;font-size:.72rem}
  .folio{font-size:1.5rem}
  .page-tools{padding:.4rem}
  .trans-toggle button span{display:none}
  .tool-label{display:none}
  .search-wrap{min-width:120px;flex:1}
  .doc-canvas-wrap{padding:.85rem}
  .trans-mode--reading{padding:1rem 1rem 1.2rem}
}

/* ===== Print ============================================================= */
@media print{
  body{padding-top:0;background:#fff;color:#000}
  body::before{display:none}
  .masthead, .legend, .toc-drawer, .toc-scrim, .page-tools,
  .facs-toolbar, .facs-hint, .site-foot, .toast, .map-wrap,
  .tool-btn--map, .doc-unplaced summary{display:none !important}
  .page{display:block !important;page-break-after:always}
  .page-cols{grid-template-columns:1fr 1fr;gap:1rem}
  .facs-panel{position:static;max-height:none;box-shadow:none;
    border-color:#aaa}
  .region-overlay{display:none}
  .trans-panel{box-shadow:none;border-color:#aaa}
  .doc-canvas{box-shadow:none;border-color:#bbb}
  .doc-unplaced{border-color:#bbb}
  .doc-unplaced-body{display:block !important}
  .trans-mode--document, .trans-mode--reading{display:block !important}
}
"""


_JS = r"""
(function(){
  'use strict';

  // ============================================================
  // Element references
  // ============================================================
  var pages       = Array.from(document.querySelectorAll('.page'));
  var pageSelect  = document.getElementById('page-select');
  var pageCounter = document.getElementById('page-counter');
  var btnPrev     = document.getElementById('btn-prev');
  var btnNext     = document.getElementById('btn-next');
  var btnToc      = document.getElementById('btn-toc');
  var btnTocClose = document.getElementById('btn-toc-close');
  var tocDrawer   = document.getElementById('toc-drawer');
  var tocScrim    = document.getElementById('toc-scrim');
  var tocItems    = Array.from(document.querySelectorAll('.toc-item'));
  var progressBar = document.getElementById('progress-bar');
  var toast       = document.getElementById('toast');
  var viewBtns    = Array.from(document.querySelectorAll('.view-toggle button'));

  var curPage = 0;
  var pendingHighlightDelay = 250;

  // ============================================================
  // Page navigation
  // ============================================================
  function showPage(i){
    if(i < 0 || i >= pages.length || i === curPage) return;
    pages[curPage].classList.remove('is-active');
    curPage = i;
    pages[curPage].classList.add('is-active');
    pageSelect.value = i;
    pageCounter.textContent = (i + 1) + ' / ' + pages.length;
    tocItems.forEach(function(it, idx){
      it.classList.toggle('is-active', idx === i);
    });
    var pct = pages.length > 1
      ? ((i) / (pages.length - 1)) * 100
      : 100;
    progressBar.style.width = pct + '%';
    btnPrev.disabled = (i === 0);
    btnNext.disabled = (i === pages.length - 1);
    window.scrollTo({ top: 0, behavior: 'auto' });
  }
  pages.forEach(function(p, i){ if(i === 0) p.classList.add('is-active'); });
  showPage(0);

  pageSelect.addEventListener('change', function(){ showPage(+pageSelect.value); });
  btnPrev.addEventListener('click', function(){ showPage(curPage - 1); });
  btnNext.addEventListener('click', function(){ showPage(curPage + 1); });

  // ============================================================
  // View toggle (Facsimile + Text  /  Text only)
  // ============================================================
  viewBtns.forEach(function(btn){
    btn.addEventListener('click', function(){
      var v = btn.dataset.view;
      viewBtns.forEach(function(b){ b.classList.toggle('active', b === btn); });
      document.body.classList.toggle('view-text', v === 'text');
    });
  });

  // ============================================================
  // Transcription mode toggle (Document / Reading) — per page
  // ============================================================
  pages.forEach(function(page){
    var panel = page.querySelector('.trans-panel');
    var btns = page.querySelectorAll('.trans-toggle button');
    btns.forEach(function(btn){
      btn.addEventListener('click', function(){
        var mode = btn.dataset.transMode;
        btns.forEach(function(b){
          b.classList.toggle('active', b.dataset.transMode === mode);
        });
        panel.setAttribute('data-mode', mode);
      });
    });
  });

  function toggleTransMode(){
    var page = pages[curPage];
    var panel = page.querySelector('.trans-panel');
    var cur = panel.getAttribute('data-mode');
    var nxt = cur === 'document' ? 'reading' : 'document';
    panel.setAttribute('data-mode', nxt);
    page.querySelectorAll('.trans-toggle button').forEach(function(b){
      b.classList.toggle('active', b.dataset.transMode === nxt);
    });
  }
  function toggleLayout(){
    var dual = viewBtns[0], textOnly = viewBtns[1];
    var isText = document.body.classList.toggle('view-text');
    dual.classList.toggle('active', !isText);
    textOnly.classList.toggle('active', isText);
  }

  // ============================================================
  // TOC drawer
  // ============================================================
  function openToc(){
    tocDrawer.classList.add('is-open');
    tocScrim.classList.add('is-visible');
    tocDrawer.setAttribute('aria-hidden', 'false');
  }
  function closeToc(){
    tocDrawer.classList.remove('is-open');
    tocScrim.classList.remove('is-visible');
    tocDrawer.setAttribute('aria-hidden', 'true');
  }
  btnToc.addEventListener('click', openToc);
  btnTocClose.addEventListener('click', closeToc);
  tocScrim.addEventListener('click', closeToc);
  tocItems.forEach(function(it){
    it.addEventListener('click', function(){
      var i = +it.dataset.jump;
      showPage(i);
      closeToc();
    });
  });

  // ============================================================
  // Keyboard navigation
  // ============================================================
  document.addEventListener('keydown', function(e){
    var tag = (e.target && e.target.tagName) || '';
    if(tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') return;
    if(e.metaKey || e.ctrlKey || e.altKey) return;

    if(e.key === 'ArrowRight'){
      e.preventDefault();
      showPage(e.shiftKey ? pages.length - 1 : curPage + 1);
    } else if(e.key === 'ArrowLeft'){
      e.preventDefault();
      showPage(e.shiftKey ? 0 : curPage - 1);
    } else if(e.key === 'Escape'){
      if(tocDrawer.classList.contains('is-open')) closeToc();
    } else if(e.key === 't' || e.key === 'T'){
      if(tocDrawer.classList.contains('is-open')) closeToc();
      else openToc();
    } else if(e.key === 'r' || e.key === 'R'){
      toggleTransMode();
    } else if(e.key === 'f' || e.key === 'F'){
      toggleLayout();
    } else if(e.key === 'b' || e.key === 'B'){
      var ov = pages[curPage].querySelector('.facs-tool--overlay');
      if(ov) ov.click();
    }
  });

  // ============================================================
  // Facsimile zoom & pan (per page)
  // ============================================================
  pages.forEach(function(page){
    var stage = page.querySelector('.facs-stage');
    var frame = page.querySelector('.facs-frame');
    var readout = page.querySelector('[data-readout="zoom"]');
    var btnIn = page.querySelector('.facs-tool--zin');
    var btnOut = page.querySelector('.facs-tool--zout');
    var btnReset = page.querySelector('.facs-tool--zreset');
    var btnOverlay = page.querySelector('.facs-tool--overlay');
    var overlay = page.querySelector('.region-overlay');
    if(!stage || !frame) return;

    var zoom = 1;
    var tx = 0, ty = 0;
    var minZoom = 1, maxZoom = 8;
    var isDragging = false;
    var dragStartX = 0, dragStartY = 0;
    var dragOrigTx = 0, dragOrigTy = 0;

    function apply(animate){
      frame.style.transform =
        'translate(' + tx + 'px, ' + ty + 'px) scale(' + zoom + ')';
      if(readout) readout.textContent = Math.round(zoom * 100) + '%';
      if(animate){
        frame.classList.remove('is-moving');
      } else {
        frame.classList.add('is-moving');
      }
    }

    function clampPan(){
      if(zoom <= 1){ tx = 0; ty = 0; return; }
      // Allow generous pan; image moves but cannot leave the stage entirely.
      var rect = stage.getBoundingClientRect();
      var maxX = rect.width * (zoom - 1) / 2 + 60;
      var maxY = rect.height * (zoom - 1) / 2 + 60;
      tx = Math.max(-maxX, Math.min(maxX, tx));
      ty = Math.max(-maxY, Math.min(maxY, ty));
    }

    function setZoom(z, cx, cy, animate){
      var old = zoom;
      zoom = Math.max(minZoom, Math.min(maxZoom, z));
      if(zoom === 1){
        tx = 0; ty = 0;
      } else if(cx != null && cy != null){
        var rect = stage.getBoundingClientRect();
        var offX = cx - rect.left - rect.width / 2;
        var offY = cy - rect.top - rect.height / 2;
        tx = (tx - offX) * (zoom / old) + offX;
        ty = (ty - offY) * (zoom / old) + offY;
        clampPan();
      }
      apply(animate);
    }

    if(btnIn) btnIn.addEventListener('click', function(){
      setZoom(zoom * 1.4, null, null, true);
    });
    if(btnOut) btnOut.addEventListener('click', function(){
      setZoom(zoom / 1.4, null, null, true);
    });
    if(btnReset) btnReset.addEventListener('click', function(){
      setZoom(1, null, null, true);
    });

    // Wheel zoom (Ctrl/⌘ + wheel zooms; plain wheel also zooms over canvas)
    stage.addEventListener('wheel', function(e){
      e.preventDefault();
      var direction = e.deltaY < 0 ? 1 : -1;
      var factor = 1 + direction * Math.min(.25, Math.abs(e.deltaY) / 400);
      setZoom(zoom * factor, e.clientX, e.clientY, false);
    }, { passive:false });

    // Drag-to-pan
    stage.addEventListener('mousedown', function(e){
      if(e.button !== 0) return;
      if(e.target.closest('.ov-box')) return;
      isDragging = true;
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      dragOrigTx = tx;
      dragOrigTy = ty;
      stage.classList.add('is-grabbing');
      e.preventDefault();
    });
    window.addEventListener('mousemove', function(e){
      if(!isDragging) return;
      tx = dragOrigTx + (e.clientX - dragStartX);
      ty = dragOrigTy + (e.clientY - dragStartY);
      clampPan();
      apply(false);
    });
    window.addEventListener('mouseup', function(){
      if(isDragging){
        isDragging = false;
        stage.classList.remove('is-grabbing');
      }
    });

    // Double-click = fit to frame
    stage.addEventListener('dblclick', function(e){
      if(e.target.closest('.ov-box')) return;
      setZoom(1, null, null, true);
    });

    // Touch panning (simple)
    var tStartX = 0, tStartY = 0, tOrigTx = 0, tOrigTy = 0;
    var tStartDist = 0, tStartZoom = 1;
    stage.addEventListener('touchstart', function(e){
      if(e.touches.length === 1){
        tStartX = e.touches[0].clientX;
        tStartY = e.touches[0].clientY;
        tOrigTx = tx;
        tOrigTy = ty;
      } else if(e.touches.length === 2){
        var dx = e.touches[0].clientX - e.touches[1].clientX;
        var dy = e.touches[0].clientY - e.touches[1].clientY;
        tStartDist = Math.sqrt(dx*dx + dy*dy);
        tStartZoom = zoom;
      }
    }, { passive:true });
    stage.addEventListener('touchmove', function(e){
      if(e.touches.length === 1){
        tx = tOrigTx + (e.touches[0].clientX - tStartX);
        ty = tOrigTy + (e.touches[0].clientY - tStartY);
        clampPan();
        apply(false);
      } else if(e.touches.length === 2 && tStartDist){
        var dx = e.touches[0].clientX - e.touches[1].clientX;
        var dy = e.touches[0].clientY - e.touches[1].clientY;
        var d = Math.sqrt(dx*dx + dy*dy);
        var cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        var cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        setZoom(tStartZoom * (d / tStartDist), cx, cy, false);
      }
      e.preventDefault();
    }, { passive:false });

    apply(true);

    // ── Overlay toggle ──
    if(btnOverlay && overlay){
      btnOverlay.addEventListener('click', function(){
        var hidden = overlay.classList.toggle('is-hidden');
        btnOverlay.classList.toggle('is-on', !hidden);
      });
    }

    // ── Fullscreen toggle ──
    var btnFull = page.querySelector('.facs-tool--fullscreen');
    var panel = page.querySelector('.facs-panel');
    if(btnFull && panel){
      btnFull.addEventListener('click', function(){
        var fsEl = document.fullscreenElement
                || document.webkitFullscreenElement;
        if(fsEl){
          (document.exitFullscreen
            || document.webkitExitFullscreen).call(document);
        } else {
          var req = panel.requestFullscreen
                 || panel.webkitRequestFullscreen;
          if(req){
            req.call(panel).catch(function(){ /* user cancel */ });
          }
        }
      });
    }
  });

  // Update the fullscreen icon's active state on enter/exit
  function updateFsButtons(){
    var fsEl = document.fullscreenElement
            || document.webkitFullscreenElement;
    document.querySelectorAll('.facs-tool--fullscreen').forEach(function(btn){
      btn.classList.toggle('is-on', !!fsEl && fsEl.contains(btn));
    });
  }
  document.addEventListener('fullscreenchange', updateFsButtons);
  document.addEventListener('webkitfullscreenchange', updateFsButtons);

  // ============================================================
  // Document view auto-fit + overlap resolver
  // ------------------------------------------------------------
  // Two-step layout pass for the bbox-positioned Document view:
  //
  //   (1) autoFitSlot — for each slot, measure the natural content
  //       height; if it's well under the bbox's allotted height, scale
  //       the font up (via the CSS variable --slot-scale) so the bbox
  //       is filled comfortably (~85 %). Slots that already fill or
  //       overflow their bbox stay at scale 1 — the reflow handles
  //       those by pushing later slots down instead.
  //
  //   (2) reflowDocCanvas — after autofit has settled fonts, walk the
  //       slots in y-order and push any horizontally-overlapping later
  //       slot down by a small gap. Grow the canvas via min-height if
  //       the cascade pushes content past the natural page bottom.
  // ============================================================

  // Per-region-type ceiling on the scale factor. Display & body text
  // can grow more aggressively; data-style content (tables, formulas)
  // is kept tighter so columns and numerals stay legible.
  var SCALE_CAP_BY_TYPE = {
    entry_heading:    2.2,
    main_text:        2.0,
    marginal_note:    1.8,
    coordinates:      1.8,
    bibliographic_ref:1.7,
    crossed_out:      1.8,
    catch_phrase:     2.2,
    page_number:      2.4,
    sketch:           1.6,
    calculation:      1.5,
    observation_table:1.4,
    instrument_list:  1.4,
    pasted_slip:      1.7,
  };

  function autoFitSlot(slot, canvasH){
    var origHPct = parseFloat(slot.dataset.origH || '0');
    if(origHPct < 2 || !canvasH) return;
    var bboxHpx = (origHPct / 100) * canvasH;
    if(bboxHpx < 12) return;                        // too small to bother

    var body = slot.querySelector('.doc-slot-body');
    if(!body) return;

    var type = slot.dataset.regionType || '';
    var cap  = SCALE_CAP_BY_TYPE[type] || 1.8;

    // Reset scale so we measure the natural content height.
    slot.style.setProperty('--slot-scale', '1');
    // The slot's min-height (set inline by Python) would stretch the
    // body to the bbox even when content is shorter, hiding the slack
    // we want to detect. Temporarily lift it so offsetHeight reflects
    // *real* content size.
    var savedMinH = slot.style.minHeight;
    slot.style.minHeight = '0';

    var scale = 1;
    for(var i = 0; i < 4; i++){
      var natural = body.offsetHeight;
      if(natural < 1) break;
      // Already at ~75 % fill (or overflowing) — leave alone.
      if(natural >= bboxHpx * 0.75) break;

      // sqrt damping: paragraph height grows roughly with scale^1.5–2
      // because text re-wraps as chars get wider. sqrt prevents
      // overshoot for wrapping content while still helping short
      // single-line content reach a sensible size.
      var ratio = (bboxHpx * 0.85) / natural;
      var next  = scale * Math.sqrt(ratio);
      next = Math.max(1, Math.min(next, cap));

      if(Math.abs(next - scale) < 0.02) break;     // converged
      scale = next;
      slot.style.setProperty('--slot-scale', scale.toFixed(3));
    }

    // Restore min-height so the slot can serve as a layout anchor again.
    slot.style.minHeight = savedMinH;
  }

  function reflowDocCanvas(canvas){
    if(!canvas) return;
    var slots = Array.from(canvas.querySelectorAll('.doc-slot'));
    if(!slots.length) return;
    // Reset prior canvas growth and per-slot positions, so we measure
    // from the natural aspect-derived geometry every time.
    canvas.style.minHeight = '';
    slots.forEach(function(s){
      if(s.dataset.origTop != null && s.dataset.origTop !== ''){
        s.style.top = s.dataset.origTop + '%';
      }
    });
    var canvasH = canvas.getBoundingClientRect().height;
    if(!canvasH) return;                            // hidden / not laid out

    // ---- step 1: autofit each slot's font to its bbox -------------
    slots.forEach(function(s){ autoFitSlot(s, canvasH); });

    // ---- step 2: cascade-resolve any remaining vertical overlaps --
    var items = slots.map(function(s){
      var top   = parseFloat(s.style.top || '0');
      var left  = parseFloat(s.style.left || '0');
      var width = parseFloat(s.style.width || '0');
      return {
        slot: s,
        left: left,
        right: left + width,
        topPx: (top / 100) * canvasH,
        heightPx: s.offsetHeight,
      };
    });

    var GAP_PX = 4;
    for(var pass = 0; pass < 8; pass++){
      items.sort(function(a, b){ return a.topPx - b.topPx; });
      var changed = false;
      for(var i = 0; i < items.length; i++){
        var cur = items[i];
        var curBottom = cur.topPx + cur.heightPx;
        for(var j = i + 1; j < items.length; j++){
          var other = items[j];
          var xOverlap = !(other.right <= cur.left + 0.2
                        || other.left  >= cur.right - 0.2);
          if(!xOverlap) continue;
          if(other.topPx < curBottom + GAP_PX){
            other.topPx = curBottom + GAP_PX;
            changed = true;
          }
        }
      }
      if(!changed) break;
    }

    items.forEach(function(it){
      it.slot.style.top = (it.topPx / canvasH * 100).toFixed(3) + '%';
    });

    // Grow the canvas if the cascade pushed content past the bottom.
    var maxBottom = 0;
    items.forEach(function(it){
      var b = it.topPx + it.heightPx;
      if(b > maxBottom) maxBottom = b;
    });
    if(maxBottom > canvasH){
      canvas.style.minHeight = Math.ceil(maxBottom + 6) + 'px';
    }
  }

  function reflowPage(page){
    if(!page) return;
    page.querySelectorAll('.doc-canvas').forEach(reflowDocCanvas);
  }

  // Initial pass for the active page, and again once webfonts settle
  // (since Fraunces/Newsreader change text wrapping vs. fallbacks).
  function reflowActive(){ reflowPage(pages[curPage]); }
  // Defer slightly so layout is stable.
  requestAnimationFrame(function(){
    setTimeout(reflowActive, 0);
  });
  if(document.fonts && document.fonts.ready){
    document.fonts.ready.then(reflowActive).catch(function(){});
  }

  // Re-run on resize (debounced) — canvas width changes alter text wrap.
  var resizeT;
  window.addEventListener('resize', function(){
    clearTimeout(resizeT);
    resizeT = setTimeout(reflowActive, 120);
  });

  // Re-run when the user flips to a new page or toggles into Document
  // mode (a previously-hidden page has no layout, so its first reflow
  // can only happen now).
  var origShowPage = showPage;
  showPage = function(i){
    origShowPage(i);
    // Wait a frame so the newly-shown page has dimensions.
    requestAnimationFrame(function(){ reflowPage(pages[curPage]); });
  };
  pages.forEach(function(page){
    var btns = page.querySelectorAll('.trans-toggle button');
    btns.forEach(function(btn){
      btn.addEventListener('click', function(){
        // After the inline mode-switch handler runs, reflow if we're in
        // document mode (reading mode doesn't need it).
        if(btn.dataset.transMode === 'document'){
          requestAnimationFrame(function(){ reflowPage(page); });
        }
      });
    });
  });
  function clearSync(page){
    page.querySelectorAll('.ov-box.is-sync, .r.is-sync, .doc-slot.is-sync')
      .forEach(function(el){ el.classList.remove('is-sync'); });
  }
  function syncFromIndex(page, idx){
    clearSync(page);
    var ovs = page.querySelectorAll('.ov-box[data-region-idx="' + idx + '"]');
    var rs  = page.querySelectorAll(
      '.r[data-region-idx="' + idx + '"], .doc-slot[data-region-idx="' + idx + '"]'
    );
    ovs.forEach(function(el){ el.classList.add('is-sync'); });
    rs.forEach(function(el){ el.classList.add('is-sync'); });
    // Scroll the transcription side into view
    var rPanel = page.querySelector('.trans-panel');
    var visible = page.querySelector(
      '.trans-mode[data-mode="' + rPanel.getAttribute('data-mode') + '"]'
    );
    if(rs.length){
      var first = Array.from(rs).find(function(el){
        return el.closest('.trans-mode[data-mode="'
          + rPanel.getAttribute('data-mode') + '"]');
      });
      if(first){
        first.scrollIntoView({ behavior:'smooth', block:'center' });
      }
    }
  }
  pages.forEach(function(page){
    page.addEventListener('click', function(e){
      var el = e.target.closest('[data-region-idx]');
      if(!el) return;
      var idx = el.dataset.regionIdx;
      if(idx == null) return;
      // If we clicked an entity inside a region, that's fine — the
      // ancestor still resolves; bail out only on irrelevant chrome.
      if(e.target.closest('a, button:not(.doc-slot):not(.ov-box):not(.r)')) {
        return;
      }
      syncFromIndex(page, idx);
    });
  });

  // Clear sync on Escape
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape') pages.forEach(clearSync);
  });

  // ============================================================
  // Legend chip toggling (entities and regions)
  // ============================================================
  document.querySelectorAll('.chip').forEach(function(chip){
    chip.addEventListener('click', function(){
      var off = chip.classList.toggle('is-off');
      var type = chip.dataset.type;
      var scope = chip.dataset.scope;
      if(scope === 'entity'){
        document.querySelectorAll('.ent[data-type="' + type + '"]')
          .forEach(function(el){
            el.classList.toggle('hide-type', off);
          });
      } else if(scope === 'region'){
        document.querySelectorAll(
          '.r[data-region-type="' + type + '"], ' +
          '.doc-slot[data-region-type="' + type + '"]'
        ).forEach(function(el){ el.classList.toggle('hide-type', off); });
        document.querySelectorAll('.region-overlay').forEach(function(ov){
          ov.classList.toggle('hide-type-' + type, off);
        });
      }
    });
  });

  // ============================================================
  // Search-as-you-type (per page)
  // ============================================================
  function searchInPage(page, query){
    var q = (query || '').trim();
    var rs = page.querySelectorAll('.r, .doc-slot');
    if(!q){
      rs.forEach(function(el){
        el.classList.remove('search-no-match', 'search-match');
      });
      // Clear hit highlights
      page.querySelectorAll('.search-hit').forEach(function(h){
        var t = document.createTextNode(h.textContent);
        h.parentNode.replaceChild(t, h);
      });
      return;
    }
    var qLow = q.toLowerCase();
    rs.forEach(function(el){
      var text = (el.textContent || '').toLowerCase();
      var hit = text.indexOf(qLow) !== -1;
      el.classList.toggle('search-no-match', !hit);
      el.classList.toggle('search-match', hit);
    });
  }
  pages.forEach(function(page){
    var input = page.querySelector('.search-input');
    if(!input) return;
    var t;
    input.addEventListener('input', function(){
      clearTimeout(t);
      t = setTimeout(function(){ searchInPage(page, input.value); }, 80);
    });
    input.addEventListener('keydown', function(e){
      if(e.key === 'Escape'){
        input.value = '';
        searchInPage(page, '');
        input.blur();
      }
    });
  });

  // ============================================================
  // Copy plain text
  // ============================================================
  function showToast(msg){
    toast.innerHTML =
      '<svg viewBox="0 0 20 20" width="14" height="14" class="i-check" ' +
      'aria-hidden="true"><path d="M4 10l4 4 8-8" fill="none" ' +
      'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' +
      'stroke-linejoin="round"/></svg><span>' + msg + '</span>';
    toast.classList.add('is-visible');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function(){
      toast.classList.remove('is-visible');
    }, 1800);
  }
  document.querySelectorAll('.tool-btn--copy').forEach(function(btn){
    btn.addEventListener('click', function(){
      var txt = btn.dataset.copy || '';
      if(!txt){ showToast('Nothing to copy'); return; }
      if(navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(txt).then(
          function(){ showToast('Copied to clipboard'); },
          function(){ fallbackCopy(txt); }
        );
      } else {
        fallbackCopy(txt);
      }
    });
  });
  function fallbackCopy(txt){
    var ta = document.createElement('textarea');
    ta.value = txt;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try{
      document.execCommand('copy');
      showToast('Copied to clipboard');
    } catch(e){
      showToast('Copy failed');
    }
    document.body.removeChild(ta);
  }

  // ============================================================
  // Map toggle + lazy Leaflet init
  // ============================================================
  var maps = {};
  document.querySelectorAll('.tool-btn--map').forEach(function(btn){
    btn.addEventListener('click', function(){
      var id = btn.dataset.toggle;
      var wrap = document.getElementById(id);
      if(!wrap) return;
      var isOpen = wrap.classList.toggle('is-open');
      btn.classList.toggle('is-on', isOpen);
      if(isOpen && !maps[id]){
        initMap(id, wrap);
      } else if(isOpen && maps[id]){
        setTimeout(function(){ maps[id].invalidateSize(); }, 60);
      }
    });
  });
  function initMap(id, wrap){
    if(typeof L === 'undefined'){
      // Leaflet not yet loaded — retry shortly
      setTimeout(function(){ initMap(id, wrap); }, 200);
      return;
    }
    var raw = wrap.dataset.locations;
    if(!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch(e){ return; }
    if(!data.locations || !data.locations.length) return;
    var map = L.map(wrap, {
      attributionControl:false,
      zoomControl:true,
      scrollWheelZoom:false
    }).setView(data.center || [data.locations[0].lat, data.locations[0].lon], 4);
    // CartoDB Voyager — muted scholarly palette, works from file:// origins
    // (OpenStreetMap's own tiles reject requests without a Referer header,
    // which means they fail when the HTML is opened as a local file.)
    L.tileLayer(
      'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/' +
      '{z}/{x}/{y}{r}.png',
      {
        maxZoom: 19,
        subdomains: 'abcd',
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">' +
          'OpenStreetMap</a> · © <a href="https://carto.com/attributions">' +
          'CARTO</a>'
      }
    ).addTo(map);
    L.control.attribution({ prefix:false, position:'bottomright' })
      .addAttribution(
        '© <a href="https://www.openstreetmap.org/copyright">' +
        'OpenStreetMap</a> · © <a href="https://carto.com/attributions">' +
        'CARTO</a>'
      )
      .addTo(map);
    var bounds = [];
    data.locations.forEach(function(loc){
      var marker = L.circleMarker([loc.lat, loc.lon], {
        radius: 7,
        fillColor: '#91361f',
        color: '#fdf8e8',
        weight: 2,
        opacity: 1,
        fillOpacity: 0.85
      }).addTo(map);
      marker.bindPopup(
        '<strong>' + escapeHtml(loc.name) + '</strong>' +
        (loc.display ? '<br><span style="font-size:.85em;color:#666">'
          + escapeHtml(loc.display) + '</span>' : '')
      );
      bounds.push([loc.lat, loc.lon]);
    });
    if(bounds.length > 1){
      map.fitBounds(bounds, { padding:[28, 28], maxZoom: 9 });
    }
    setTimeout(function(){ map.invalidateSize(); }, 80);
    maps[id] = map;
  }
  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
})();
"""

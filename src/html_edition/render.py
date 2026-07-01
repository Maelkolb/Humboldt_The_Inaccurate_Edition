"""Per-page HTML rendering: facsimile overlay, document/reading panels, diff view."""

from __future__ import annotations

import html as html_lib
from typing import Dict, List, Optional, Tuple

from ..models import Entity, Region, is_opposite_marginal
from .markup import (
    authority_source, find_entity_spans, postprocess_editorial, render_block,
    render_plain, strip_editorial_markers,
)
from .textcmp import render_diff, render_gt_plain

LANG_NAMES = {"de": "German", "fr": "French", "la": "Latin", "es": "Spanish"}

LANG_SHORT = {"de": "DE", "fr": "FR", "la": "LA", "es": "ES"}

DEFAULT_PAGE_ASPECT = 0.72

REGION_FALLBACK_COLOR = "#6b5e4d"

ENTITY_FALLBACK_COLOR = "#6b5e4d"

def _annotate_text(
    text: str, entities: List[Entity], ec: Dict[str, str]
) -> str:
    """Render text with entity highlighting plus editorial markup."""
    if not text:
        return ""
    spans = find_entity_spans(text, entities) if entities else []
    parts, cur = [], 0
    for s, e, ent in spans:
        if s > cur:
            parts.append(render_plain(text[cur:s]))
        color = ec.get(ent.entity_type, ENTITY_FALLBACK_COLOR)
        # Tooltip text must be plain — strip this project's editorial marker
        # syntax (~~struck~~, <u>, [?]) before it goes into a `title="..."`
        # attribute, or postprocess_editorial's later regex pass will inject
        # a real tag *inside* the attribute value and corrupt the markup.
        ctx = html_lib.escape(strip_editorial_markers(ent.context))
        norm = (
            f" → {html_lib.escape(strip_editorial_markers(ent.normalized_form))}"
            if ent.normalized_form else ""
        )
        surface = render_plain(text[s:e])
        # Authority link (populated by the optional entity-linking post-process).
        # Prefer the eHD register page (persons/places); fall back to the raw
        # authority URI (e.g. GBIF for plants, which have no eHD page).
        link = getattr(ent, "ehd_url", None) or getattr(ent, "authority_uri", None)
        if link:
            auth_uri = getattr(ent, "authority_uri", None)
            auth_label = getattr(ent, "authority_label", None)
            src = authority_source(auth_uri or link)
            bits = [f"{html_lib.escape(ent.entity_type)}: {ctx}{norm}"]
            if auth_label:
                bits.append(f"⟶ {html_lib.escape(strip_editorial_markers(auth_label))}")
            ref = ent.ehd_id if getattr(ent, "ehd_id", None) else ""
            tail = " · ".join(x for x in (f"{src}" if src else "", ref) if x)
            if tail:
                bits.append(tail)
            title = " | ".join(bits)
            parts.append(
                f'<a class="ent is-linked" data-type="{html_lib.escape(ent.entity_type)}"'
                f' style="--ent:{color};" href="{html_lib.escape(link)}"'
                f' target="_blank" rel="noopener noreferrer"'
                f' title="{title}">{surface}'
                f'<span class="ent-ext" aria-hidden="true">↗</span></a>'
            )
        else:
            parts.append(
                f'<mark class="ent" data-type="{html_lib.escape(ent.entity_type)}"'
                f' style="--ent:{color};" '
                f'title="{html_lib.escape(ent.entity_type)}: {ctx}{norm}">'
                f'{surface}</mark>'
            )
        cur = e
    if cur < len(text):
        parts.append(render_plain(text[cur:]))
    return postprocess_editorial("".join(parts))

def _render_region_text(
    region: Region,
    entities: List[Entity],
    ec: Dict[str, str],
) -> str:
    """
    Render a region's content for inline display in HTML.

    When the region has no ``ground_truth_content``, returns the same
    annotated HTML as ``_annotate_text(region.content, ...)`` — the
    existing behaviour, unchanged.

    When ground-truth content IS available, returns a single wrapper
    ``<span class="region-content has-gt">`` containing three child spans —
    one per source mode (``gemini`` / ``gt`` / ``diff``). CSS on the
    enclosing page (``[data-source-mode="…"]``) controls which one is
    visible.

    (The pre-consistency snapshot — ``region.content_pre_consistency`` —
    is persisted to the JSON output but is NOT exposed in the HTML
    viewer; it's available for offline inspection / diffing.)
    """
    gemini_html = _annotate_text(region.content, entities, ec)

    gt = region.ground_truth_content
    if gt is None or gt == "":
        # No GT match: still emit the mode-wrapper so the source toggle can hide the
        # Gemini text in GT/Diff mode (otherwise it stays visible in the GT tab).
        return (
            '<span class="region-content has-gt is-empty-gt">'
            f'<span class="rc rc--gemini">{gemini_html}</span>'
            '<span class="rc rc--gt"><em class="rc-empty">— no ground-truth match —</em></span>'
            f'<span class="rc rc--diff">{gemini_html}</span>'
            '</span>'
        )

    # Ground-truth text: annotate with the eHD gold-standard entities attached
    # by ground_truth.py (clickable register/authority links via _annotate_text).
    # Falls back to plain rendering when none are present (e.g. TEI-only mode or
    # a region whose GT slice has no tagged entities).
    gt_entities = getattr(region, "ground_truth_entities", None) or []
    gt_html = (
        _annotate_text(gt, gt_entities, ec)
        if gt_entities
        else render_gt_plain(gt)
    )
    diff_html = render_diff(region.content or "", gt)

    conf = region.ground_truth_confidence
    conf_attr = ""
    if conf is not None:
        try:
            conf_attr = f' data-gt-confidence="{float(conf):.2f}"'
        except (TypeError, ValueError):
            pass

    return (
        f'<span class="region-content has-gt"{conf_attr}>'
        f'<span class="rc rc--gemini">{gemini_html}</span>'
        f'<span class="rc rc--gt">{gt_html}</span>'
        f'<span class="rc rc--diff">{diff_html}</span>'
        f'</span>'
    )

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
            f"{render_plain(str(c))}"
            f"</{'th' if ri == 0 else 'td'}>"
            for c in row
        ) + "</tr>"
        for ri, row in enumerate(td.get("cells", []))
    )
    cap = (
        f'<caption>{render_plain(td.get("caption", ""))}</caption>'
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
    if r.region_type == "pasted_slip":
        cls += " is-pasted-slip"
    if getattr(r, "writing_layer", None) == "later_addition":
        cls += " is-later-addition"
    mp = getattr(r, "marginal_position", None)
    if mp == "left":
        cls += " is-margin-left"
    elif mp == "right":
        cls += " is-margin-right"
    elif is_opposite_marginal(mp):
        cls += " is-margin-opposite"
    elif mp in ("mTop", "top"):
        cls += " is-margin-top"
    elif mp in ("mBottom", "bottom"):
        cls += " is-margin-bottom"
    # Regions with no matched ground truth are dimmed in GT/Diff modes so the
    # source toggle is visibly responsive even on partially-matched pages.
    if not getattr(r, "ground_truth_content", None):
        cls += " is-no-gt"
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
    color = rc.get(rtype, REGION_FALLBACK_COLOR)
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
            f'{render_plain(region.content or "[sketch]")}</p>'
        )
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "entry_heading":
        annotated = _render_region_text(region, entities, ec)
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
            body = f'<pre class="r-data">{render_block(region.content)}</pre>'
        else:
            body = '<p class="r-text r-faint"><em>[Table not parsed]</em></p>'
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "calculation":
        body = f'<pre class="r-data">{render_block(region.content or "")}</pre>'
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "crossed_out":
        annotated = _render_region_text(region, entities, ec)
        repl = (
            f'<div class="r-replacement">'
            f'<span class="r-replacement-label">Replaced by</span>'
            f'<span>{render_plain(region.crossed_out_text)}</span></div>'
            if region.crossed_out_text else ""
        )
        return (
            f'{open_tag}{meta}<p class="r-crossed">{annotated}</p>'
            f'{repl}{note}{close_tag}'
        )

    if rtype == "coordinates":
        body = (
            f'<p class="r-coords">{render_plain(region.content or "")}</p>'
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
            body = f'<pre class="r-data">{render_block(region.content)}</pre>'
        else:
            body = '<p class="r-text r-faint"><em>[List not parsed]</em></p>'
        return f'{open_tag}{meta}{body}{note}{close_tag}'

    if rtype == "marginal_note":
        mp = getattr(region, "marginal_position", None)
        if is_opposite_marginal(mp):
            return (
                f'{open_tag}{meta}'
                f'<p class="r-text r-opposite">'
                f'<em>[Bleedthrough from opposite folio — not transcribed]</em>'
                f'</p>{note}{close_tag}'
            )
        annotated = _render_region_text(region, entities, ec)
        return (
            f'{open_tag}{meta}<p class="r-text">{annotated}</p>'
            f'{note}{close_tag}'
        )

    # default: main_text, bibliographic_ref, page_number, etc.
    annotated = _render_region_text(region, entities, ec)
    return f'{open_tag}{meta}<p class="r-text">{annotated}</p>{note}{close_tag}'

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
        desc = _render_region_text(region, entities, ec) \
            if region.content else render_plain("[sketch]")
        return (
            f'<span class="doc-sketch-desc">'
            f'<span class="doc-sketch-mark" aria-hidden="true">✦</span>'
            f'{desc}</span>'
        )

    if rtype in ("observation_table", "instrument_list"):
        cells = (
            (region.table_data or {}).get("cells")
            if region.table_data else None
        )
        if cells:
            return _render_table_html(region.table_data)
        if region.content:
            return f'<pre class="doc-data">{render_block(region.content)}</pre>'
        return '<em class="doc-faint">[unparsed]</em>'

    if rtype == "calculation":
        return f'<pre class="doc-data">{render_block(region.content or "")}</pre>'

    if rtype == "coordinates":
        return (
            f'<span class="doc-coords">'
            f'{_render_region_text(region, entities, ec)}</span>'
        )

    if rtype == "crossed_out":
        annotated = _render_region_text(region, entities, ec)
        repl = ""
        if region.crossed_out_text:
            repl = (
                f'<span class="doc-crossed-repl" '
                f'title="Replaced by">→ '
                f'{render_plain(region.crossed_out_text)}</span>'
            )
        return f'<span class="doc-crossed">{annotated}</span>{repl}'

    if rtype == "marginal_note":
        mp = getattr(region, "marginal_position", None)
        if is_opposite_marginal(mp):
            return '<em class="doc-faint">[opposite-folio bleedthrough]</em>'
        return _render_region_text(region, entities, ec)

    return _render_region_text(region, entities, ec)

_DEOVERLAP_GAP = 0.5

def _deoverlap_slots(items: List[Dict], gap: float = _DEOVERLAP_GAP,
                     passes: int = 12) -> None:
    """Gently separate any overlapping Document-view boxes, in place.

    Region detector bounding boxes occasionally overlap (a heading box dipping
    into the text below it, two stacked paragraphs, or two columns sharing a
    seam). Each region keeps its *size*; only its top/left move. For an
    overlapping pair we resolve along the axis of the *smaller* overlap — the
    minimum nudge: a vertical overlap pushes the lower box straight down, a
    horizontal overlap (side-by-side columns) splits the pair apart sideways.
    Everything is in the page's normalised 0–100 space; a handful of passes
    converge. Because the slot's rendered rectangle is exactly its bbox
    rectangle (the runtime fit only scales the text *inside* the box), doing
    this at generation time is equivalent to — and far simpler than — a
    runtime layout cascade.
    """
    n = len(items)
    if n < 2:
        return
    for _ in range(passes):
        order = sorted(range(n), key=lambda i: (items[i]["t"], items[i]["l"]))
        moved = False
        for ai in range(n):
            for bi in range(ai + 1, n):
                a = items[order[ai]]
                b = items[order[bi]]
                ox = min(a["l"] + a["w"], b["l"] + b["w"]) - max(a["l"], b["l"])
                oy = min(a["t"] + a["h"], b["t"] + b["h"]) - max(a["t"], b["t"])
                if ox <= 0.001 or oy <= 0.001:
                    continue
                if oy <= ox:                       # vertical: lower box moves down
                    lo, hi = (a, b) if a["t"] <= b["t"] else (b, a)
                    hi["t"] = lo["t"] + lo["h"] + gap
                else:                              # horizontal: split apart
                    push = (ox + gap) / 2.0
                    lft, rgt = (a, b) if a["l"] <= b["l"] else (b, a)
                    lft["l"] = max(0.0, lft["l"] - push)
                    rgt["l"] = min(100.0 - rgt["w"], rgt["l"] + push)
                moved = True
        if not moved:
            break

def _fit_doc_aspect(items: List[Dict], aspect: float) -> float:
    """If de-overlap pushed any box past the page bottom (>100%), scale all
    vertical positions back into [0,100] and return a proportionally taller
    canvas aspect so nothing is clipped. The vertical scale is uniform, so
    every slot keeps the same rendered pixel size and the text-fit is
    unchanged — the canvas simply grows tall enough to hold the content.
    """
    if not items:
        return aspect
    max_bottom = max(it["t"] + it["h"] for it in items)
    if max_bottom <= 100.0:
        return aspect
    f = 100.0 / max_bottom
    for it in items:
        it["t"] *= f
        it["h"] *= f
    return aspect * f

def build_doc_panel(
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

    # Resolve each region to a rectangle, then gently separate any overlapping
    # boxes and, if that pushed content off the page bottom, grow the canvas to
    # contain it. Both steps are pure geometry on the bbox rectangles.
    items: List[Dict] = []
    for r in positioned:
        rect = _bbox_rect_pct(r)
        if rect is None:
            continue
        top, left, width, height = rect
        items.append({"r": r, "t": top, "l": left, "w": width, "h": height})

    _deoverlap_slots(items)
    aspect = _fit_doc_aspect(items, aspect)

    slots = []
    for it in items:
        r = it["r"]
        top, left, width, height = it["t"], it["l"], it["w"], it["h"]
        color = rc.get(r.region_type, REGION_FALLBACK_COLOR)
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

def build_reading_panel(
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

def build_overlay(
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
        color = rc.get(region.region_type, REGION_FALLBACK_COLOR)
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

def plain_text_from_regions(regions: List[Region]) -> str:
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
        elif r.region_type in ("page_number"):
            out.append(f"[{r.region_type}] {text}")
        else:
            out.append(text)
    return "\n\n".join(out).strip()

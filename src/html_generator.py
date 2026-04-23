"""
HTML Generator – Humboldt Journal Digital Edition

Integrates the updated code-cell version with:
- SVG overlay of region bounding boxes on facsimile (toggleable)
- Click text region → highlight on facsimile (and vice versa)
- Inline editorial markup: ~~strikethrough~~ and <u>underline</u>
- Entity highlighting, maps, responsive design
- Bbox-driven region ordering and margin-note positioning so the
  transcription panel mirrors the physical page layout.
"""

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
from .region_detection import load_image_as_base64

logger = logging.getLogger(__name__)

LANG_NAMES = {"de": "German", "fr": "French", "la": "Latin", "es": "Spanish"}
LANG_SHORT = {"de": "DE", "fr": "FR", "la": "LA", "es": "ES"}

# Maximum width (px) for embedded facsimile images. Larger originals are
# downscaled before base64-encoding so the HTML file stays manageable.
EMBED_IMAGE_MAX_WIDTH = 1000
EMBED_IMAGE_QUALITY   = 72   # JPEG quality for embedded thumbnails


def _resize_image_for_embed(image_path: Path) -> str:
    """
    Open *image_path*, downscale to at most EMBED_IMAGE_MAX_WIDTH wide,
    re-compress as JPEG, and return a base64-encoded string.
    """
    with Image.open(image_path) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if w > EMBED_IMAGE_MAX_WIDTH:
            ratio = EMBED_IMAGE_MAX_WIDTH / w
            img = img.resize((EMBED_IMAGE_MAX_WIDTH, int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=EMBED_IMAGE_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()


def _find_entity_spans(text, entities):
    raw = []
    for ent in entities:
        if not ent.text: continue
        s = 0
        while True:
            i = text.find(ent.text, s)
            if i == -1: break
            raw.append((i, i + len(ent.text), ent))
            s = i + 1
    raw.sort(key=lambda x: (x[0], -(x[1]-x[0])))
    result, cur = [], 0
    for s, e, ent in raw:
        if s >= cur: result.append((s, e, ent)); cur = e
    return result


def _render_plain(text):
    """Render plain text with uncertain-reading markers and inline editorial markup."""
    escaped = html_lib.escape(text)
    # ~~strikethrough~~ → <del> (crossed-out words)
    escaped = re.sub(
        r'~~(.+?)~~',
        r'<del class="inline-struck" title="Struck through in original">\1</del>',
        escaped,
    )
    # <u>underline</u> → <span class="inline-underline"> (underlined words)
    escaped = re.sub(
        r'&lt;u&gt;(.+?)&lt;/u&gt;',
        r'<span class="inline-underline" title="Underlined in original">\1</span>',
        escaped,
    )
    # Uncertain readings
    escaped = re.sub(r'(\w+)\[(\?)\]',
        r'<span class="uncertain-word" title="Uncertain reading">\1<span class="unc">[?]</span></span>', escaped)
    escaped = re.sub(r'\[(\?)\]',
        r'<span class="unc" title="Uncertain reading">[?]</span>', escaped)
    return escaped.replace("\n", "<br>\n")


def _annotate_text(text, entities, ec):
    if not text: return ""
    spans = _find_entity_spans(text, entities) if entities else []
    parts, cur = [], 0
    for s, e, ent in spans:
        if s > cur: parts.append(_render_plain(text[cur:s]))
        color = ec.get(ent.entity_type, "#9e9e9e")
        ctx = html_lib.escape(ent.context or "")
        norm = f" -> {html_lib.escape(ent.normalized_form)}" if ent.normalized_form else ""
        parts.append(
            f'<mark class="entity" data-type="{html_lib.escape(ent.entity_type)}" '
            f'style="--ent-color:{color};" '
            f'title="{html_lib.escape(ent.entity_type)}: {ctx}{norm}">'
            f'{_render_plain(text[s:e])}</mark>')
        cur = e
    if cur < len(text): parts.append(_render_plain(text[cur:]))
    return "".join(parts)


def _lang_badges(languages):
    if not languages: return ""
    return '<span class="lang-badges">' + " ".join(
        f'<span class="lang-badge lang-{l}" title="{LANG_NAMES.get(l,l)}">{LANG_SHORT.get(l,l)}</span>'
        for l in languages) + '</span>'


def _render_table_html(td):
    rows = "".join(
        "<tr>" + "".join(
            f"<{'th' if ri==0 else 'td'}>{html_lib.escape(str(c))}</{'th' if ri==0 else 'td'}>"
            for c in row) + "</tr>"
        for ri, row in enumerate(td.get("cells", [])))
    cap = f'<caption>{html_lib.escape(td.get("caption",""))}</caption>' if td.get("caption") else ""
    return f'<table class="obs-table">{cap}<tbody>{rows}</tbody></table>'


def _render_region(region, entities, ec, rc, rl, page_idx):
    rtype = region.region_type
    ridx = region.region_index
    color = rc.get(rtype, "#546e7a")
    label = rl.get(rtype, rtype.replace("_", " ").title())
    tag = f'<span class="region-tag" style="--tag-color:{color};">{label}</span>'
    lang = _lang_badges(region.languages)
    pos = f'<span class="pos-note">{html_lib.escape(region.position)}</span>' if region.position else ""
    note = (f'<div class="ed-note"><span class="ed-icon">&#9998;</span> '
            f'{html_lib.escape(region.editorial_note)}</div>') if region.editorial_note else ""
    meta = f'<div class="region-meta">{lang}{pos}</div>' if lang or pos else ""

    # Extra CSS classes for special region flags
    extra_cls = ""
    if getattr(region, "is_pasted_slip", False) or rtype == "pasted_slip":
        extra_cls += " region--pasted-slip"
    if getattr(region, "writing_layer", None) == "later_addition":
        extra_cls += " region--later-addition"

    wo = (f'<div class="region region--{rtype}{extra_cls}" data-region-type="{rtype}" '
          f'data-region-idx="{ridx}" data-page-idx="{page_idx}" tabindex="0">')
    wc = '</div>'
    hd = f'{tag}{meta}'

    if region.is_visual or rtype == "sketch":
        return f'{wo}{hd}<p class="sketch-desc">{html_lib.escape(region.content or "")}</p>{note}{wc}'
    if rtype == "entry_heading":
        a = _annotate_text(region.content, entities, ec)
        return f'{wo}{hd}<h2 class="entry-heading">{a}</h2>{note}{wc}'
    if rtype == "observation_table":
        cells = (region.table_data or {}).get("cells") if region.table_data else None
        if cells:
            return f'{wo}{hd}{_render_table_html(region.table_data)}{note}{wc}'
        raw = html_lib.escape(region.content or "")
        body = (f'<pre class="calc-body">{raw}</pre>' if raw
                else '<p class="body-text fg-faint"><em>[Table not parsed – no structured data returned]</em></p>')
        return f'{wo}{hd}{body}{note}{wc}'
    if rtype == "calculation":
        return f'{wo}{hd}<pre class="calc-body">{html_lib.escape(region.content or "")}</pre>{note}{wc}'
    if rtype == "crossed_out":
        a = _annotate_text(region.content, entities, ec)
        repl = (f'<div class="repl-text">Replaced by: {html_lib.escape(region.crossed_out_text)}</div>') if region.crossed_out_text else ""
        return f'{wo}{hd}<p class="crossed-text">{a}</p>{repl}{note}{wc}'
    if rtype == "coordinates":
        return f'{wo}{hd}<p class="coords-body">{html_lib.escape(region.content or "")}</p>{note}{wc}'
    if rtype == "instrument_list":
        cells = (region.table_data or {}).get("cells") if region.table_data else None
        if cells:
            return f'{wo}{hd}{_render_table_html(region.table_data)}{note}{wc}'
        raw = html_lib.escape(region.content or "")
        body = (f'<pre class="calc-body">{raw}</pre>' if raw
                else '<p class="body-text fg-faint"><em>[List not parsed – no structured data returned]</em></p>')
        return f'{wo}{hd}{body}{note}{wc}'
    if rtype == "marginal_note":
        mp = getattr(region, "marginal_position", None)
        if mp == "opposite":
            return (f'{wo}{hd}'
                    f'<p class="body-text opposite-bleed">'
                    f'<em>[Bleedthrough from opposite folio — not transcribed]</em></p>'
                    f'{note}{wc}')
        a = _annotate_text(region.content, entities, ec)
        return f'{wo}{hd}<p class="body-text">{a}</p>{note}{wc}'
    a = _annotate_text(region.content, entities, ec)
    return f'{wo}{hd}<p class="body-text">{a}</p>{note}{wc}'


# ---------------------------------------------------------------------------
# Bbox-driven ordering helpers
# ---------------------------------------------------------------------------

def _sort_key(r: Region) -> Tuple[float, float, int]:
    """
    Sort key for regions so they appear in natural reading order on the page.
    Primary: bbox y_min (top-to-bottom), Secondary: bbox x_min (left-to-right),
    Fallback: region_index (for regions without bbox — e.g. TEI-parsed).
    """
    if r.bbox and len(r.bbox) == 4:
        return (float(r.bbox[0]), float(r.bbox[1]), r.region_index)
    # Regions without bbox fall back to their natural order
    return (float("inf"), float("inf"), r.region_index)


def _top_pct_from_bbox(r: Region) -> Optional[float]:
    """Return y_min as a percentage (0..100) from the bbox, or None."""
    if r.bbox and len(r.bbox) == 4:
        return max(0.0, min(100.0, float(r.bbox[0]) / 10.0))
    return None


def _build_transcription_panel(regions, entities, ec, rc, rl, page_idx):
    """
    Build the transcription panel HTML.

    Layout strategy:
    - When there are left/right marginal notes, use a 3-column CSS grid:
        [left margin] [main body] [right margin]
      Margin notes in left/right columns are ABSOLUTELY POSITIONED at
      `top: <y_min/10>%`, so their vertical location mirrors their position
      on the original page. The main column stretches the container's height,
      so the percentage resolves against a height that corresponds to the
      page's reading region.
    - Main-column regions are sorted by bbox (y_min, x_min) so reading order
      follows the physical page geometry.
    - mTop / mBottom marginal strips and opposite-folio bleedthrough sit
      above/below the 3-column block as ordinary flow strips.
    """
    def _mp(r):
        return getattr(r, "marginal_position", None)

    left_notes  = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "left"]
    right_notes = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "right"]
    top_notes   = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "mTop"]
    bot_notes   = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "mBottom"]
    opp_notes   = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "opposite"]

    # Everything else → main column
    placed = {id(r) for r in left_notes + right_notes + top_notes + bot_notes + opp_notes}
    main_regions = [r for r in regions if id(r) not in placed]

    # Bbox-based ordering for reading flow
    main_regions.sort(key=_sort_key)
    top_notes.sort(key=_sort_key)
    bot_notes.sort(key=_sort_key)
    left_notes.sort(key=_sort_key)
    right_notes.sort(key=_sort_key)

    def _html(rs):
        return "".join(_render_region(r, entities, ec, rc, rl, page_idx) for r in rs)

    def _html_margin_absolute(rs):
        """
        Render margin notes inside absolute-positioned anchors so their
        vertical position on the transcription panel mirrors the original
        page's bbox.y_min. Falls back to stacked anchors (10%, 30%, 50%...)
        when bboxes are missing.
        """
        if not rs:
            return ""
        parts = []
        for i, r in enumerate(rs):
            top = _top_pct_from_bbox(r)
            if top is None:
                # Evenly distribute as a graceful fallback (TEI-only output)
                top = 8.0 + (i * 16.0)
            parts.append(
                f'<div class="margin-anchor" style="top:{top:.2f}%;">'
                f'{_render_region(r, entities, ec, rc, rl, page_idx)}'
                f'</div>'
            )
        return "".join(parts)

    # Top notes strip
    top_html = (f'<div class="margin-strip margin-strip--top">'
                f'<span class="strip-label">Top margin</span>{_html(top_notes)}</div>'
                if top_notes else "")

    # Bottom notes strip
    bot_html = (f'<div class="margin-strip margin-strip--bottom">'
                f'<span class="strip-label">Bottom margin</span>{_html(bot_notes)}</div>'
                if bot_notes else "")

    # Opposite-folio bleedthrough strip (collapsed / visually de-emphasised)
    opp_html = (f'<div class="margin-strip margin-strip--opposite">'
                f'<span class="strip-label">Opposite folio</span>{_html(opp_notes)}</div>'
                if opp_notes else "")

    # 3-column body or single-column if no left/right margins
    if left_notes or right_notes:
        body_html = (
            f'<div class="page-body page-body--three-col">'
            f'<div class="margin-col margin-col--left">'
            f'{_html_margin_absolute(left_notes)}</div>'
            f'<div class="main-col">{_html(main_regions)}</div>'
            f'<div class="margin-col margin-col--right">'
            f'{_html_margin_absolute(right_notes)}</div>'
            f'</div>'
        )
    else:
        body_html = f'<div class="page-body"><div class="main-col">{_html(main_regions)}</div></div>'

    return top_html + body_html + bot_html + opp_html


def _build_overlay(regions, rc, rl):
    boxes = []
    for region in regions:
        if not region.bbox or len(region.bbox) != 4:
            continue
        y1, x1, y2, x2 = region.bbox
        top = y1 / 10.0
        left = x1 / 10.0
        width = (x2 - x1) / 10.0
        height = (y2 - y1) / 10.0
        color = rc.get(region.region_type, "#546e7a")
        lab = rl.get(region.region_type, region.region_type.replace("_", " ").title())
        boxes.append(
            f'<div class="ov-box" style="top:{top:.2f}%;left:{left:.2f}%;'
            f'width:{width:.2f}%;height:{height:.2f}%;'
            f'--ov-color:{color};" '
            f'data-region-idx="{region.region_index}" '
            f'data-region-type="{region.region_type}">'
            f'<span class="ov-label" style="background:{color};">{lab}</span>'
            f'</div>')
    if not boxes:
        return ""
    return '<div class="region-overlay hidden">' + "".join(boxes) + '</div>'


ICON_MAP = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" style="width:13px;height:13px;vertical-align:middle;"><path d="M10 17s-6-5.2-6-9a6 6 0 1112 0c0 3.8-6 9-6 9z"/><circle cx="10" cy="8" r="2"/></svg>'


def generate_html_edition(
    results,
    output_path,
    title="Humboldt - the inaccurate Edition",
    subtitle="",
    entity_colors=None,
    entity_labels=None,
    region_colors=None,
    region_labels=None,
    image_folder=None,
    image_ref_prefix=None,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ec = entity_colors or {}
    el = entity_labels or {}
    rc = region_colors or {}
    rl = region_labels or {}

    ent_chips = "".join(
        f'<span class="chip" data-type="{et}"><span class="chip-dot" style="background:{co};"></span>{el.get(et,et)}</span>'
        for et, co in ec.items())

    used_rt = set(reg.region_type for r in results for reg in r.regions)
    reg_chips = "".join(
        f'<span class="chip" data-type="{rt}"><span class="chip-dot" style="background:{rc.get(rt,"#546e7a")};"></span>{rl.get(rt,rt.replace("_"," ").title())}</span>'
        for rt in sorted(used_rt))

    options = "".join(
        f'<option value="{i}">Fol. {r.folio_label}{" - Entries " + ", ".join(r.entry_numbers) if r.entry_numbers else ""}</option>'
        for i, r in enumerate(results))

    page_divs = []
    for idx, result in enumerate(results):
        facs_img = ""
        if image_ref_prefix is not None:
            pfx = (image_ref_prefix.rstrip("/") + "/") if image_ref_prefix else ""
            facs_img = f'<img src="{pfx}{result.image_filename}" alt="Fol. {result.folio_label}" loading="lazy">'
        elif image_folder:
            ip = Path(image_folder) / result.image_filename
            if ip.exists():
                b64 = _resize_image_for_embed(ip)
                facs_img = f'<img src="data:image/jpeg;base64,{b64}" alt="Fol. {result.folio_label}">'

        overlay = _build_overlay(result.regions, rc, rl)
        facs_panel = ""
        if facs_img:
            facs_panel = (
                '<div class="facsimile-panel">'
                '<div class="facs-toolbar"><button class="facs-btn facs-btn-overlay">Regions</button></div>'
                f'<div class="facs-image-wrap"><div class="facs-zoom-layer">{facs_img}{overlay}</div></div></div>')

        map_html = ""
        if result.locations:
            locs = {"locations": [{"name":l.name,"lat":l.lat,"lon":l.lon,"display":l.display_name} for l in result.locations],
                    "center": [sum(l.lat for l in result.locations)/len(result.locations), sum(l.lon for l in result.locations)/len(result.locations)]}
            map_html = (f'<button class="map-btn" data-toggle="map-{idx}">{ICON_MAP} Map ({len(result.locations)} locations)</button>'
                        f'<div class="map-wrap" id="map-{idx}" data-locations="{html_lib.escape(json.dumps(locs))}"></div>')

        counts = {}
        for e in result.entities: counts[e.entity_type] = counts.get(e.entity_type, 0) + 1
        stats = ""
        if counts:
            stats = '<div class="stats-row">' + "".join(
                f'<span class="stat-chip"><span class="stat-dot" style="background:{ec.get(t,"#999")};"></span>{t} ({c})</span>'
                for t, c in sorted(counts.items(), key=lambda x: -x[1])) + '</div>'

        entry_html = f'<span class="entry-nums">Entries {", ".join(result.entry_numbers)}</span>' if result.entry_numbers else ""
        lang_html = f'<span class="page-langs">Languages: {", ".join(LANG_NAMES.get(l,l) for l in result.page_languages)}</span>' if result.page_languages else ""
        trans_panel_html = _build_transcription_panel(
            result.regions, result.entities, ec, rc, rl, idx)
        cols = "page-columns" if facs_panel else "page-columns transcription-only"

        page_divs.append(
            f'<div class="book-page" id="page-{idx}">'
            f'<div class="page-header"><span class="folio-label">Fol. {result.folio_label}</span>{entry_html}'
            f'<span class="page-info">{len(result.regions)} regions &middot; {len(result.entities)} entities</span>{lang_html}</div>'
            f'{map_html}{stats}'
            f'<div class="{cols}">{facs_panel}<div class="transcription-panel">{trans_panel_html}</div></div></div>')

    has_maps = any(r.locations for r in results)
    lh = '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">' if has_maps else ""
    lf = '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>' if has_maps else ""

    CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#faf8f4;--bg-warm:#f3efe6;--bg-card:#fff;--bg-panel:#fefcf7;
  --fg:#1f1a15;--fg-dim:#5c544b;--fg-faint:#948b80;
  --accent:#4a3520;--accent2:#7a6148;--accent-l:#c4a67a;
  --rule:#d9d3c7;--rule-l:#ebe6da;--rule-xl:#f4f0e6;
  --red:#b71c1c;--amber:#b07020;
  --radius:4px;--shadow:0 1px 2px rgba(60,40,20,.06);--shadow-m:0 3px 12px rgba(60,40,20,.08);
}
html{scroll-behavior:smooth}
body{font-family:'Source Serif 4','Noto Serif',Georgia,serif;background:var(--bg);color:var(--fg);font-size:15.5px;line-height:1.72;-webkit-font-smoothing:antialiased}

/* ── Top bar ── */
.top-bar{position:sticky;top:0;z-index:100;background:var(--accent);color:#fff}
.nav-inner{max-width:1500px;margin:0 auto;padding:.5rem 1.4rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.nav-title{font-family:'EB Garamond',serif;font-size:1.08rem;font-weight:500;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.95;letter-spacing:.01em}
.nav-subtitle{font-size:.7rem;opacity:.5;margin-left:.4rem;font-style:italic}
.nav-btn{width:1.8rem;height:1.8rem;border:1px solid rgba(255,255,255,.2);border-radius:var(--radius);background:rgba(255,255,255,.08);color:#fff;cursor:pointer;font-size:.8rem;display:inline-flex;align-items:center;justify-content:center;transition:background .12s}
.nav-btn:hover{background:rgba(255,255,255,.18)}
#page-select{padding:.25rem .6rem;font-size:.78rem;border:1px solid rgba(255,255,255,.2);border-radius:var(--radius);background:rgba(255,255,255,.08);color:#fff;cursor:pointer;max-width:320px}
#page-select option{color:#333;background:#fff}
.page-counter{font-size:.72rem;opacity:.6;white-space:nowrap}
.view-toggle{display:inline-flex;border-radius:var(--radius);overflow:hidden;border:1px solid rgba(255,255,255,.2)}
.view-toggle button{padding:.22rem .55rem;font-size:.64rem;font-weight:600;background:rgba(255,255,255,.06);color:rgba(255,255,255,.75);border:none;cursor:pointer;text-transform:uppercase;letter-spacing:.04em}
.view-toggle button.active{background:rgba(255,255,255,.2);color:#fff}

/* ── Legend ── */
.legend-panel{background:var(--bg-warm);border-bottom:1px solid var(--rule-l)}
.legend-inner{max-width:1500px;margin:0 auto;padding:.3rem 1.4rem;display:flex;flex-wrap:wrap;gap:.3rem;align-items:center}
.legend-label{font-size:.58rem;font-weight:700;color:var(--fg-faint);text-transform:uppercase;letter-spacing:.06em;margin-right:.25rem}
.chip{display:inline-flex;align-items:center;gap:.22rem;padding:.1rem .42rem;font-size:.64rem;border:1px solid var(--rule);border-radius:100px;background:var(--bg-card);color:var(--fg-dim);cursor:pointer;user-select:none;transition:border-color .12s}
.chip:hover{border-color:var(--accent2)}.chip.inactive{opacity:.2}
.chip-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}

/* ── Page container ── */
.book-page{display:none;max-width:1500px;margin:0 auto;padding:1.4rem 1.4rem 3rem}
.book-page.active{display:block}
.page-header{display:flex;align-items:baseline;gap:.9rem;flex-wrap:wrap;margin-bottom:1.1rem;padding-bottom:.55rem;border-bottom:2px solid var(--accent)}
.folio-label{font-family:'EB Garamond',serif;font-size:1.35rem;font-weight:600;color:var(--accent);letter-spacing:.015em}
.entry-nums{font-size:.78rem;color:var(--accent2);font-style:italic}
.page-info{font-size:.7rem;color:var(--fg-faint)}
.page-langs{font-size:.68rem;color:var(--fg-faint)}

/* ── Main layout: transcription panel slightly larger than half ── */
.page-columns{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:1.6rem;align-items:start}
.page-columns.transcription-only{grid-template-columns:1fr;max-width:860px;margin:0 auto}

/* ── Facsimile ── */
.facsimile-panel{position:sticky;top:52px;border:1px solid var(--rule);border-radius:var(--radius);overflow:auto;box-shadow:var(--shadow-m);background:#262422;max-height:calc(100vh - 70px)}
.facs-toolbar{display:flex;gap:.4rem;padding:.35rem .55rem;background:#2d2a27;border-bottom:1px solid #3d3935}
.facs-btn{padding:.18rem .5rem;font-size:.62rem;font-weight:600;background:rgba(255,255,255,.07);color:rgba(255,255,255,.7);border:1px solid rgba(255,255,255,.13);border-radius:3px;cursor:pointer;text-transform:uppercase;letter-spacing:.04em;transition:background .12s}
.facs-btn:hover{background:rgba(255,255,255,.14);color:#fff}
.facs-btn.active{background:rgba(255,255,255,.2);color:#fff;border-color:rgba(255,255,255,.3)}
.facs-image-wrap{position:relative;overflow:auto}
.facs-zoom-layer{position:relative;line-height:0;width:100%;transition:width .2s}
.facs-zoom-layer.zoomed{width:200%}
.facs-image-wrap img{display:block;width:100%;height:auto;cursor:zoom-in}
.facs-image-wrap img.zoomed{cursor:zoom-out}
.region-overlay{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;transition:opacity .25s}
.region-overlay.hidden{opacity:0;pointer-events:none}
.ov-box{position:absolute;border:2px solid var(--ov-color,#546e7a);background:rgba(0,0,0,.04);border-radius:2px;pointer-events:all;cursor:pointer;transition:background .15s,box-shadow .15s}
.ov-box:hover{background:rgba(0,0,0,.13);box-shadow:0 0 0 1px var(--ov-color)}
.ov-box.overlay-active{background:rgba(0,0,0,.2);border-width:3px;box-shadow:0 0 8px rgba(0,0,0,.3)}
.ov-label{position:absolute;top:-1px;left:-1px;display:inline-block;padding:1px 5px;font-size:9px;font-weight:700;font-family:system-ui,-apple-system,sans-serif;color:#fff;line-height:1.35;white-space:nowrap;text-transform:uppercase;letter-spacing:.03em;border-radius:1px 0 3px 0;pointer-events:none;opacity:.92}

/* ── Transcription panel (refined, scholarly) ── */
.transcription-panel{
  min-width:0;
  background:var(--bg-panel);
  border:1px solid var(--rule);
  border-radius:var(--radius);
  box-shadow:var(--shadow);
  padding:1.25rem 1.4rem 1.6rem;
}
.stats-row{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:1rem}
.stat-chip{display:inline-flex;align-items:center;gap:.2rem;padding:.1rem .4rem;font-size:.64rem;background:var(--bg-warm);border-radius:100px;color:var(--fg-dim)}
.stat-dot{width:5px;height:5px;border-radius:50%}
.map-wrap{display:none;height:280px;margin-bottom:1rem;border:1px solid var(--rule);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
.map-btn{display:inline-flex;align-items:center;gap:.3rem;padding:.3rem .6rem;font-size:.7rem;border:1px solid var(--rule);border-radius:var(--radius);background:var(--bg-card);color:var(--fg-dim);cursor:pointer;margin-bottom:.8rem}
.map-btn:hover{background:var(--bg-warm);border-color:var(--accent2)}

/* ── Region cards ── */
.region{position:relative;margin-bottom:.35rem;padding:.5rem .7rem .55rem;border-radius:var(--radius);border-left:3px solid transparent;transition:background .15s,border-color .15s,box-shadow .15s;cursor:default}
.region:hover,.region:focus{background:rgba(74,53,32,.035);outline:none}
.region.region-active{background:rgba(21,101,192,.055);border-left-color:#1565c0!important;box-shadow:inset 0 0 0 1px rgba(21,101,192,.1)}
.region-tag{display:inline-block;font-size:.5rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--tag-color,#546e7a);opacity:.5;margin-bottom:.15rem}
.region-meta{display:flex;align-items:center;gap:.3rem;margin-bottom:.15rem;flex-wrap:wrap}
.pos-note{font-size:.6rem;color:var(--fg-faint);font-style:italic}
.lang-badges{display:inline-flex;gap:.12rem}
.lang-badge{font-size:.48rem;font-weight:700;padding:.02rem .22rem;border-radius:2px;text-transform:uppercase;letter-spacing:.03em}
.lang-de{background:#e8eaf6;color:#3949ab}.lang-fr{background:#fce4ec;color:#c62828}.lang-la{background:#e8f5e9;color:#2e7d32}.lang-es{background:#fff8e1;color:#e65100}
.body-text{text-align:justify;hyphens:auto;-webkit-hyphens:auto}

/* ── Three-column layout with bbox-positioned margin notes ──
   The margin columns use position:relative so child anchors with
   position:absolute and top:<y>% resolve against the container height.
   Grid's default align-items:stretch makes the margin columns match the
   main column's natural height. */
.page-body{display:block}
.page-body--three-col{
  position:relative;
  display:grid;
  grid-template-columns:minmax(0,150px) minmax(0,1fr) minmax(0,150px);
  gap:1rem;
  align-items:stretch;
}
.margin-col{
  position:relative;
  font-size:.83rem;line-height:1.55;
  min-height:100%;
}
.margin-col--left{border-right:1px dashed var(--rule-l);padding-right:.55rem}
.margin-col--right{border-left:1px dashed var(--rule-l);padding-left:.55rem}
.margin-col .margin-anchor{
  position:absolute;
  left:0;right:0;
}
.margin-col--left .margin-anchor{padding-right:.55rem;right:.55rem}
.margin-col--right .margin-anchor{padding-left:.55rem;left:.55rem}
.margin-col .region{margin-bottom:0;padding:.35rem .5rem}
.margin-col .region--marginal_note .body-text{font-size:.82rem;font-style:italic;line-height:1.5}
.main-col{min-width:0}

/* ── Margin strips (top/bottom/opposite) ── */
.margin-strip{
  position:relative;
  padding:.55rem .75rem .55rem 5.5rem;
  background:var(--bg-warm);
  border:1px dashed var(--rule);
  border-radius:var(--radius);
  margin-bottom:.75rem;
  font-size:.85rem;
}
.margin-strip .strip-label{
  position:absolute;left:.55rem;top:.55rem;
  font-size:.55rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:var(--fg-faint);
}
.margin-strip--top{border-top-color:#b39ddb}
.margin-strip--bottom{margin-top:.75rem;margin-bottom:0;border-bottom-color:#b39ddb}
.margin-strip--opposite{opacity:.55;border-style:dotted;border-color:#c7c1b3;background:var(--bg);margin-top:.5rem}
.opposite-bleed{color:var(--fg-faint);font-style:italic;font-size:.8rem}

/* ── Region types ── */
.region--entry_heading{margin-top:1.3rem;padding-top:.75rem;border-top:1px solid var(--rule);border-left-color:var(--accent)!important}
.region--entry_heading:first-child{margin-top:0;padding-top:.5rem;border-top:none}
.entry-heading{font-family:'EB Garamond',serif;font-size:1.25rem;font-weight:600;line-height:1.3;color:var(--accent);letter-spacing:.01em}
.region--main_text{border-left-color:#90a4ae}
.region--marginal_note{border-left-color:#b39ddb}
.region--marginal_note .body-text{font-size:.88rem;font-style:italic;line-height:1.55}

/* Pasted slips – look like a physical slip of paper */
.region--pasted_slip,.region--pasted-slip{
  background:linear-gradient(135deg,#fffde7 92%,#fff4b8);
  border:1px solid #e6b840;border-left:3px solid #d49a1e;
  box-shadow:2px 3px 8px rgba(80,60,20,.12),0 0 0 1px rgba(230,184,64,.15);
  border-radius:2px 4px 4px 2px;margin:.7rem .2rem .7rem 0;padding:.55rem .7rem
}
.region--pasted_slip .body-text,.region--pasted-slip .body-text{font-size:.88rem}

/* Later additions – slightly different tone */
.region--later-addition{border-left-style:dashed}

.region--observation_table{overflow-x:auto;border-left-color:#00897b}
.obs-table{width:100%;border-collapse:collapse;font-size:.78rem;margin-top:.25rem;font-variant-numeric:tabular-nums}
.obs-table th,.obs-table td{border:1px solid var(--rule);padding:.25rem .5rem;text-align:right;font-family:'JetBrains Mono',monospace}
.obs-table th{background:var(--bg-warm);font-weight:600;font-size:.68rem;text-transform:uppercase;color:var(--fg-dim);text-align:center;letter-spacing:.03em}
.obs-table caption{caption-side:bottom;text-align:left;font-size:.66rem;color:var(--fg-faint);padding-top:.25rem;font-style:italic}

.region--calculation{border-left-color:#00897b}
.calc-body{font-family:'JetBrains Mono',monospace;font-size:.76rem;line-height:1.5;white-space:pre-wrap;color:#2a2522;background:var(--bg-warm);padding:.5rem .7rem;border-radius:3px;margin-top:.25rem}

.region--crossed_out{border-left-color:var(--red);opacity:.65}
.crossed-text{text-decoration:line-through;text-decoration-color:var(--red);color:var(--fg-dim);font-size:.9rem}
.repl-text{font-size:.7rem;color:#2e7d32;margin-top:.15rem;font-style:italic}

.region--bibliographic_ref{border-left-color:#5d4037}
.region--bibliographic_ref .body-text{font-size:.88rem}

.region--coordinates{border-left-color:#1565c0}
.coords-body{font-family:'JetBrains Mono',monospace;font-size:.82rem;color:#0d47a1;letter-spacing:.01em}

.region--instrument_list{border-left-color:#e65100}
.region--sketch{border-left-color:#6d4c41}
.sketch-desc{font-style:italic;color:var(--fg-dim);font-size:.88rem;line-height:1.55}

.region--page_number,.region--catch_phrase{text-align:center;padding:.2rem 0;border-left-color:transparent!important;color:var(--fg-faint);font-size:.82rem;letter-spacing:.04em}
.meta-body{font-size:.68rem;color:var(--fg-faint);letter-spacing:.03em}

/* ── Editorial apparatus ── */
.ed-note{font-size:.66rem;color:var(--fg-dim);margin-top:.2rem;display:flex;align-items:baseline;gap:.25rem;font-style:italic}
.ed-icon{font-size:.6rem;opacity:.5}
.unc{color:#b07020;font-weight:600;cursor:help}
.uncertain-word{background:rgba(176,112,32,.06);border-bottom:1px dashed #b07020}
.entity{background:color-mix(in srgb,var(--ent-color) 10%,transparent);border-bottom:2px solid var(--ent-color);border-radius:2px;padding:0 1px;cursor:help;color:inherit}
.entity:hover{background:color-mix(in srgb,var(--ent-color) 22%,transparent)}
.entity.hidden-type{background:transparent!important;border-bottom-color:transparent!important}
.inline-struck{text-decoration:line-through;text-decoration-color:var(--red);color:var(--fg-dim);opacity:.7;cursor:help}
.inline-underline{border-bottom:2px solid var(--accent);padding-bottom:1px;cursor:help}

/* ── Responsive ── */
@media(max-width:1200px){
  .page-body--three-col{grid-template-columns:minmax(0,120px) minmax(0,1fr) minmax(0,120px)}
}
@media(max-width:1000px){
  .page-columns{grid-template-columns:1fr}
  .facsimile-panel{position:relative;top:0;max-height:60vh}
  .transcription-panel{padding:1rem 1.1rem 1.3rem}
}
@media(max-width:720px){
  body{font-size:14.5px}
  .book-page{padding:.9rem}
  .page-body--three-col{grid-template-columns:1fr;position:static}
  .margin-col{position:static;border:none;border-top:1px dashed var(--rule-l);padding:.5rem 0;margin-top:.5rem}
  .margin-col .margin-anchor{position:static;padding:0!important;right:auto;left:auto}
  .margin-col .margin-anchor+.margin-anchor{margin-top:.35rem}
}
@media print{
  .top-bar,.legend-panel,.facs-toolbar{display:none}
  .book-page{display:block!important;page-break-after:always}
  .page-columns{grid-template-columns:1fr}
  .facsimile-panel{display:none}
  .transcription-panel{border:none;box-shadow:none;padding:0;background:transparent}
  .page-body--three-col{position:static}
  .margin-col{position:static}
  .margin-col .margin-anchor{position:static}
}
"""

    JS = """
(function(){
  var pages=document.querySelectorAll('.book-page'),sel=document.getElementById('page-select'),ctr=document.getElementById('page-counter');
  var cur=0;
  function show(i){if(i<0||i>=pages.length)return;pages[cur].classList.remove('active');cur=i;pages[cur].classList.add('active');sel.value=i;ctr.textContent=(i+1)+' / '+pages.length;window.scrollTo({top:0});}
  sel.addEventListener('change',function(){show(+sel.value);});
  document.getElementById('btn-prev').addEventListener('click',function(){show(cur-1);});
  document.getElementById('btn-next').addEventListener('click',function(){show(cur+1);});
  document.addEventListener('keydown',function(e){if(e.target.tagName==='SELECT'||e.target.tagName==='INPUT')return;if(e.key==='ArrowRight')show(cur+1);if(e.key==='ArrowLeft')show(cur-1);});

  var hiddenEnt=new Set();
  document.querySelectorAll('#ent-legend .chip').forEach(function(b){b.addEventListener('click',function(){var t=b.dataset.type;if(hiddenEnt.has(t)){hiddenEnt.delete(t);b.classList.remove('inactive');}else{hiddenEnt.add(t);b.classList.add('inactive');}document.querySelectorAll('.entity').forEach(function(el){el.classList.toggle('hidden-type',hiddenEnt.has(el.dataset.type));});});});

  var hiddenReg=new Set();
  document.querySelectorAll('#reg-legend .chip').forEach(function(b){b.addEventListener('click',function(){var t=b.dataset.type;if(hiddenReg.has(t)){hiddenReg.delete(t);b.classList.remove('inactive');}else{hiddenReg.add(t);b.classList.add('inactive');}document.querySelectorAll('.region').forEach(function(el){el.style.display=hiddenReg.has(el.dataset.regionType)?'none':'';});});});

  document.querySelectorAll('.view-toggle button').forEach(function(b){b.addEventListener('click',function(){var m=b.dataset.mode;document.querySelectorAll('.view-toggle button').forEach(function(x){x.classList.remove('active');});b.classList.add('active');document.querySelectorAll('.page-columns').forEach(function(c){var f=c.querySelector('.facsimile-panel');if(m==='dual'){c.classList.remove('transcription-only');if(f)f.style.display='';}else{c.classList.add('transcription-only');if(f)f.style.display='none';}});});});

  document.querySelectorAll('.facs-image-wrap img').forEach(function(img){img.addEventListener('click',function(){img.classList.toggle('zoomed');img.closest('.facs-zoom-layer').classList.toggle('zoomed');});});

  document.querySelectorAll('.facs-btn-overlay').forEach(function(b){b.addEventListener('click',function(){b.classList.toggle('active');var o=b.closest('.book-page').querySelector('.region-overlay');if(o)o.classList.toggle('hidden');});});

  function clearActive(pg){pg.querySelectorAll('.region.region-active').forEach(function(e){e.classList.remove('region-active');});pg.querySelectorAll('.ov-box.overlay-active').forEach(function(e){e.classList.remove('overlay-active');});}

  document.querySelectorAll('.region[data-region-idx]').forEach(function(el){el.addEventListener('click',function(ev){
    if(ev.target.closest('mark,a,button'))return;
    var pg=el.closest('.book-page'),idx=el.dataset.regionIdx;
    clearActive(pg); el.classList.add('region-active');
    var box=pg.querySelector('.ov-box[data-region-idx="'+idx+'"]');
    if(box){box.classList.add('overlay-active');var o=pg.querySelector('.region-overlay');if(o&&o.classList.contains('hidden')){o.classList.remove('hidden');var btn=pg.querySelector('.facs-btn-overlay');if(btn)btn.classList.add('active');}var fp=pg.querySelector('.facsimile-panel');if(fp)fp.scrollTop=box.offsetTop-fp.clientHeight/3;}
  });});

  document.querySelectorAll('.ov-box').forEach(function(box){box.addEventListener('click',function(){
    var pg=box.closest('.book-page'),idx=box.dataset.regionIdx;
    clearActive(pg); box.classList.add('overlay-active');
    var tr=pg.querySelector('.region[data-region-idx="'+idx+'"]');
    if(tr){tr.classList.add('region-active');tr.scrollIntoView({behavior:'smooth',block:'center'});}
  });});

  document.querySelectorAll('[data-toggle]').forEach(function(b){b.addEventListener('click',function(){var t=document.getElementById(b.dataset.toggle);if(!t)return;var s=t.style.display==='none'||!t.style.display;t.style.display=s?'block':'none';if(s&&t.classList.contains('map-wrap')&&!t.dataset.init){t.dataset.init='1';var d=JSON.parse(t.dataset.locations);var m=L.map(t).setView(d.center,6);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',{attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',subdomains:'abcd',maxZoom:19}).addTo(m);
    d.locations.forEach(function(l){L.marker([l.lat,l.lon]).addTo(m).bindPopup('<b>'+l.name+'</b><br>'+l.display);});setTimeout(function(){m.invalidateSize();},120);}});});

  show(0);
})();
"""

    final = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{html_lib.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400..800;1,400..800&family=Source+Serif+4:ital,opsz,wght@0,8..60,300..900;1,8..60,300..900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
{lh}<style>{CSS}</style>
</head>
<body>
<div class="top-bar"><div class="nav-inner">
<span class="nav-title">{html_lib.escape(title)}<span class="nav-subtitle">{html_lib.escape(subtitle)}</span></span>
<div class="view-toggle"><button data-mode="dual" class="active">Facsimile + Text</button><button data-mode="text">Text Only</button></div>
<button class="nav-btn" id="btn-prev" title="Previous page">&#9664;</button>
<select id="page-select">{options}</select>
<button class="nav-btn" id="btn-next" title="Next page">&#9654;</button>
<span class="page-counter" id="page-counter">1 / {len(results)}</span>
</div></div>
<div class="legend-panel" id="ent-legend"><div class="legend-inner"><span class="legend-label">Entities</span>{ent_chips}</div></div>
<div class="legend-panel" id="reg-legend"><div class="legend-inner"><span class="legend-label">Regions</span>{reg_chips}</div></div>
{"".join(page_divs)}
{lf}<script>{JS}</script>
</body></html>"""

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(final)
    size_mb = output_path.stat().st_size / 1e6
    logger.info("HTML edition written to %s (%.1f MB)", output_path, size_mb)
    return output_path

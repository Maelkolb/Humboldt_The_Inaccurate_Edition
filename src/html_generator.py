"""
HTML Generator – Humboldt Journal Digital Edition

Integrates the updated code-cell version with:
- SVG overlay of region bounding boxes on facsimile (toggleable)
- Click text region → highlight on facsimile (and vice versa)
- Inline editorial markup: ~~strikethrough~~ and <u>underline</u>
- Entity highlighting, maps, responsive design
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
    # The <u> tags were escaped to &lt;u&gt; by html_lib.escape, so match that
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
    if getattr(region, "is_usage_marked", False) or rtype == "usage_mark":
        extra_cls += " region--usage-marked"
    if getattr(region, "is_pasted_slip", False) or rtype == "pasted_slip":
        extra_cls += " region--pasted-slip"
    if getattr(region, "writing_layer", None) == "later_addition":
        extra_cls += " region--later-addition"

    wo = (f'<div class="region region--{rtype}{extra_cls}" data-region-type="{rtype}" '
          f'data-region-idx="{ridx}" data-page-idx="{page_idx}" tabindex="0">')
    wc = '</div>'
    hd = f'{tag}{meta}'

    if region.is_visual or rtype == "sketch":
        return f'{wo}{hd}<p class="sketch-desc">{html_lib.escape(region.content)}</p>{note}{wc}'
    if rtype == "entry_heading":
        a = _annotate_text(region.content, entities, ec)
        return f'{wo}{hd}<h2 class="entry-heading">{a}</h2>{note}{wc}'
    if rtype == "observation_table" and region.table_data:
        return f'{wo}{hd}{_render_table_html(region.table_data)}{note}{wc}'
    if rtype == "calculation":
        return f'{wo}{hd}<pre class="calc-body">{html_lib.escape(region.content)}</pre>{note}{wc}'
    if rtype == "crossed_out":
        a = _annotate_text(region.content, entities, ec)
        repl = (f'<div class="repl-text">Replaced by: {html_lib.escape(region.crossed_out_text)}</div>') if region.crossed_out_text else ""
        return f'{wo}{hd}<p class="crossed-text">{a}</p>{repl}{note}{wc}'
    if rtype == "usage_mark":
        # Usage marks: render the underlying text normally with a diagonal overlay
        a = _annotate_text(region.content, entities, ec)
        return f'{wo}{hd}<p class="body-text usage-marked-text">{a}</p>{note}{wc}'
    if rtype == "coordinates":
        return f'{wo}{hd}<p class="coords-body">{html_lib.escape(region.content)}</p>{note}{wc}'
    if rtype == "instrument_list" and region.table_data:
        return f'{wo}{hd}{_render_table_html(region.table_data)}{note}{wc}'
    if rtype in ("page_number", "catch_phrase"):
        return f'{wo}{tag}<span class="meta-body">{html_lib.escape(region.content)}</span>{wc}'
    a = _annotate_text(region.content, entities, ec)
    return f'{wo}{hd}<p class="body-text">{a}</p>{note}{wc}'


def _build_transcription_panel(regions, entities, ec, rc, rl, page_idx):
    """
    Build the transcription panel HTML, using a 3-column layout when there are
    left or right marginal notes:

        [left margin] [main body] [right margin]

    Top and bottom margin notes appear above/below the 3-column block.
    Pasted slips are rendered inside the main body at their detected position.
    """
    # Categorise by marginal position
    def _mp(r):
        return getattr(r, "marginal_position", None)

    left_notes  = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "left"]
    right_notes = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "right"]
    top_notes   = [r for r in regions if r.region_type == "marginal_note" and _mp(r) == "mTop"]
    bot_notes   = [r for r in regions if r.region_type == "marginal_note"
                   and _mp(r) in ("mBottom", "opposite")]
    # Everything else goes in the main column
    main_regions = [r for r in regions
                    if r not in left_notes and r not in right_notes
                    and r not in top_notes and r not in bot_notes]

    def _html(rs):
        return "".join(_render_region(r, entities, ec, rc, rl, page_idx) for r in rs)

    # Top notes strip
    top_html = (f'<div class="margin-strip margin-strip--top">{_html(top_notes)}</div>'
                if top_notes else "")

    # Bottom/opposite notes strip
    bot_html = (f'<div class="margin-strip margin-strip--bottom">{_html(bot_notes)}</div>'
                if bot_notes else "")

    # 3-column body or single-column if no left/right margins
    if left_notes or right_notes:
        body_html = (
            f'<div class="page-body page-body--three-col">'
            f'<div class="margin-col margin-col--left">{_html(left_notes)}</div>'
            f'<div class="main-col">{_html(main_regions)}</div>'
            f'<div class="margin-col margin-col--right">{_html(right_notes)}</div>'
            f'</div>'
        )
    else:
        body_html = f'<div class="page-body">{_html(main_regions)}</div>'

    return top_html + body_html + bot_html


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
                # FIX 3: downscale + recompress before embedding so the HTML
                # file stays small. _resize_image_for_embed caps width at
                # EMBED_IMAGE_MAX_WIDTH and re-saves at EMBED_IMAGE_QUALITY.
                b64 = _resize_image_for_embed(ip)
                facs_img = f'<img src="data:image/jpeg;base64,{b64}" alt="Fol. {result.folio_label}">'

        overlay = _build_overlay(result.regions, rc, rl)
        facs_panel = ""
        if facs_img:
            facs_panel = (
                '<div class="facsimile-panel">'
                '<div class="facs-toolbar"><button class="facs-btn facs-btn-overlay">Regions</button></div>'
                f'<div class="facs-image-wrap">{facs_img}{overlay}</div></div>')

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
:root{--bg:#fafaf8;--bg-warm:#f4f2ed;--bg-card:#fff;--fg:#222;--fg-dim:#666;--fg-faint:#999;--accent:#4a3520;--accent2:#7a6148;--accent-l:#c4a67a;--border:#e0ddd7;--border-l:#edebe6;--red:#c62828;--radius:5px;--shadow:0 1px 3px rgba(0,0,0,.05)}
html{scroll-behavior:smooth}
body{font-family:'Source Serif 4','Noto Serif',Georgia,serif;background:var(--bg);color:var(--fg);font-size:15.5px;line-height:1.72;-webkit-font-smoothing:antialiased}
.top-bar{position:sticky;top:0;z-index:100;background:var(--accent);color:#fff}
.nav-inner{max-width:1440px;margin:0 auto;padding:.5rem 1.25rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.nav-title{font-family:'EB Garamond',serif;font-size:1.05rem;font-weight:500;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.95}
.nav-subtitle{font-size:.7rem;opacity:.5;margin-left:.4rem;font-style:italic}
.nav-btn{width:1.8rem;height:1.8rem;border:1px solid rgba(255,255,255,.2);border-radius:var(--radius);background:rgba(255,255,255,.08);color:#fff;cursor:pointer;font-size:.8rem;display:inline-flex;align-items:center;justify-content:center}
.nav-btn:hover{background:rgba(255,255,255,.18)}
#page-select{padding:.25rem .6rem;font-size:.78rem;border:1px solid rgba(255,255,255,.2);border-radius:var(--radius);background:rgba(255,255,255,.08);color:#fff;cursor:pointer;max-width:320px}
#page-select option{color:#333;background:#fff}
.page-counter{font-size:.72rem;opacity:.6;white-space:nowrap}
.view-toggle{display:inline-flex;border-radius:var(--radius);overflow:hidden;border:1px solid rgba(255,255,255,.2)}
.view-toggle button{padding:.22rem .55rem;font-size:.64rem;font-weight:600;background:rgba(255,255,255,.06);color:rgba(255,255,255,.75);border:none;cursor:pointer;text-transform:uppercase;letter-spacing:.04em}
.view-toggle button.active{background:rgba(255,255,255,.2);color:#fff}
.legend-panel{background:var(--bg-warm);border-bottom:1px solid var(--border-l)}
.legend-inner{max-width:1440px;margin:0 auto;padding:.28rem 1.25rem;display:flex;flex-wrap:wrap;gap:.28rem;align-items:center}
.legend-label{font-size:.58rem;font-weight:700;color:var(--fg-faint);text-transform:uppercase;letter-spacing:.06em;margin-right:.2rem}
.chip{display:inline-flex;align-items:center;gap:.22rem;padding:.1rem .42rem;font-size:.64rem;border:1px solid var(--border);border-radius:100px;background:var(--bg-card);color:var(--fg-dim);cursor:pointer;user-select:none}
.chip:hover{border-color:var(--accent2)}.chip.inactive{opacity:.18}
.chip-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.book-page{display:none;max-width:1440px;margin:0 auto;padding:1.25rem}
.book-page.active{display:block}
.page-header{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap;margin-bottom:1rem;padding-bottom:.5rem;border-bottom:2px solid var(--accent)}
.folio-label{font-family:'EB Garamond',serif;font-size:1.3rem;font-weight:600;color:var(--accent)}
.entry-nums{font-size:.78rem;color:var(--accent2);font-style:italic}
.page-info{font-size:.7rem;color:var(--fg-faint)}
.page-langs{font-size:.68rem;color:var(--fg-faint)}
/* FIX 2: give the transcription column significantly more space than the
   facsimile (3fr vs 2fr). The old rule was 1fr 1fr (equal split). */
.page-columns{display:grid;grid-template-columns:2fr 3fr;gap:1.25rem;align-items:start}
.page-columns.transcription-only{grid-template-columns:1fr;max-width:780px}
.facsimile-panel{position:sticky;top:52px;border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);background:#2a2a2a;max-height:calc(100vh - 70px);overflow-y:auto}
.facs-toolbar{display:flex;gap:.4rem;padding:.3rem .5rem;background:#333;border-bottom:1px solid #444}
.facs-btn{padding:.18rem .5rem;font-size:.62rem;font-weight:600;background:rgba(255,255,255,.07);color:rgba(255,255,255,.65);border:1px solid rgba(255,255,255,.13);border-radius:3px;cursor:pointer;text-transform:uppercase;letter-spacing:.04em}
.facs-btn:hover{background:rgba(255,255,255,.14);color:#fff}
.facs-btn.active{background:rgba(255,255,255,.2);color:#fff;border-color:rgba(255,255,255,.3)}
.facs-image-wrap{position:relative;line-height:0}
.facs-image-wrap img{display:block;width:100%;height:auto;cursor:zoom-in}
.facs-image-wrap img.zoomed{width:200%;cursor:zoom-out}
.region-overlay{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;transition:opacity .25s}
.region-overlay.hidden{opacity:0;pointer-events:none}
.ov-box{position:absolute;border:2px solid var(--ov-color,#546e7a);background:rgba(0,0,0,.05);border-radius:2px;pointer-events:all;cursor:pointer;transition:background .15s,box-shadow .15s}
.ov-box:hover{background:rgba(0,0,0,.13);box-shadow:0 0 0 1px var(--ov-color)}
.ov-box.overlay-active{background:rgba(0,0,0,.2);border-width:3px;box-shadow:0 0 8px rgba(0,0,0,.3)}
.ov-label{position:absolute;top:-1px;left:-1px;display:inline-block;padding:1px 5px;font-size:9px;font-weight:700;font-family:system-ui,-apple-system,sans-serif;color:#fff;line-height:1.35;white-space:nowrap;text-transform:uppercase;letter-spacing:.03em;border-radius:1px 0 3px 0;pointer-events:none;opacity:.92}
.transcription-panel{min-width:0}
.stats-row{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:1rem}
.stat-chip{display:inline-flex;align-items:center;gap:.2rem;padding:.1rem .4rem;font-size:.64rem;background:var(--bg-warm);border-radius:100px;color:var(--fg-dim)}
.stat-dot{width:5px;height:5px;border-radius:50%}
.map-wrap{display:none;height:280px;margin-bottom:1rem;border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
.map-btn{display:inline-flex;align-items:center;gap:.3rem;padding:.3rem .6rem;font-size:.7rem;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-card);color:var(--fg-dim);cursor:pointer;margin-bottom:.8rem}
.map-btn:hover{background:var(--bg-warm);border-color:var(--accent2)}
.region{position:relative;margin-bottom:.1rem;padding:.45rem .6rem;border-radius:var(--radius);border-left:3px solid transparent;transition:background .15s,border-color .15s;cursor:default}
.region:hover,.region:focus{background:rgba(74,53,32,.04)}
.region.region-active{background:rgba(21,101,192,.06);border-left-color:#1565c0!important;box-shadow:inset 0 0 0 1px rgba(21,101,192,.1)}
.region-tag{display:inline-block;font-size:.5rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--tag-color,#546e7a);opacity:.45;margin-bottom:.1rem}
.region-meta{display:flex;align-items:center;gap:.3rem;margin-bottom:.12rem}
.pos-note{font-size:.6rem;color:var(--fg-faint);font-style:italic}
.lang-badges{display:inline-flex;gap:.12rem}
.lang-badge{font-size:.48rem;font-weight:700;padding:.02rem .22rem;border-radius:2px;text-transform:uppercase;letter-spacing:.03em}
.lang-de{background:#e8eaf6;color:#3949ab}.lang-fr{background:#fce4ec;color:#c62828}.lang-la{background:#e8f5e9;color:#2e7d32}.lang-es{background:#fff8e1;color:#e65100}
.body-text{text-align:justify;hyphens:auto;-webkit-hyphens:auto}
/* ── Marginal layout ── */
.page-body{display:block}
/* FIX 2 (continued): narrow the margin columns from 180px to 120px so the
   main text body gets the bulk of the transcription panel width. */
.page-body--three-col{display:grid;grid-template-columns:minmax(0,120px) 1fr minmax(0,120px);gap:.75rem;align-items:start}
.margin-col{font-size:.82rem;line-height:1.55}
.margin-col--left{border-right:1px dashed var(--border-l);padding-right:.5rem}
.margin-col--right{border-left:1px dashed var(--border-l);padding-left:.5rem}
.main-col{min-width:0}
.margin-strip{padding:.4rem .6rem;background:var(--bg-warm);border:1px dashed var(--border);border-radius:var(--radius);margin-bottom:.5rem;font-size:.82rem}
.margin-strip--top{border-top-color:#b39ddb}.margin-strip--bottom{border-bottom-color:#b39ddb}
/* ── Region types ── */
.region--entry_heading{margin-top:1.2rem;padding-top:.6rem;border-top:1px solid var(--border);border-left-color:var(--accent)!important}
.entry-heading{font-family:'EB Garamond',serif;font-size:1.2rem;font-weight:600;line-height:1.3;color:var(--accent)}
.region--main_text{border-left-color:#90a4ae}
.region--marginal_note{border-left-color:#b39ddb}
.region--marginal_note .body-text{font-size:.84rem;font-style:italic;line-height:1.5}
/* Pasted slips – look like a physical slip of paper */
.region--pasted_slip,.region--pasted-slip{
  background:linear-gradient(135deg,#fffde7 90%,#fff9c4);
  border:1px solid #f9a825;border-left:3px solid #f57f17;
  box-shadow:2px 3px 8px rgba(0,0,0,.12),0 0 0 1px rgba(249,168,37,.15);
  border-radius:2px 4px 4px 2px;margin:.6rem .2rem .6rem 0;padding:.5rem .65rem}
.region--pasted_slip .body-text,.region--pasted-slip .body-text{font-size:.86rem}
/* Usage marks – legible text with diagonal-line overlay */
.region--usage_mark,.region--usage-marked{border-left-color:#c62828;opacity:.75;position:relative}
.region--usage_mark::after,.region--usage-marked::after{
  content:'';position:absolute;inset:0;pointer-events:none;
  background:repeating-linear-gradient(-55deg,transparent,transparent 12px,rgba(198,40,40,.07) 12px,rgba(198,40,40,.07) 13px);
  border-radius:inherit}
.usage-marked-text{color:var(--fg-dim)}
/* Later additions – slightly different tone */
.region--later-addition{border-left-style:dashed}
.region--observation_table{overflow-x:auto;border-left-color:#00897b}
.obs-table{width:100%;border-collapse:collapse;font-size:.78rem;margin-top:.2rem;font-variant-numeric:tabular-nums}
.obs-table th,.obs-table td{border:1px solid var(--border);padding:.22rem .45rem;text-align:right;font-family:'JetBrains Mono',monospace}
.obs-table th{background:var(--bg-warm);font-weight:600;font-size:.68rem;text-transform:uppercase;color:var(--fg-dim);text-align:center}
.obs-table caption{caption-side:bottom;text-align:left;font-size:.66rem;color:var(--fg-faint);padding-top:.2rem;font-style:italic}
.region--calculation{border-left-color:#00897b}
.calc-body{font-family:'JetBrains Mono',monospace;font-size:.76rem;line-height:1.45;white-space:pre-wrap;color:#333}
.region--crossed_out{border-left-color:var(--red);opacity:.6}
.crossed-text{text-decoration:line-through;text-decoration-color:var(--red);color:var(--fg-dim);font-size:.88rem}
.repl-text{font-size:.7rem;color:#2e7d32;margin-top:.1rem;font-style:italic}
.region--interlinear{border-left-color:#f9a825}
.region--interlinear .body-text{font-size:.88rem}
.region--bibliographic_ref{border-left-color:#5d4037}
.region--bibliographic_ref .body-text{font-size:.88rem}
.region--coordinates{border-left-color:#1565c0}
.coords-body{font-family:'JetBrains Mono',monospace;font-size:.8rem;color:#0d47a1}
.region--instrument_list{border-left-color:#e65100}
.region--sketch{border-left-color:#6d4c41}
.sketch-desc{font-style:italic;color:var(--fg-dim);font-size:.86rem;line-height:1.5}
.region--page_number,.region--catch_phrase{text-align:center;padding:.15rem 0;border-left-color:transparent!important}
.meta-body{font-size:.68rem;color:var(--fg-faint);letter-spacing:.03em}
.ed-note{font-size:.66rem;color:var(--fg-dim);margin-top:.15rem;display:flex;align-items:baseline;gap:.2rem}
.ed-icon{font-size:.6rem;opacity:.45}
.unc{color:#e65100;font-weight:600;cursor:help}
.uncertain-word{background:rgba(230,81,0,.05);border-bottom:1px dashed #e65100}
.entity{background:color-mix(in srgb,var(--ent-color) 10%,transparent);border-bottom:2px solid var(--ent-color);border-radius:2px;padding:0 1px;cursor:help;color:inherit}
.entity:hover{background:color-mix(in srgb,var(--ent-color) 22%,transparent)}
.entity.hidden-type{background:transparent!important;border-bottom-color:transparent!important}
/* Inline editorial markup */
.inline-struck{text-decoration:line-through;text-decoration-color:var(--red);color:var(--fg-dim);opacity:.65;cursor:help}
.inline-underline{border-bottom:2px solid var(--accent);padding-bottom:1px;cursor:help}
@media(max-width:1100px){.page-body--three-col{grid-template-columns:minmax(0,90px) 1fr minmax(0,90px)}}
@media(max-width:900px){.page-columns{grid-template-columns:1fr}.facsimile-panel{position:relative;top:0;max-height:50vh}body{font-size:14.5px}.book-page{padding:.8rem}.page-body--three-col{grid-template-columns:1fr}.margin-col{border:none;border-top:1px dashed var(--border-l);padding:.3rem 0;margin-top:.3rem}}
@media print{.top-bar,.legend-panel,.facs-toolbar{display:none}.book-page{display:block!important;page-break-after:always}.page-columns{grid-template-columns:1fr}.facsimile-panel{display:none}}
"""

    # FIX 1: switch map tiles from openstreetmap.org (blocks requests with a
    # Referer header → 403) to CARTO Voyager, which is free, requires no API
    # key, and does not block embedded HTML consumers.
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

  document.querySelectorAll('.facs-image-wrap img').forEach(function(img){img.addEventListener('click',function(){img.classList.toggle('zoomed');});});

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
    /* FIX 1: CARTO Voyager tiles — no Referer block, free, no API key needed */
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

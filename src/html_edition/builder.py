"""Assemble the static digital-edition bundle (HTML + CSS + JS + TEI + facsimiles).

The edition is emitted as a self-contained directory:

    <bundle>/
        index.html
        assets/edition.css
        assets/edition.js
        tei/folio_<label>.tei.xml      (one per page)
        tei/digital_edition.tei.xml     (full book, if available)
        facsimiles/<image>              (copied unless embedding / external prefix)

and, by default, packaged into a sibling ``.zip``.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from ..imaging import embed_jpeg_base64, measure_aspect
from ..models import PageResult
from ..tei_writer import page_result_to_tei_document
from .icons import icon
from .markup import render_plain
from .render import (
    DEFAULT_PAGE_ASPECT,
    LANG_NAMES,
    ENTITY_FALLBACK_COLOR,
    REGION_FALLBACK_COLOR,
    build_doc_panel,
    build_overlay,
    build_reading_panel,
    plain_text_from_regions,
)
from .textcmp import cer_wer_vs_gt

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_RT_ORDER = [
    "entry_heading", "main_text", "marginal_note", "pasted_slip",
    "calculation", "observation_table", "instrument_list",
    "sketch", "crossed_out",
    "page_number",
]


# ---------------------------------------------------------------------------
# Shared chrome (legend / page-select / table of contents)
# ---------------------------------------------------------------------------
def _legend_chips(results, ec, el, rc, rl) -> str:
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
    sorted_rt = sorted(
        used_rt,
        key=lambda t: (_RT_ORDER.index(t) if t in _RT_ORDER else 999, t),
    )
    reg_chips = "".join(
        f'<button type="button" class="chip chip--reg" data-type="{rt}" '
        f'data-scope="region" '
        f'title="Toggle visibility of {html_lib.escape(rl.get(rt, rt))}">'
        f'<span class="chip-swatch" '
        f'style="background:{rc.get(rt, REGION_FALLBACK_COLOR)};"></span>'
        f'<span class="chip-label">'
        f'{html_lib.escape(rl.get(rt, rt.replace("_", " ").title()))}'
        f'</span></button>'
        for rt in sorted_rt
    )
    return (
        '<div class="legend"><div class="legend-inner">'
        '<span class="legend-group">'
        '<span class="legend-heading">Entities</span>'
        f'<span class="legend-chips">{ent_chips}</span></span>'
        '<span class="legend-rule" aria-hidden="true"></span>'
        '<span class="legend-group">'
        '<span class="legend-heading">Regions</span>'
        f'<span class="legend-chips">{reg_chips}</span></span>'
        '</div></div>'
    )


def _page_options(results) -> str:
    return "".join(
        f'<option value="{i}">Fol. {html_lib.escape(r.folio_label)}'
        f'{" — Entries " + html_lib.escape(", ".join(r.entry_numbers)) if r.entry_numbers else ""}'
        f'</option>'
        for i, r in enumerate(results)
    )


def _toc(results) -> str:
    items = []
    for i, r in enumerate(results):
        label = f"Fol. {r.folio_label}"
        sub = f" · Entries {', '.join(r.entry_numbers)}" if r.entry_numbers else ""
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
            f'<span class="toc-preview">{render_plain(preview)}</span>'
            if preview else ""
        )
        items.append(
            f'<button type="button" class="toc-item" data-jump="{i}">'
            f'<span class="toc-num">{i+1:02d}</span>'
            f'<span class="toc-main">'
            f'<span class="toc-label">'
            f'{html_lib.escape(label)}{html_lib.escape(sub)}</span>'
            f'{preview_html}'
            f'</span></button>'
        )
    return "".join(items)


# ---------------------------------------------------------------------------
# Facsimile resolution
# ---------------------------------------------------------------------------
def _facs_img_tag(result, *, page_aspect, mode, ref_prefix):
    """Return (img_html, page_aspect) for one page given the chosen image mode."""
    fname = result.image_filename
    alt = f'alt="Fol. {html_lib.escape(result.folio_label)}"'
    if mode == "external" and ref_prefix is not None:
        pfx = (ref_prefix.rstrip("/") + "/") if ref_prefix else ""
        return (
            f'<img src="{pfx}{html_lib.escape(fname)}" {alt} loading="lazy" '
            f'class="facs-img" draggable="false">',
            page_aspect,
        )
    if mode == "copy":
        return (
            f'<img src="facsimiles/{html_lib.escape(fname)}" {alt} '
            f'loading="lazy" class="facs-img" draggable="false">',
            page_aspect,
        )
    if mode == "embed":
        # caller supplies aspect; src filled in by caller (needs bytes)
        return "", page_aspect
    return "", page_aspect


def _facs_panel(facs_img: str, page_aspect: float, overlay: str) -> str:
    if not facs_img:
        return ""
    return (
        '<div class="facs-panel">'
        '  <div class="facs-toolbar">'
        '    <button type="button" class="facs-tool facs-tool--overlay" '
        '            title="Toggle region overlay (B)">'
        f'      {icon("boxes", 13)}<span>Regions</span></button>'
        '    <div class="facs-toolbar-spacer"></div>'
        '    <button type="button" class="facs-tool facs-tool--zout" '
        '            title="Zoom out" aria-label="Zoom out">'
        f'      {icon("zoom-out", 13)}</button>'
        '    <span class="facs-zoom-readout" data-readout="zoom">100%</span>'
        '    <button type="button" class="facs-tool facs-tool--zin" '
        '            title="Zoom in" aria-label="Zoom in">'
        f'      {icon("zoom-in", 13)}</button>'
        '    <button type="button" class="facs-tool facs-tool--zreset" '
        '            title="Fit to frame">'
        f'      {icon("fit", 13)}</button>'
        '    <button type="button" class="facs-tool facs-tool--fullscreen" '
        '            title="Toggle fullscreen" aria-label="Toggle fullscreen">'
        f'      {icon("fullscreen", 13)}</button>'
        '  </div>'
        '  <div class="facs-stage">'
        '    <div class="facs-frame" data-zoom="1">'
        f'      <div class="facs-canvas" style="--page-aspect:{page_aspect:.4f};">'
        f'{facs_img}{overlay}</div>'
        '    </div>'
        '    <div class="facs-hint">'
        '      <span>Drag · wheel · double-click to fit</span>'
        '    </div>'
        '  </div>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# One page <article>
# ---------------------------------------------------------------------------
def _render_page_article(
    idx, result, ec, el, rc, rl, *, image_mode, image_folder, ref_prefix, tei_dir
):
    page_aspect = DEFAULT_PAGE_ASPECT
    facs_img = ""

    if image_mode == "embed" and image_folder is not None:
        ip = Path(image_folder) / result.image_filename
        if ip.exists():
            b64, page_aspect = embed_jpeg_base64(ip)
            facs_img = (
                f'<img src="data:image/jpeg;base64,{b64}" '
                f'alt="Fol. {html_lib.escape(result.folio_label)}" '
                f'class="facs-img" draggable="false">'
            )
    elif image_mode == "copy" and image_folder is not None:
        ip = Path(image_folder) / result.image_filename
        if ip.exists():
            page_aspect = measure_aspect(ip)
            facs_img, page_aspect = _facs_img_tag(
                result, page_aspect=page_aspect, mode="copy", ref_prefix=None
            )
    elif image_mode == "external":
        facs_img, page_aspect = _facs_img_tag(
            result, page_aspect=page_aspect, mode="external", ref_prefix=ref_prefix
        )

    overlay = build_overlay(result.regions, rc, rl)
    facs_panel = _facs_panel(facs_img, page_aspect, overlay)

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
            f'{icon("map", 13)}<span>Map</span>'
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
            f'style="--pill-color:{ec.get(t, ENTITY_FALLBACK_COLOR)};">'
            f'<span class="stat-dot"></span>'
            f'<span class="stat-label">{html_lib.escape(el.get(t, t))}</span>'
            f'<span class="stat-num">{c}</span></span>'
            for t, c in sorted(counts.items(), key=lambda x: -x[1])
        ) + '</div>'

    entry_html = (
        f'<span class="meta-entries">Entries '
        f'{html_lib.escape(", ".join(result.entry_numbers))}</span>'
        if result.entry_numbers else ""
    )
    lang_html = (
        '<span class="meta-langs">'
        + " · ".join(html_lib.escape(LANG_NAMES.get(l, l))
                     for l in result.page_languages)
        + '</span>'
        if result.page_languages else ""
    )

    # ── Per-page TEI written as a real file; control is a download anchor ──
    plain_text = plain_text_from_regions(result.regions)
    plain_text_attr = html_lib.escape(plain_text, quote=True)

    tei_btn = ""
    try:
        page_tei_xml = page_result_to_tei_document(result)
        tei_name = f"folio_{result.folio_label or result.page_number}.tei.xml"
        tei_name = tei_name.replace("/", "_").replace(" ", "_")
        (tei_dir / tei_name).write_text(page_tei_xml, encoding="utf-8")
        tei_btn = (
            f'<a class="tool-btn tool-btn--tei" href="tei/{html_lib.escape(tei_name)}" '
            f'download title="Download this page as TEI XML">'
            f'{icon("tei", 13)}<span class="tool-label">TEI</span></a>'
        )
    except Exception as exc:
        logger.warning("Could not build per-page TEI for folio %s: %s",
                       result.folio_label, exc)

    has_gt = result.has_ground_truth
    gt_toggle = ""
    if has_gt:
        gt_toggle = (
            '  <div class="source-toggle" role="tablist" '
            '       aria-label="Transcription source">'
            '    <button type="button" data-source-mode="gemini" '
            '            class="active" role="tab" '
            '            title="Gemini transcription (S)">Gemini</button>'
            '    <button type="button" data-source-mode="gt" role="tab" '
            '            title="Ground-truth transcription (S)">'
            '      Ground Truth</button>'
            '    <button type="button" data-source-mode="diff" role="tab" '
            '            title="Diff: Gemini vs. Ground Truth (S)">'
            '      Diff</button>'
            '  </div>'
        )

    tools_html = (
        '<div class="page-tools">'
        '  <div class="trans-toggle" role="tablist" '
        '       aria-label="Transcription mode">'
        '    <button type="button" data-trans-mode="document" '
        '            class="active" role="tab" '
        '            title="Document view — mirrors page layout (R)">'
        f'      {icon("document", 13)}<span>Document</span></button>'
        '    <button type="button" data-trans-mode="reading" role="tab" '
        '            title="Linear reading flow (R)">'
        f'      {icon("reading", 13)}<span>Reading</span></button>'
        '  </div>'
        f'  {gt_toggle}'
        '  <div class="page-tools-spacer"></div>'
        f'  {map_html}'
        f'  {tei_btn}'
        '  <button type="button" class="tool-btn tool-btn--copy" '
        f'          data-copy="{plain_text_attr}" '
        f'          title="Copy plain text of this page">'
        f'    {icon("copy", 13)}<span class="tool-label">Copy</span></button>'
        '  <div class="search-wrap">'
        f'    {icon("search", 13)}'
        '    <input type="search" class="search-input" '
        '           placeholder="Search this page…" '
        '           aria-label="Search in this page">'
        '  </div>'
        '</div>'
    )

    doc_html = build_doc_panel(
        result.regions, result.entities, ec, rc, rl, idx, page_aspect
    )
    reading_html = build_reading_panel(
        result.regions, result.entities, ec, rc, rl, idx
    )

    metrics_html = ""
    if has_gt:
        cw = cer_wer_vs_gt(result.regions)
        if cw is not None:
            dropped = (
                '<span class="dm-pill dm-pill--muted">'
                '<span class="dm-k">unaligned dropped</span>'
                f'<span class="dm-v">{cw["n_dropped"]}</span></span>'
                if cw["n_dropped"] else ""
            )
            metrics_html = (
                '<div class="diff-metrics" role="status" aria-live="polite">'
                '<span class="diff-metrics-title">'
                'Accuracy vs. ground truth (all matched regions)</span>'
                '<span class="dm-pill"><span class="dm-k">CER</span>'
                f'<span class="dm-v">{cw["cer"] * 100:.1f}%</span></span>'
                '<span class="dm-pill"><span class="dm-k">WER</span>'
                f'<span class="dm-v">{cw["wer"] * 100:.1f}%</span></span>'
                '<span class="dm-pill dm-pill--muted">'
                '<span class="dm-k">regions</span>'
                f'<span class="dm-v">{cw["n_regions"]}</span></span>'
                f'{dropped}'
                '</div>'
            )

    cols_cls = "page-cols" if facs_panel else "page-cols is-text-only"
    article_attrs = (
        f'id="page-{idx}" data-page-idx="{idx}" '
        f'data-source-mode="gemini" '
        f'data-has-gt="{"true" if has_gt else "false"}"'
    )

    return (
        f'<article class="page" {article_attrs}>'
        '  <header class="page-header">'
        '    <div class="page-header-main">'
        f'      <span class="folio">Fol. '
        f'{html_lib.escape(result.folio_label)}</span>'
        f'      {entry_html}'
        '    </div>'
        '    <div class="page-header-aside">'
        f'      <span class="meta-info">'
        f'{len(result.regions)} regions · {len(result.entities)} entities</span>'
        f'      {lang_html}'
        '    </div>'
        '  </header>'
        f'  {stats_html}'
        f'  {tools_html}'
        f'  {metrics_html}'
        f'  <div class="{cols_cls}">'
        f'    {facs_panel}'
        '    <section class="trans-panel" data-mode="document">'
        f'      {doc_html}{reading_html}'
        '    </section>'
        '  </div>'
        '</article>'
    )


# ---------------------------------------------------------------------------
# Document shell
# ---------------------------------------------------------------------------
def _document_shell(*, title, subtitle, results, legend, options, toc, pages,
                    has_maps) -> str:
    leaflet_css = (
        '<link rel="stylesheet" '
        'href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
        if has_maps else ""
    )
    leaflet_js = (
        '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" '
        'defer></script>'
        if has_maps else ""
    )
    return f"""<!DOCTYPE html>
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
{leaflet_css}<link rel="stylesheet" href="assets/edition.css">
</head>
<body>

<header class="masthead" role="banner">
  <div class="masthead-inner">
    <button type="button" class="masthead-menu" id="btn-toc"
            aria-label="Open table of contents (T)" title="Table of contents (T)">
      {icon("menu", 16)}
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
          {icon("prev", 14)}
        </button>
        <select id="page-select" aria-label="Jump to page">{options}</select>
        <button type="button" class="nav-btn" id="btn-next"
                title="Next page (→)" aria-label="Next page">
          {icon("next", 14)}
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
            aria-label="Close table of contents">{icon("close", 14)}</button>
  </div>
  <div class="toc-list">{toc}</div>
</aside>
<div class="toc-scrim" id="toc-scrim" aria-hidden="true"></div>

{legend}

<main id="pages-wrap" role="main">
{pages}
</main>

<footer class="site-foot">
  <span class="foot-mark" aria-hidden="true">· · · ❦ · · ·</span>
  <span class="foot-text">Humboldt — The Inaccurate Edition</span>
  <span class="foot-hint">
    {icon("kbd", 12)}
    <span>← → navigate · Shift to jump · T toc · R mode · S source · F layout</span>
  </span>
</footer>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

{leaflet_js}<script src="assets/edition.js" defer></script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def build_edition_bundle(
    results: List[PageResult],
    bundle_dir,
    *,
    title: str = "Humboldt — The Inaccurate Edition",
    subtitle: str = "",
    entity_colors: Optional[Dict[str, str]] = None,
    entity_labels: Optional[Dict[str, str]] = None,
    region_colors: Optional[Dict[str, str]] = None,
    region_labels: Optional[Dict[str, str]] = None,
    image_folder=None,
    image_ref_prefix: Optional[str] = None,
    embed_images: bool = False,
    full_tei_path=None,
) -> Path:
    """Write the complete static edition into ``bundle_dir`` and return it."""
    bundle_dir = Path(bundle_dir)
    assets_out = bundle_dir / "assets"
    tei_out = bundle_dir / "tei"
    facs_out = bundle_dir / "facsimiles"
    for d in (bundle_dir, assets_out, tei_out):
        d.mkdir(parents=True, exist_ok=True)

    ec = entity_colors or {}
    el = entity_labels or {}
    rc = region_colors or {}
    rl = region_labels or {}

    # Decide how facsimiles are referenced.
    if image_ref_prefix is not None:
        image_mode = "external"
    elif embed_images:
        image_mode = "embed"
    elif image_folder is not None:
        image_mode = "copy"
    else:
        image_mode = "none"

    if image_mode == "copy":
        facs_out.mkdir(parents=True, exist_ok=True)
        for r in results:
            ip = Path(image_folder) / r.image_filename
            if ip.exists():
                shutil.copy2(ip, facs_out / r.image_filename)

    # Static assets.
    shutil.copy2(_ASSETS_DIR / "edition.css", assets_out / "edition.css")
    shutil.copy2(_ASSETS_DIR / "edition.js", assets_out / "edition.js")

    # Full-book TEI, if the pipeline produced one.
    if full_tei_path is not None:
        fp = Path(full_tei_path)
        if fp.exists():
            shutil.copy2(fp, tei_out / "digital_edition.tei.xml")

    # Pages (per-page TEI is written as a side effect inside the renderer).
    page_divs = [
        _render_page_article(
            idx, result, ec, el, rc, rl,
            image_mode=image_mode, image_folder=image_folder,
            ref_prefix=image_ref_prefix, tei_dir=tei_out,
        )
        for idx, result in enumerate(results)
    ]

    has_maps = any(r.locations for r in results)
    html = _document_shell(
        title=title, subtitle=subtitle, results=results,
        legend=_legend_chips(results, ec, el, rc, rl),
        options=_page_options(results),
        toc=_toc(results),
        pages="".join(page_divs),
        has_maps=has_maps,
    )
    (bundle_dir / "index.html").write_text(html, encoding="utf-8")
    logger.info("Edition bundle written to %s", bundle_dir)
    return bundle_dir


def zip_bundle(bundle_dir, zip_path) -> Path:
    """Zip the *contents* of ``bundle_dir`` (index.html at the archive root)."""
    bundle_dir = Path(bundle_dir)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(bundle_dir).as_posix())
    logger.info("Edition archive written to %s (%.1f MB)",
                zip_path, zip_path.stat().st_size / 1e6)
    return zip_path


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
    embed_images: bool = False,
    make_zip: bool = True,
):
    """Build the edition bundle (and, by default, a zip) from ``output_path``.

    ``output_path`` is interpreted as the desired index location: its parent
    receives a ``<stem>/`` bundle directory, and a sibling ``<stem>.zip`` is
    written next to it. Returns the zip path when ``make_zip`` is true,
    otherwise the bundle directory.
    """
    output_path = Path(output_path)
    bundle_dir = output_path.with_suffix("")  # ".../humboldt_edition"
    bundle_dir = bundle_dir.parent / bundle_dir.name

    build_edition_bundle(
        results, bundle_dir,
        title=title, subtitle=subtitle,
        entity_colors=entity_colors, entity_labels=entity_labels,
        region_colors=region_colors, region_labels=region_labels,
        image_folder=image_folder, image_ref_prefix=image_ref_prefix,
        embed_images=embed_images,
        full_tei_path=output_path.parent / "digital_edition.tei.xml",
    )

    if make_zip:
        zip_path = bundle_dir.with_suffix(".zip")
        return zip_bundle(bundle_dir, zip_path)
    return bundle_dir

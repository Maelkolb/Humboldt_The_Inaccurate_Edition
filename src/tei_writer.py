"""
TEI XML Writer – Humboldt Journal Digital Edition
==================================================
Serialises :class:`~src.models.PageResult` objects to TEI XML following the
same conventions used by **edition humboldt digital**
(https://edition-humboldt.de/) and by ``tei_parser.py`` in this codebase.

Two output modes
----------------
1. ``results_to_tei_document(results, ...)`` — a full TEI document for the
   whole book. Includes a ``<teiHeader>`` and one ``<pb/>`` per page in
   ``<text><body>``. This is what gets written to
   ``output/digital_edition.tei.xml``.

2. ``page_result_to_tei_document(page_result, ...)`` — a full, self-contained
   TEI document for a single page (one ``<pb/>``, surrounded by a minimal but
   valid ``<teiHeader>``). This is what the HTML "Download TEI" button
   per-page produces.

Markup mapping (matches ``tei_parser._extract_text`` in reverse)
----------------------------------------------------------------
  ``~~text~~``        →  ``<del rendition="#s">text</del>``
  ``<u>text</u>``     →  ``<hi rendition="#u">text</hi>``
  ``word[?]``         →  ``<unclear>word</unclear>``  (use ``<unclear/>`` when
                          the stem is empty)
  ``[text]``          →  ``<supplied>text</supplied>``
  ``[N pages unleserlich]``  →  ``<gap unit="pages" quantity="N" reason="illegible"/>``
  ``[...]``           →  ``<gap/>``
  ``\\n``             →  ``<lb/>``
  Entities (Person/Location/Institution) detected by NER
                      →  ``<persName>`` / ``<placeName>`` / ``<orgName>``
                         wrapping the entity span; ``ref`` attribute carries
                         the normalised form when available.

Region → TEI structure
----------------------
  ``page_number``     →  ``<fw type="folNum">…</fw>``
  ``catch_phrase``    →  ``<fw type="catch">…</fw>``
  ``entry_heading``   →  ``<head>…</head>``  inside a ``<div type="diaryEntry">``
  ``main_text``       →  ``<p>…</p>``        (inside the current diaryEntry div
                                              when one is open, else top-level)
  ``marginal_note``   →  ``<note place="left|right|mTop|mBottom|opposite">…</note>``
  ``pasted_slip``     →  ``<note rend="sticked">…</note>``
  ``calculation`` /
  ``observation_table`` /
  ``instrument_list`` /
  ``coordinates``     →  ``<p>…</p>`` (with table cells joined by spaces and
                                       <lb/> between rows for tables)
  ``sketch``          →  ``<figure><figDesc>…</figDesc></figure>``
  ``crossed_out``     →  ``<del rendition="#s">…</del>`` at block level
  ``bibliographic_ref`` →  ``<bibl>…</bibl>``
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .models import Entity, PageResult, Region

logger = logging.getLogger(__name__)

TEI_NS = "http://www.tei-c.org/ns/1.0"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# Register the TEI namespace so ElementTree emits ``xmlns="..."`` without a
# prefix on the root element (matches the H0017682.xml style).
ET.register_namespace("", TEI_NS)

# Entity types we know how to round-trip to TEI
_ENT_TO_TEI = {
    "Person":      "persName",
    "Location":    "placeName",
    "Institution": "orgName",
}


# ---------------------------------------------------------------------------
# Inline-markup tokeniser
# ---------------------------------------------------------------------------

# Matches our editorial conventions (longest first wins because we anchor each
# alternative explicitly):
#   ~~...~~             struck-through
#   <u>...</u>          underlined
#   word[?] or [?]      uncertain reading
#   [text]              editorial supply (limited to [...] without spaces/newlines)
#   [N <unit> unleserlich]   localised gap
#   [...]               unspecified gap
# We deliberately do NOT consume entity spans here — entities are layered on
# top in a separate pass.
_MARKUP_RE = re.compile(
    r"~~(?P<del>.+?)~~"
    r"|<u>(?P<u>.+?)</u>"
    r"|(?P<unc_stem>\w*)\[\?\]"
    r"|\[(?P<gap_qty>\d+)\s+(?P<gap_unit>\w+)\s+unleserlich\]"
    r"|\[\.{3}\]"
    r"|\[(?P<supplied>[^\[\]\n]{1,200})\]",
    flags=re.DOTALL,
)


def _emit_text_into(parent: ET.Element, text: str) -> None:
    """Append plain ``text`` to ``parent``, converting newlines to ``<lb/>``."""
    if not text:
        return
    parts = text.split("\n")
    for i, frag in enumerate(parts):
        if i > 0:
            ET.SubElement(parent, f"{{{TEI_NS}}}lb")
        if not frag:
            continue
        if len(parent) == 0:
            parent.text = (parent.text or "") + frag
        else:
            last = parent[-1]
            last.tail = (last.tail or "") + frag


def _find_entity_spans(
    text: str, entities: List[Entity]
) -> List[tuple]:
    """Return non-overlapping ``(start, end, entity)`` spans for known
    entity types whose surface form occurs in ``text``."""
    raw = []
    for ent in entities:
        if not ent.text or ent.entity_type not in _ENT_TO_TEI:
            continue
        s = 0
        while True:
            i = text.find(ent.text, s)
            if i == -1:
                break
            raw.append((i, i + len(ent.text), ent))
            s = i + 1
    raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    out, cur = [], 0
    for s, e, ent in raw:
        if s >= cur:
            out.append((s, e, ent))
            cur = e
    return out


def _emit_plain_with_entities(
    parent: ET.Element, text: str, entities: List[Entity]
) -> None:
    """Emit ``text`` into ``parent``, wrapping entity surface forms in their
    TEI element (persName / placeName / orgName). Plain text portions go in
    via ``_emit_text_into`` (so ``\\n`` → ``<lb/>``)."""
    if not text:
        return
    spans = _find_entity_spans(text, entities) if entities else []
    if not spans:
        _emit_text_into(parent, text)
        return

    cursor = 0
    for s, e, ent in spans:
        if s > cursor:
            _emit_text_into(parent, text[cursor:s])

        tag = _ENT_TO_TEI[ent.entity_type]
        attrs = {}
        if ent.normalized_form:
            attrs["ref"] = ent.normalized_form
        ent_el = ET.SubElement(parent, f"{{{TEI_NS}}}{tag}", **attrs)
        _emit_text_into(ent_el, text[s:e])
        cursor = e

    if cursor < len(text):
        _emit_text_into(parent, text[cursor:])


def _render_text_with_entities(
    text: str, entities: List[Entity], parent: ET.Element
) -> None:
    """
    Render ``text`` into ``parent`` as TEI children, resolving inline editorial
    markup (``~~``, ``<u>``, ``[?]``, ``[supplied]``, gaps, newlines) and
    layering entity wrappers (``persName`` / ``placeName`` / ``orgName``)
    inside both the plain segments AND the inner content of markup wrappers.

    Strategy:
      1. Tokenise the markup with ``_MARKUP_RE``.
      2. For each plain-text gap between matches, apply entity layering.
      3. For each markup wrapper (``~~...~~``, ``<u>...</u>``), build the
         wrapper element and recurse into it with the same algorithm.
      4. Inline self-closing markup (``[?]``, ``[...]``, gaps, ``<supplied>``)
         emits a single TEI element with no further nesting.
    """
    if not text:
        return

    cursor = 0
    n = len(text)
    while cursor < n:
        m = _MARKUP_RE.search(text, cursor)
        if not m:
            _emit_plain_with_entities(parent, text[cursor:], entities)
            break

        # Plain text before the markup match: layer entities into it
        if m.start() > cursor:
            _emit_plain_with_entities(parent, text[cursor:m.start()], entities)

        if m.group("del") is not None:
            # ~~struck~~ → <del rendition="#s">struck</del>, with entities
            inner = m.group("del")
            del_el = ET.SubElement(
                parent, f"{{{TEI_NS}}}del", rendition="#s"
            )
            _render_text_with_entities(inner, entities, del_el)
        elif m.group("u") is not None:
            # <u>underlined</u> → <hi rendition="#u">…</hi>, with entities
            inner = m.group("u")
            hi = ET.SubElement(parent, f"{{{TEI_NS}}}hi", rendition="#u")
            _render_text_with_entities(inner, entities, hi)
        elif m.group("unc_stem") is not None:
            stem = m.group("unc_stem") or ""
            if stem:
                unc = ET.SubElement(parent, f"{{{TEI_NS}}}unclear")
                # The stem itself could in principle be an entity (rare); we
                # treat it as plain text for simplicity.
                unc.text = stem
            else:
                ET.SubElement(parent, f"{{{TEI_NS}}}unclear")
        elif m.group("gap_qty") is not None:
            ET.SubElement(
                parent, f"{{{TEI_NS}}}gap",
                unit=m.group("gap_unit"),
                quantity=m.group("gap_qty"),
                reason="illegible",
            )
        elif m.group("supplied") is not None:
            inner = m.group("supplied")
            sup = ET.SubElement(parent, f"{{{TEI_NS}}}supplied")
            sup.text = inner
        else:
            # [...] (unspecified gap)
            ET.SubElement(parent, f"{{{TEI_NS}}}gap")

        cursor = m.end()


# ---------------------------------------------------------------------------
# Region → TEI element(s)
# ---------------------------------------------------------------------------

# Where each region type ends up. Some types need to be placed inside the
# currently-open diaryEntry div; others (page_number, catch_phrase) live at
# the top level of <body>.
_PLACE_MAP = {
    "left": "left",
    "right": "right",
    "mTop": "mTop",
    "mBottom": "mBottom",
    "opposite": "opposite",
    "inline": "inline",
}


def _table_text(region: Region) -> str:
    """Flatten a table_data dict into a single text body (cells joined by
    spaces, rows joined by ``\\n`` so the markup tokeniser turns them into
    ``<lb/>``)."""
    if region.table_data and isinstance(region.table_data, dict):
        cells = region.table_data.get("cells") or []
        if cells:
            rows = []
            for row in cells:
                rows.append("  ".join(
                    str(c) for c in row if c is not None
                ))
            return "\n".join(rows)
    return region.content or ""


def _make_region_element(
    region: Region, entities: List[Entity]
) -> Optional[ET.Element]:
    """Build the TEI element for one region (without attaching it to a parent
    yet). Returns ``None`` when the region produces no TEI content (e.g. an
    opposite-folio bleedthrough that was intentionally left blank)."""
    rtype = region.region_type
    text  = region.content or ""

    # opposite-folio bleedthrough: not transcribed, do NOT emit
    if (rtype == "marginal_note"
            and (region.marginal_position or "") == "opposite"
            and not text.strip()):
        return None

    if rtype == "page_number":
        fw = ET.Element(f"{{{TEI_NS}}}fw", type="folNum")
        _render_text_with_entities(text, entities, fw)
        return fw

    if rtype == "catch_phrase":
        fw = ET.Element(f"{{{TEI_NS}}}fw", type="catch")
        _render_text_with_entities(text, entities, fw)
        return fw

    if rtype == "entry_heading":
        head = ET.Element(f"{{{TEI_NS}}}head")
        _render_text_with_entities(text, entities, head)
        return head

    if rtype == "marginal_note":
        attrs = {"hand": "#author"}
        mp = _PLACE_MAP.get(region.marginal_position or "", None)
        if mp:
            attrs["place"] = mp
        if region.is_pasted_slip:
            attrs["rend"] = "sticked"
        note = ET.Element(f"{{{TEI_NS}}}note", **attrs)
        # Marginal notes are wrapped in <p> internally in the GT TEI
        p = ET.SubElement(note, f"{{{TEI_NS}}}p")
        _render_text_with_entities(text, entities, p)
        return note

    if rtype == "pasted_slip":
        note = ET.Element(f"{{{TEI_NS}}}note", rend="sticked", hand="#author")
        p = ET.SubElement(note, f"{{{TEI_NS}}}p")
        _render_text_with_entities(text, entities, p)
        return note

    if rtype == "sketch" or region.is_visual:
        fig = ET.Element(f"{{{TEI_NS}}}figure")
        desc = ET.SubElement(fig, f"{{{TEI_NS}}}figDesc")
        desc.text = text or "Hand-drawn illustration"
        return fig

    if rtype == "crossed_out":
        del_el = ET.Element(f"{{{TEI_NS}}}del", rendition="#s")
        _render_text_with_entities(text, entities, del_el)
        return del_el

    if rtype == "bibliographic_ref":
        bibl = ET.Element(f"{{{TEI_NS}}}bibl")
        _render_text_with_entities(text, entities, bibl)
        return bibl

    if rtype in ("observation_table", "instrument_list", "calculation",
                 "coordinates"):
        p = ET.Element(f"{{{TEI_NS}}}p")
        if rtype in ("observation_table", "instrument_list"):
            body_text = _table_text(region)
        else:
            body_text = text
        _render_text_with_entities(body_text, entities, p)
        return p

    # Default: main_text (and any unknown type) → <p>
    p = ET.Element(f"{{{TEI_NS}}}p")
    _render_text_with_entities(text, entities, p)
    return p


# ---------------------------------------------------------------------------
# Region ordering and page body assembly
# ---------------------------------------------------------------------------

def _region_sort_key(r: Region):
    """Reading-order sort: bbox top, then left. Regions without bboxes keep
    their natural ``region_index`` order at the end."""
    if r.bbox and len(r.bbox) == 4:
        return (0, float(r.bbox[0]), float(r.bbox[1]), r.region_index)
    return (1, 0.0, 0.0, r.region_index)


def _append_page_body(
    body: ET.Element,
    page: PageResult,
    *,
    emit_pb: bool = True,
) -> None:
    """
    Append a single page's regions into the given ``<body>`` element,
    preceded by a ``<pb facs="..." n="..."/>`` when ``emit_pb`` is True.

    The structural pattern follows H0017682.xml: page-level fw (page_number,
    catch_phrase) sit directly under <body>, while diaryEntry divs gather an
    entry_heading (<head>), the prose (<p>), and any associated marginal
    notes (<note>) / figures.
    """
    if emit_pb:
        attrs = {"n": page.folio_label or str(page.page_number)}
        if page.image_filename:
            # Use the bare filename as a facs hint; consumers can resolve to
            # the actual image. This is informational, not required for TEI
            # to be valid.
            attrs["facs"] = page.image_filename
        ET.SubElement(body, f"{{{TEI_NS}}}pb", **attrs)

    regions = sorted(page.regions, key=_region_sort_key)

    # Page-level fw (page_number, catch_phrase): always at top of body
    for r in regions:
        if r.region_type in ("page_number", "catch_phrase"):
            el = _make_region_element(r, page.entities)
            if el is not None:
                body.append(el)

    # Single diaryEntry div per page (most pages are continuous prose).
    # If we ever see multiple entry_heading regions, we open a new div for
    # each.
    cur_div: Optional[ET.Element] = None

    def _ensure_div() -> ET.Element:
        nonlocal cur_div
        if cur_div is None:
            cur_div = ET.SubElement(body, f"{{{TEI_NS}}}div", type="diaryEntry")
        return cur_div

    # We need to emit the rest of the regions in their bbox order, but with
    # one twist: when we hit an entry_heading, close the previous div and
    # open a new one.
    for r in regions:
        if r.region_type in ("page_number", "catch_phrase"):
            continue  # already emitted

        el = _make_region_element(r, page.entities)
        if el is None:
            continue

        if r.region_type == "entry_heading":
            # Start (or restart) a div with this <head>
            cur_div = ET.SubElement(body, f"{{{TEI_NS}}}div", type="diaryEntry")
            cur_div.append(el)
        else:
            _ensure_div().append(el)


# ---------------------------------------------------------------------------
# TEI header
# ---------------------------------------------------------------------------

def _build_tei_header(
    title: str,
    *,
    edition_url: Optional[str] = None,
    creator: Optional[str] = "Alexander von Humboldt",
) -> ET.Element:
    """Build a minimal but well-formed ``<teiHeader>`` element."""
    header = ET.Element(f"{{{TEI_NS}}}teiHeader")

    file_desc = ET.SubElement(header, f"{{{TEI_NS}}}fileDesc")

    title_stmt = ET.SubElement(file_desc, f"{{{TEI_NS}}}titleStmt")
    title_el = ET.SubElement(title_stmt, f"{{{TEI_NS}}}title")
    title_el.text = title

    pub_stmt = ET.SubElement(file_desc, f"{{{TEI_NS}}}publicationStmt")
    publisher = ET.SubElement(pub_stmt, f"{{{TEI_NS}}}publisher")
    publisher.text = "Humboldt – The Inaccurate Edition (generated)"
    avail = ET.SubElement(pub_stmt, f"{{{TEI_NS}}}availability")
    licence = ET.SubElement(
        avail, f"{{{TEI_NS}}}licence",
        target="https://creativecommons.org/licenses/by-sa/4.0/",
    )
    licence.text = "Creative Commons Attribution-ShareAlike 4.0 International"
    if edition_url:
        idno = ET.SubElement(pub_stmt, f"{{{TEI_NS}}}idno", type="URLWeb")
        idno.text = edition_url

    src_desc = ET.SubElement(file_desc, f"{{{TEI_NS}}}sourceDesc")
    bibl = ET.SubElement(src_desc, f"{{{TEI_NS}}}bibl")
    bibl.text = (
        f"Automated transcription of {title}"
        + (f" by {creator}" if creator else "")
        + ", produced by the Humboldt – The Inaccurate Edition pipeline."
    )

    profile = ET.SubElement(header, f"{{{TEI_NS}}}profileDesc")
    creation = ET.SubElement(profile, f"{{{TEI_NS}}}creation")
    date = ET.SubElement(creation, f"{{{TEI_NS}}}date")
    date.text = datetime.now().date().isoformat()

    return header


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------

def _indent(elem: ET.Element, level: int = 0) -> None:
    """In-place pretty-printer (ET.indent is only available on Py3.9+, and
    we want to be conservative here). Adds whitespace text/tail nodes."""
    i = "\n" + "    " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "    "
        for sub in elem:
            _indent(sub, level + 1)
        # Last child's tail should be one level back up
        if not elem[-1].tail or not elem[-1].tail.strip():
            elem[-1].tail = i
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = i


def _tostring(root: ET.Element) -> str:
    """Serialise ``root`` to a UTF-8 string with XML declaration and pretty
    indentation."""
    _indent(root)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def results_to_tei_document(
    results: List[PageResult],
    *,
    title: str = "Humboldt – Travel Journal (automated transcription)",
    edition_url: Optional[str] = None,
) -> str:
    """
    Serialise a list of PageResult objects to a single TEI XML document.

    Returns the document as a string (UTF-8, with XML declaration).
    """
    root = ET.Element(f"{{{TEI_NS}}}TEI")
    root.append(_build_tei_header(title, edition_url=edition_url))

    text_el = ET.SubElement(root, f"{{{TEI_NS}}}text")
    body = ET.SubElement(text_el, f"{{{TEI_NS}}}body")

    for page in results:
        _append_page_body(body, page, emit_pb=True)

    return _tostring(root)


def page_result_to_tei_document(
    page: PageResult,
    *,
    title: Optional[str] = None,
) -> str:
    """
    Serialise a single PageResult to a self-contained TEI XML document
    (with its own teiHeader and one ``<pb/>``).

    Used by the HTML viewer's per-page TEI download button.
    """
    page_title = title or f"Humboldt – Folio {page.folio_label}"
    root = ET.Element(f"{{{TEI_NS}}}TEI")
    root.append(_build_tei_header(page_title))

    text_el = ET.SubElement(root, f"{{{TEI_NS}}}text")
    body = ET.SubElement(text_el, f"{{{TEI_NS}}}body")
    _append_page_body(body, page, emit_pb=True)

    return _tostring(root)


def write_tei_file(
    results: List[PageResult],
    output_path: str | Path,
    *,
    title: str = "Humboldt – Travel Journal (automated transcription)",
    edition_url: Optional[str] = None,
) -> Path:
    """Convenience wrapper: write the full-book TEI to disk."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    xml = results_to_tei_document(
        results, title=title, edition_url=edition_url
    )
    output_path.write_text(xml, encoding="utf-8")
    size_kb = output_path.stat().st_size / 1024
    logger.info("TEI document written to %s (%.1f KB)", output_path, size_kb)
    return output_path

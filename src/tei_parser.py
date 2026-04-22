"""
TEI XML Parser – Humboldt Journal Edition
==========================================
Parses TEI XML files from edition-humboldt.de directly into PageResult objects,
enabling HTML edition generation WITHOUT requiring Gemini API transcription.

When you already have the scholarly TEI XML (e.g. from
https://edition-humboldt.de/v11/H1242132), this produces higher-quality output
than image-based transcription because:
- The text is the published scholarly transcription
- All editorial apparatus (del, add, unclear, etc.) is preserved
- Named entities are TEI-tagged (persName, placeName, orgName)
- Marginal notes carry precise place attributes (left, right, mTop, mBottom)

Usage:
    from src.tei_parser import parse_tei_file
    results = parse_tei_file("H1242132.xml")
    # → same List[PageResult] as process_book(); feed directly into generate_html_edition()

Supported TEI elements:
    <pb>            page break (defines folio boundaries)
    <fw>            forme work (page number, catchword)
    <note place="">  marginal notes; place = left|right|mTop|mBottom|opposite|inline
    <note rend="sticked">  pasted slip
    <figure>        sketch or diagram
    <del>           struck-through text  →  ~~text~~
    <add>           interlinear addition (flattened into main text)
    <subst>         substitution (del + add)
    <unclear>       uncertain reading    →  text[?]
    <gap>           lacuna              →  [...]
    <supplied>      editorial supply    →  [text]
    <choice>        orig/reg or abbr/expan
    <lb/>           line break          →  \\n
    <persName>      named person entity
    <placeName>     named place entity
    <orgName>       named organisation entity
    <foreign xml:lang>  language switch
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lxml import etree

from .models import Entity, GeoLocation, PageResult, Region

logger = logging.getLogger(__name__)

TEI_NS = "http://www.tei-c.org/ns/1.0"
XML_NS = "http://www.w3.org/XML/1998/namespace"
TEI = f"{{{TEI_NS}}}"
XML = f"{{{XML_NS}}}"

# TEI named-entity tags → our entity types
_TEI_ENT_MAP = {
    "persName": "Person",
    "placeName": "Location",
    "orgName": "Institution",
}


# ---------------------------------------------------------------------------
# Low-level text extraction
# ---------------------------------------------------------------------------

def _localname(elem) -> str:
    if callable(elem.tag):
        return ""
    return etree.QName(elem.tag).localname


def _get_all_text(elem) -> str:
    """Get concatenated text content of an element (ignoring structure)."""
    return "".join(elem.itertext())


def _extract_text(elem, skip_notes: bool = True) -> str:
    """
    Recursively extract text from a TEI element, applying editorial apparatus
    notation conventions used throughout this codebase:
      - <del> → ~~text~~
      - <unclear> → text[?]
      - <gap> → [...]
      - <supplied> → [text]
      - <choice><orig>/<reg> → reg (preferred)
      - <lb/> → \\n
      - <note> → skipped if skip_notes=True
      - <figure> → skipped
      - <metamark>, <anchor>, <fw>, <pb> → skipped (ignored completely)
    """
    parts: List[str] = []

    def walk(node):
        if callable(node.tag):   # Comment, ProcessingInstruction
            if node.tail:
                parts.append(node.tail)
            return

        local = _localname(node)

        # ---- elements to skip entirely ----
        if local in ("pb", "anchor", "metamark"):
            if node.tail:
                parts.append(node.tail)
            return

        if local == "fw":
            if node.tail:
                parts.append(node.tail)
            return

        if local == "figure":
            if node.tail:
                parts.append(node.tail)
            return

        if local == "note" and skip_notes:
            if node.tail:
                parts.append(node.tail)
            return

        # ---- structural / formatting ----
        if local == "lb":
            parts.append("\n")
            if node.tail:
                parts.append(node.tail)
            return

        if local == "del":
            text = _get_all_text(node).strip()
            if text:
                parts.append(f"~~{text}~~")
            if node.tail:
                parts.append(node.tail)
            return

        if local == "subst":
            del_node = node.find(f"{TEI}del")
            add_node = node.find(f"{TEI}add")
            if del_node is not None:
                t = _get_all_text(del_node).strip()
                if t:
                    parts.append(f"~~{t}~~")
            if add_node is not None:
                if add_node.text:
                    parts.append(add_node.text)
                for child in add_node:
                    walk(child)
            if node.tail:
                parts.append(node.tail)
            return

        if local == "add":
            if node.text:
                parts.append(node.text)
            for child in node:
                walk(child)
            if node.tail:
                parts.append(node.tail)
            return

        if local == "unclear":
            text = _get_all_text(node)
            parts.append(f"{text}[?]")
            if node.tail:
                parts.append(node.tail)
            return

        if local == "gap":
            unit = node.get("unit", "")
            quantity = node.get("quantity", "")
            if unit and quantity:
                parts.append(f"[{quantity} {unit} unleserlich]")
            else:
                parts.append("[...]")
            if node.tail:
                parts.append(node.tail)
            return

        if local == "supplied":
            text = _get_all_text(node)
            parts.append(f"[{text}]")
            if node.tail:
                parts.append(node.tail)
            return

        if local == "choice":
            reg = node.find(f"{TEI}reg")
            orig = node.find(f"{TEI}orig")
            expan = node.find(f"{TEI}expan")
            abbr = node.find(f"{TEI}abbr")
            if reg is not None:
                parts.append(_get_all_text(reg))
            elif expan is not None:
                parts.append(_get_all_text(expan))
            elif orig is not None:
                parts.append(_get_all_text(orig))
            elif abbr is not None:
                parts.append(_get_all_text(abbr))
            if node.tail:
                parts.append(node.tail)
            return

        # ---- transparent / pass-through ----
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if node.tail:
            parts.append(node.tail)

    if elem.text:
        parts.append(elem.text)
    for child in elem:
        walk(child)

    return "".join(parts)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def _collect_languages(elem) -> List[str]:
    """Collect xml:lang values from <foreign> and other elements in elem."""
    langs: set = set()
    for node in elem.iter():
        if callable(node.tag):
            continue
        lang = node.get(f"{XML}lang")
        if lang:
            langs.add(lang[:2].lower())  # normalise "deu" → "de", "fra" → "fr", etc.
    # Normalise codes
    norm: set = set()
    for lg in langs:
        if lg.startswith("de"):
            norm.add("de")
        elif lg.startswith("fr"):
            norm.add("fr")
        elif lg.startswith("la"):
            norm.add("la")
        elif lg.startswith("es"):
            norm.add("es")
        else:
            norm.add(lg)
    return sorted(norm)


# ---------------------------------------------------------------------------
# Named entity extraction
# ---------------------------------------------------------------------------

def _collect_entities(elem) -> List[Dict[str, str]]:
    """
    Extract named entities from TEI persName / placeName / orgName elements.
    Returns list of {text, entity_type, normalized_form, language} dicts.
    """
    entities = []
    for tag, etype in _TEI_ENT_MAP.items():
        for node in elem.findall(f".//{TEI}{tag}"):
            text = _get_all_text(node).strip()
            if not text:
                continue
            ref = node.get("ref", "")
            lang = (node.get(f"{XML}lang") or "")[:2].lower() or None
            entities.append({
                "text": text,
                "entity_type": etype,
                "normalized_form": ref if ref and not ref.startswith("#") else None,
                "language": lang,
            })
    return entities


# ---------------------------------------------------------------------------
# Page content collector
# ---------------------------------------------------------------------------

class _PageCollector:
    """
    Walks the TEI body in document order, splitting content at <pb> elements
    and collecting notes, figures, etc. into per-page buckets.
    """

    def __init__(self):
        self._pages: List[Dict[str, Any]] = []
        self._current: Optional[Dict[str, Any]] = None

    def _new_page(self, n: str, facs: str):
        self._current = {
            "n": n,
            "facs": facs,
            "text_parts": [],
            "notes": [],        # list of lxml elements
            "figures": [],      # list of lxml elements
            "fw": [],           # list of lxml elements
        }
        self._pages.append(self._current)

    def _push(self, text: str):
        if self._current is not None and text:
            self._current["text_parts"].append(text)

    def walk(self, elem):
        if callable(elem.tag):
            if elem.tail:
                self._push(elem.tail)
            return

        local = _localname(elem)

        if local == "pb":
            n = elem.get("n", "")
            facs = elem.get("facs", "")
            self._new_page(n, facs)
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "note":
            if self._current is not None:
                self._current["notes"].append(elem)
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "figure":
            if self._current is not None:
                self._current["figures"].append(elem)
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "fw":
            if self._current is not None:
                self._current["fw"].append(elem)
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "metamark":
            # Skip metamark entirely (used to collect Erledigt-Striche; feature removed)
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "anchor":
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "lb":
            self._push("\n")
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "del":
            text = _get_all_text(elem).strip()
            if text:
                self._push(f"~~{text}~~")
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "subst":
            del_node = elem.find(f"{TEI}del")
            add_node = elem.find(f"{TEI}add")
            if del_node is not None:
                t = _get_all_text(del_node).strip()
                if t:
                    self._push(f"~~{t}~~")
            if add_node is not None:
                if add_node.text:
                    self._push(add_node.text)
                for child in add_node:
                    self.walk(child)
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "add":
            if elem.text:
                self._push(elem.text)
            for child in elem:
                self.walk(child)
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "unclear":
            text = _get_all_text(elem)
            self._push(f"{text}[?]")
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "gap":
            unit = elem.get("unit", "")
            quantity = elem.get("quantity", "")
            if unit and quantity:
                self._push(f"[{quantity} {unit} unleserlich]")
            else:
                self._push("[...]")
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "supplied":
            text = _get_all_text(elem)
            self._push(f"[{text}]")
            if elem.tail:
                self._push(elem.tail)
            return

        if local == "choice":
            reg = elem.find(f"{TEI}reg")
            orig = elem.find(f"{TEI}orig")
            expan = elem.find(f"{TEI}expan")
            abbr = elem.find(f"{TEI}abbr")
            if reg is not None:
                self._push(_get_all_text(reg))
            elif expan is not None:
                self._push(_get_all_text(expan))
            elif orig is not None:
                self._push(_get_all_text(orig))
            elif abbr is not None:
                self._push(_get_all_text(abbr))
            if elem.tail:
                self._push(elem.tail)
            return

        # Default: pass-through
        if elem.text:
            self._push(elem.text)
        for child in elem:
            self.walk(child)
        if elem.tail:
            self._push(elem.tail)

    @property
    def pages(self) -> List[Dict[str, Any]]:
        return self._pages


# ---------------------------------------------------------------------------
# Region builders
# ---------------------------------------------------------------------------

_ENTRY_RE = re.compile(r"^(\d{1,3}[).])")


def _detect_entry_numbers(text: str) -> List[str]:
    entries = []
    for line in text.split("\n"):
        m = _ENTRY_RE.match(line.strip())
        if m:
            num = re.sub(r"[).]", "", m.group(1))
            if num not in entries:
                entries.append(num)
    return entries


def _build_page_regions(page_data: Dict[str, Any], page_idx: int) -> List[Region]:
    """Convert raw page_data dict → list of Region objects."""
    regions: List[Region] = []
    ridx = 0

    # 1) Page number (fw type="folNum")
    for fw_elem in page_data["fw"]:
        fw_type = fw_elem.get("type", "")
        text = _get_all_text(fw_elem).strip()
        if not text:
            continue
        rtype = "page_number" if "folNum" in fw_type else "catch_phrase"
        regions.append(Region(
            region_type=rtype,
            region_index=ridx,
            content=text,
            is_visual=False,
            languages=["de"],
            position="top right" if rtype == "page_number" else "bottom",
        ))
        ridx += 1

    # 2) Main text
    main_text = "".join(page_data["text_parts"]).strip()
    if main_text:
        langs = ["de"]  # default; will be enriched below
        regions.append(Region(
            region_type="main_text",
            region_index=ridx,
            content=main_text,
            is_visual=False,
            languages=langs,
            position="main body",
        ))
        ridx += 1

    # 3) Marginal notes and pasted slips
    for note_elem in page_data["notes"]:
        place = note_elem.get("place", "inline")
        rend = note_elem.get("rend", "")
        xml_id = note_elem.get(f"{XML}id") or note_elem.get("id")

        content = _extract_text(note_elem, skip_notes=False).strip()
        if not content:
            continue

        langs = _collect_languages(note_elem) or ["de"]

        is_sticked = "sticked" in rend
        rtype = "pasted_slip" if is_sticked else "marginal_note"

        # Normalise place → marginal_position
        PLACE_MAP = {
            "left": "left",
            "right": "right",
            "mTop": "mTop",
            "mBottom": "mBottom",
            "opposite": "opposite",
            "inline": "inline",
        }
        mp = PLACE_MAP.get(place, "left" if "left" in place else
                            "right" if "right" in place else None)

        editorial_note = None
        if is_sticked:
            editorial_note = "Pasted slip (Zettel) – physically attached to the page"
        elif mp:
            editorial_note = f"Marginal note ({mp} margin)"

        regions.append(Region(
            region_type=rtype,
            region_index=ridx,
            content=content,
            is_visual=False,
            languages=langs,
            position=f"{place} margin" if not is_sticked else "pasted",
            marginal_position=mp,
            is_pasted_slip=is_sticked,
            editorial_note=editorial_note,
            tei_id=xml_id,
        ))
        ridx += 1

    # 4) Figures (sketches)
    for fig_elem in page_data["figures"]:
        # Try to extract any text label inside the figure
        label_elems = fig_elem.findall(f".//{TEI}figDesc") or \
                      fig_elem.findall(f".//{TEI}label") or \
                      fig_elem.findall(f".//{TEI}p")
        desc_parts = [_get_all_text(e).strip() for e in label_elems if _get_all_text(e).strip()]
        desc = " ".join(desc_parts) or "Hand-drawn illustration"

        regions.append(Region(
            region_type="sketch",
            region_index=ridx,
            content=desc,
            is_visual=True,
            languages=[],
            position="inline",
        ))
        ridx += 1

    return regions


# ---------------------------------------------------------------------------
# Entity collector across whole page
# ---------------------------------------------------------------------------

def _build_page_entities(page_data: Dict[str, Any]) -> List[Entity]:
    """
    Extract TEI-tagged named entities from all elements in the page.
    We build a fake root element so we can search across the entire page.
    """
    # We need to search across the text portions too, but those are plain strings.
    # Collect entities from the lxml element trees we kept (notes, figures).
    entities: List[Entity] = []
    seen: set = set()

    def _add_from_elem(elem):
        for item in _collect_entities(elem):
            key = (item["text"], item["entity_type"])
            if key in seen:
                continue
            seen.add(key)
            entities.append(Entity(
                text=item["text"],
                entity_type=item["entity_type"],
                start_char=-1,
                end_char=-1,
                normalized_form=item.get("normalized_form"),
                language=item.get("language"),
            ))

    for note in page_data["notes"]:
        _add_from_elem(note)
    for fig in page_data["figures"]:
        _add_from_elem(fig)

    return entities


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_tei_file(
    tei_path: str | Path,
    model_id: str = "gemini-3-flash-preview",
) -> List[PageResult]:
    """
    Parse a TEI XML file from edition-humboldt.de into a list of PageResult objects.

    The resulting list can be passed directly to generate_html_edition().
    No API calls are made — this is pure XML parsing.

    Args:
        tei_path: Path to the TEI XML file.
        model_id: Model ID to record in PageResult.model_used (informational only).

    Returns:
        List of PageResult objects, one per folio (pb element).
    """
    tei_path = Path(tei_path)
    logger.info("Parsing TEI file: %s", tei_path)

    tree = etree.parse(str(tei_path))
    root = tree.getroot()

    # Find the body element
    body = root.find(f".//{TEI}body")
    if body is None:
        raise ValueError(f"No <body> element found in {tei_path}")

    # Walk the body, collecting per-page content
    collector = _PageCollector()
    if body.text:
        collector._push(body.text)
    for child in body:
        collector.walk(child)

    pages = collector.pages
    logger.info("Found %d pages (pb elements)", len(pages))

    results: List[PageResult] = []
    timestamp = datetime.now().isoformat()

    for page_idx, page_data in enumerate(pages):
        folio_n = page_data["n"]
        facs = page_data["facs"]

        # Skip blank/empty pages
        main_text = "".join(page_data["text_parts"]).strip()
        has_content = bool(
            main_text or page_data["notes"] or page_data["figures"]
        )
        if not has_content:
            logger.debug("Skipping empty page %s", folio_n)
            continue

        regions = _build_page_regions(page_data, page_idx)
        entities = _build_page_entities(page_data)

        # Collect all languages
        all_langs: set = set()
        for r in regions:
            all_langs.update(r.languages or [])

        # Entry numbers from main text
        entry_numbers = _detect_entry_numbers(main_text)

        # Build the full_text string (for NER compatibility)
        full_text_parts = [main_text] if main_text else []
        for note in page_data["notes"]:
            nt = _extract_text(note, skip_notes=False).strip()
            if nt:
                full_text_parts.append(nt)
        full_text = "\n\n".join(full_text_parts)

        # Image filename: derive from folio label or facs URL
        img_filename = f"folio_{folio_n}.jpg"
        if facs:
            # Try to get the last path component as a filename hint
            fn = facs.rstrip("/").split("/")[-1]
            if fn:
                img_filename = fn + ".jpg"

        result = PageResult(
            page_number=page_idx + 1,
            image_filename=img_filename,
            folio_label=folio_n,
            regions=regions,
            full_text=full_text,
            entities=entities,
            locations=[],  # geocoding can be run separately
            processing_timestamp=timestamp,
            model_used=f"tei_parser:{model_id}",
            entry_numbers=entry_numbers,
            page_languages=sorted(all_langs),
        )
        results.append(result)
        logger.debug("  Folio %s: %d regions, %d entities",
                     folio_n, len(regions), len(entities))

    logger.info("Parsed %d pages from TEI file.", len(results))
    return results


def parse_tei_string(
    tei_xml: str,
    model_id: str = "gemini-3-flash-preview",
) -> List[PageResult]:
    """
    Parse a TEI XML string (e.g. downloaded from edition-humboldt.de API).
    Same as parse_tei_file() but accepts a string instead of a file path.
    """
    import io
    tree = etree.parse(io.BytesIO(tei_xml.encode("utf-8")))
    root = tree.getroot()

    # Reuse the same logic
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False, mode="w", encoding="utf-8") as f:
        f.write(tei_xml)
        tmp = f.name
    try:
        return parse_tei_file(tmp, model_id=model_id)
    finally:
        os.unlink(tmp)

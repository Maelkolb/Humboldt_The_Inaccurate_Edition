"""Detect and classify the regions on a journal page (whole-page reasoning).

The returned bounding boxes drive the per-region crops. Pages are complex:
numbered entries, multilingual Kurrentschrift, marginalia (including
opposite-folio bleedthrough), tables, sketches, struck-through passages, and a
non-linear reading order.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from google import genai

from .imaging import page_image_bytes
from .llm import generate_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt – tailored to Humboldt's journal structure
# ---------------------------------------------------------------------------

REGION_DETECTION_PROMPT = """\
You are a specialist in Alexander von Humboldt's handwritten scientific
journals, examining one page from his travel field journals. These pages
have extremely heterogeneous layouts with many co-existing text layers.

WHAT THE PAGES LOOK LIKE:
- Entries are numbered ("50)", "N. 9-11", "N. 50-52"); a page may span several
  entries or continue one from the previous page.
- Main text is German Kurrentschrift with frequent switches to French, Latin
  (scientific nomenclature) and Spanish.
- Margins carry notes in a different hand-size, sometimes rotated, at the top,
  bottom or side. These are genuine content Humboldt added, not decoration.
- Astronomical/geodetic calculations appear as columns of numbers with the
  degree, minute and second symbols, often in tables.
- Pen sketches of mountain profiles, river courses and diagrams appear inline
  or in the margins.
- Words or whole paragraphs are struck through.

REGION TYPES TO DETECT:
- entry_heading: numbered entry header ("N. 50-52.", "51)", "9)")
- main_text: primary journal prose within an entry (the body column)
- marginal_note: text Humboldt wrote in a margin of THIS leaf, reading in normal
  orientation and lying on THIS side of the binding fold. Record marginal_position
  "left", "right", "mTop" or "mBottom". A genuine marginal note may be faint, small
  or a tall narrow column — keep it as long as it belongs to THIS leaf.
- pasted_slip: a separate piece of paper physically pasted on (distinct texture,
  raised edge, own ink, sometimes angled), carrying its own text.
- calculation: mathematical/astronomical computation blocks
- observation_table: structured observational data (times, angles, measurements)
- sketch: hand-drawn illustration, landscape profile or diagram
- crossed_out: a large struck-through section (multiple lines or a whole block
  deleted as a unit). NOT for single struck words -- those are handled inline.
- instrument_list: lists of scientific instruments, often with prices
- page_number: folio number (usually a top corner, e.g. "2r", "67")

OPPOSITE-FOLIO TEXT — DETECT IT, BUT NEVER TRANSCRIBE IT:
The image almost always captures a thin strip of the FACING leaf across the book's
binding. That binding shows as a darker vertical FOLD / GUTTER line (usually with a
shadow) toward one side of the image. Any writing on the FAR side of that fold line
-- in the narrow strip that belongs to the adjacent page -- is the OPPOSITE FOLIO,
not this page's content. The same goes for faint, mirror-reversed show-through from
the leaf's reverse. In every such case emit it as region_type "marginal_note" with
marginal_position "opposite", has_text false, and empty content -- never transcribe.
How to distinguish it from a real margin note:
  * Opposite-folio text sits BEYOND the gutter fold, in the outermost left/right
    strip; it is often cut off at the image edge, may be mirror-reversed, and is
    oriented toward the other page.
  * A genuine marginal note -- even a faint, tall, narrow one -- lies on THIS side
    of the fold and reads in normal orientation. Faintness or a slim shape ALONE
    does NOT make it opposite-folio; the side of the fold and the orientation do.

RULES:
1. Marginal notes are content: detect every one with the right marginal_position.
   The only exception is opposite-folio text beyond the binding fold (see above):
   marginal_position "opposite", has_text false, never transcribed.
2. Merge small fragments: do not emit bare enumeration numbers, single words or
   short labels as their own region -- fold them into the block they belong to.
3. Coherent blocks: a continuous main_text passage is ONE region; split only at
   genuine type changes.
4. Bounding boxes: be generous -- slightly too large beats clipping text. Each
   region needs a bbox [y_min, x_min, y_max, x_max], values 0-1000 relative to
   the full image (0 = top/left, 1000 = bottom/right).
5. Reading order: return regions in the best reconstructable order -- page_number,
   then mTop notes, entry_heading, then the logical text flow; side notes may
   follow the main_text they annotate.
6. For each region give a brief (~20 word) summary for identification, and the
   dominant language: "de", "fr", "la" or "es".

Respond ONLY with a JSON array:
[
    {
        "region_index": 0,
        "region_type": "page_number",
        "has_text": true,
        "summary": "Folio number '2r' in top right corner",
        "position": "top right",
        "marginal_position": null,
        "related_entry": null,
        "bbox": [5, 870, 60, 980]
    },
    {
        "region_index": 1,
        "region_type": "marginal_note",
        "has_text": true,
        "summary": "Short note about a temperature observation, left margin",
        "position": "left margin",
        "marginal_position": "left",
        "related_entry": "12",
        "bbox": [180, 20, 340, 120]
    },
    {
        "region_index": 2,
        "region_type": "main_text",
        "has_text": true,
        "summary": "50) am 31 Oct. wahrer Zeit im Mittag -- main entry prose",
        "position": "main body",
        "marginal_position": null,
        "related_entry": "50",
        "bbox": [55, 90, 290, 910]
    }
]
"""

VALID_MARGINAL = {"left", "right", "mTop", "mBottom", "opposite", "inline"}


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def detect_regions(
    client: genai.Client,
    image_path: str | Path,
    model_id: str,
    thinking_level: str = "medium",
    *,
    page_bytes: tuple[bytes, str] | None = None,
) -> List[Dict[str, Any]]:
    """Detect regions on a Humboldt journal page image.

    ``page_bytes`` is the pre-encoded ``(jpeg, mime)`` for the page; when given the
    page is not re-read/re-encoded (the pipeline encodes it once per page).
    """
    image_bytes, mime = page_bytes if page_bytes else page_image_bytes(image_path)

    regions = generate_json(
        client, model_id, REGION_DETECTION_PROMPT,
        thinking_level=thinking_level,
        images=[(image_bytes, mime)],
        default=[],
        stage="region_detection",
    )
    if not isinstance(regions, list):
        regions = []

    return _normalise(regions)


def _normalise(regions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Re-index sequentially and clean up marginal_position."""
    for idx, region in enumerate(regions):
        region["region_index"] = idx
        mp = region.get("marginal_position")
        if not mp and region.get("region_type") == "marginal_note":
            pos = (region.get("position") or "").lower()
            if "left" in pos:
                region["marginal_position"] = "left"
            elif "right" in pos:
                region["marginal_position"] = "right"
            elif "top" in pos:
                region["marginal_position"] = "mTop"
            elif "bottom" in pos:
                region["marginal_position"] = "mBottom"
            elif "opposite" in pos or "bleed" in pos:
                region["marginal_position"] = "opposite"
        elif mp and mp not in VALID_MARGINAL:
            region["marginal_position"] = None
        if region.get("marginal_position") == "opposite":
            region["has_text"] = False
    return regions

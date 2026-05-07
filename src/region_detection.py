"""
Region Detection (Step 1) – Humboldt Journal Edition
=====================================================
Detects and classifies distinct regions on a page of Alexander von Humboldt's
handwritten scientific journal.

Humboldt's pages are notoriously complex:
- Numbered entries (N. 50, 51, 52...) that may span multiple topics
- Main text interspersed with calculations, coordinates, and tables
- Marginal notes on left and right sides, sometimes rotated
- Crossed-out passages with corrections above or beside them
- Pen sketches of landscape profiles, instruments, geological features
- Multilingual content switching freely between German, French, Latin
- Non-linear reading order requiring editorial judgment
"""

import base64
import io
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image
from google import genai
from google.genai import types

from .json_utils import parse_json_robust

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Image utilities
# ---------------------------------------------------------------------------

def load_image_as_base64(image_path: str | Path) -> tuple[str, str]:
    """Load an image file and return (base64_string, mime_type)."""
    with Image.open(image_path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        raw = buf.getvalue()
    return base64.b64encode(raw).decode("utf-8"), "image/jpeg"


# ---------------------------------------------------------------------------
# Prompt – tailored to Humboldt's journal structure
# ---------------------------------------------------------------------------

REGION_DETECTION_PROMPT = """\
You are a specialist in Alexander von Humboldt's handwritten scientific journals. You are examining a page from his field journals, which features extremely
complex, heterogeneous layouts with many co-existing text layers.

CHARACTERISTICS OF HUMBOLDT'S AMERICAN JOURNAL PAGES:
- Entries are numbered (e.g. "50)", "N. 9-11", "N. 50-52") – one page may span
  multiple entries or continue one from the previous page.
- Main text in German Kurrentschrift with frequent switches to French, Latin
  (scientific nomenclature), and Spanish.
- Margins contain notes in a different hand-size, sometimes rotated 90°,
  sometimes at top or bottom of the page, sometimes on the opposite side of the
  leaf (visible when the page is turned). These are important content, not
  decorative – they are Humboldt's own additions made at a different time.
- Astronomical/geodetic calculations appear as columns of numbers with ° ' "
  symbols, often in structured tables.
- Pen sketches of mountain profiles, river courses, plant/animal diagrams appear
  inline or at margins.
- Individual words or short phrases are struck through (corrected); whole
  paragraphs may also be struck through (deleted).
- Reading order is NOT always top-to-bottom: marginalia relate to specific
  passages, insertions are possible.

REGION TYPES TO DETECT:
- entry_heading: Numbered entry header (e.g. "N. 50-52.", "51)", "9)")
- main_text: Primary journal prose within an entry (the body text column)
- marginal_note: Any text Humboldt wrote in the margins of THIS page —
  left margin, right margin, top margin, bottom margin. These are genuine content
  regions. For EACH marginal_note, record its position: "left", "right", "mTop",
  or "mBottom".
  **OPPOSITE-FOLIO BLEEDTHROUGH**: If text is faintly visible at the page edge
  because it bleeds through from the reverse side of the leaf or the facing folio
  (often appears faint, mirrored, or partially cut off), classify it as
  marginal_note with marginal_position "opposite" and has_text: false. This is
  ghost text — NOT readable content, NOT to be transcribed.
- pasted_slip: A separate piece of paper physically pasted onto the page.
  Visually distinct: often slightly raised, sometimes at an angle, with its own
  ink. Contains its own separate text.
- calculation: Mathematical/astronomical computation blocks
- observation_table: Structured observational data (times, angles, measurements)
- sketch: Hand-drawn illustration, landscape profile, or diagram
- crossed_out: Large struck-through sections (multiple continuous lines or a
  whole block struck through as a unit — a deletion).
  Do NOT use for individual struck-through words; those are handled inline.
- bibliographic_ref: Citation of a publication, atlas, or other work
- coordinates: Geographic coordinate notations (latitude, longitude)
- instrument_list: Lists of scientific instruments (often with prices)
- page_number: Folio number (usually in top corner, e.g. "2r", "67")
- catch_phrase: Catchword at page bottom for continuation

CRITICAL RULES:

1. MARGINAL NOTES ARE IMPORTANT CONTENT: Every note Humboldt physically wrote in
   the margins of this page must be detected as a "marginal_note" region with
   marginal_position "left", "right", "mTop", or "mBottom". Do NOT merge them
   into main_text or ignore them.
   OPPOSITE BLEEDTHROUGH IS DIFFERENT: If text from the reverse/facing folio
   bleeds faintly through the paper (appears ghost-like, mirrored, or cut off at
   the edge), mark it as marginal_note with marginal_position "opposite" and
   has_text: false. It will NOT be transcribed.

2. PASTED SLIPS: If you see a piece of paper that appears to be glued onto the
   page (different paper texture, slightly raised edge, sometimes at an angle),
   classify it as "pasted_slip".

3. MERGE SMALL FRAGMENTS: Do NOT detect individual enumeration numbers ("50)",
   "51)"), single words, or short labels as standalone regions. Include them in
   the larger region they belong to. Each region should be a meaningful block.

4. BOUNDING BOXES: Make bboxes generous — slightly too large is better than
   cutting off text. Add ~10 units of margin. The bbox for a marginal note
   should tightly contain the marginal text.

5. COHERENT BLOCKS: A continuous main_text passage should be ONE region, not
   fragmented. Only split at genuine type changes.

6. READING ORDER: Return regions in the best reconstructable reading order.
   Start with page_number, then mTop marginal notes, then entry_heading, then
   follow the logical text flow. Left/right marginal notes can come after the
   main_text region they annotate.

7. For each region provide a brief summary (~20 words) for identification.

8. Identify the language: "de" (German), "fr" (French), "la" (Latin), "es" (Spanish).

9. EACH region must have a bounding box: [y_min, x_min, y_max, x_max], values
   0–1000 relative to the full image (0=top/left, 1000=bottom/right).

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
        "summary": "Short note about temperature observation, left margin",
        "position": "left margin",
        "marginal_position": "left",
        "related_entry": "12",
        "bbox": [180, 20, 340, 120]
    },
    {
        "region_index": 2,
        "region_type": "pasted_slip",
        "has_text": true,
        "summary": "Small pasted slip with astronomical calculation",
        "position": "center, pasted over main text",
        "marginal_position": null,
        "related_entry": "13",
        "bbox": [410, 150, 520, 700]
    },
    {
        "region_index": 3,
        "region_type": "main_text",
        "has_text": true,
        "summary": "50) am 31 Oct. wahrer Zeit im Mittag — main entry prose",
        "position": "main body",
        "marginal_position": null,
        "related_entry": "50",
        "bbox": [55, 90, 290, 910]
    },
    {
        "region_index": 4,
        "region_type": "sketch",
        "has_text": false,
        "summary": "Pen sketch of coastal mountain profile",
        "position": "bottom",
        "marginal_position": null,
        "related_entry": "52",
        "bbox": [840, 190, 980, 810]
    }
]
"""


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def detect_regions(
    client: genai.Client,
    image_path: str | Path,
    model_id: str,
    thinking_level: str = "medium",
) -> List[Dict[str, Any]]:
    """
    Detect regions on a Humboldt journal page image.
    """
    image_data, mime_type = load_image_as_base64(image_path)
    image_bytes = base64.b64decode(image_data)

    max_attempts = 3
    regions: List[Dict[str, Any]] = []

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=[
                    types.Content(
                        parts=[
                            types.Part(text=REGION_DETECTION_PROMPT),
                            types.Part(
                                inline_data=types.Blob(
                                    mime_type=mime_type,
                                    data=image_bytes,
                                )
                            ),
                        ]
                    )
                ],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
                    response_mime_type="application/json",
                ),
            )

            regions = parse_json_robust(response.text)
            if not isinstance(regions, list):
                regions = []
            if regions:
                break

        except json.JSONDecodeError as exc:
            logger.error("JSON error in region detection (attempt %d/%d): %s",
                         attempt, max_attempts, exc)
        except Exception as exc:
            logger.error("Region detection error (attempt %d/%d): %s",
                         attempt, max_attempts, exc)

    # Re-index sequentially and normalise marginal_position
    VALID_MARGINAL = {"left", "right", "mTop", "mBottom", "opposite", "inline"}
    for idx, region in enumerate(regions):
        region["region_index"] = idx
        # Normalise marginal_position from position field if not explicit
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
        # Opposite bleedthrough must never be marked as having readable text
        if region.get("marginal_position") == "opposite":
            region["has_text"] = False

    return regions

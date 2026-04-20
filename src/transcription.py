"""
Transcription / Description (Step 2) – Humboldt Journal Edition
================================================================
Transcribes text from Humboldt's handwritten journal pages using Gemini
multimodal models. Handles:
- German Kurrentschrift with Humboldt's personal abbreviations
- French passages (scientific terminology, references)
- Latin citations (book titles, species names)
- Astronomical notation (degrees °, minutes ', seconds ")
- Crossed-out text reconstruction
- Uncertain readings marked with [?]
- Editorial conventions for scholarly transcription
"""

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from google import genai
from google.genai import types

from .json_utils import parse_json_robust
from .models import Region
from .region_detection import load_image_as_base64

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt – scholarly transcription of Humboldt's journal
# ---------------------------------------------------------------------------

TRANSCRIPTION_PROMPT = """\
You are one of the world's leading experts on Alexander von Humboldt's handwriting
(Kurrentschrift) and his scientific journal conventions. You have decades of experience
transcribing his Amerikanische Reisetagebücher for the Berlin-Brandenburgische
Akademie der Wissenschaften (edition humboldt digital).

You are given a page image from Humboldt's American travel journal (Venezuela, 1799–1804)
and a list of detected regions. For EACH region, produce an accurate scholarly transcription.

HUMBOLDT'S HANDWRITING CHARACTERISTICS (AMERICAN JOURNALS):
- German Kurrentschrift, fluid and hasty; word spacing inconsistent
- Common German abbreviations: "d." = der/die/das, "u." = und, "v." = von,
  "Th." = Theil, "Anm." = Anmerkung, "s." = siehe, "vgl." = vergleiche
- French in Latin script (often easier): species names, references, scientific terms
- Latin in Latin script: species binomials (e.g. "Croton cascarilla"), citations
- Spanish in Latin script: Venezuelan place names, colonial terms (e.g. "Llanos",
  "Cumaná", "Maracaibo", "Misioneros", "Alcalde", "Corregidor")
- Indigenous place/group names may appear in any script
- Degree symbols ° ′ ″ used extensively for geographic coordinates and angles
- Astronomical notation: h (hours), ' (minutes), " (seconds)
- Alchemical symbols: ☉ (Sonne/Sun), ☿ (Quecksilber/Mercury), ♂ (Eisen/Iron)
- Superscript small letters for abbreviations
- Marginal notes often in smaller, more compressed script than main text
- Pasted slips may be in a different ink or paper

TRANSCRIPTION RULES – FOLLOW EXACTLY:

1. PRESERVE original spelling:
   - "Küste" as written, "Moqueur" not "Moqueur", etc.
   - Historical variants: "sey" / "sei", "Theil" / "Teil", "giebt" / "gibt"
   - Spanish as written: "Cumana" or "Cumaná", whichever appears

2. RESOLVE long s (ſ) → 's'; resolve ligatures where clearly identifiable

3. USE modern umlauts (ä ö ü) unless the original clearly writes ae/oe/ue

4. MARK uncertain readings with [?]:
   - A single unclear word: "Salz[?]burg"
   - Completely illegible: "[?]"
   - Partially illegible: "Mon[?]te"

5. INLINE EDITORIAL MARKUP:
   - STRUCK-THROUGH words/phrases (correction strikethrough): ~~text~~
     Example: "Die Höhe beträgt ~~120~~ 135 Toisen"
   - UNDERLINED words: <u>text</u>
     Example: "besonders <u>wichtig</u> für die Beobachtung"
   - Do NOT use ~~text~~ for usage-mark regions; in those, the text is legible
     despite the diagonal lines, so transcribe it normally.

6. PASTED SLIPS (region_type = "pasted_slip"):
   Transcribe the content of the slip as it appears. Note in editorial_note
   whether the slip appears to be in Humboldt's hand or another hand, and
   whether it overlaps/covers any underlying main text.

7. USAGE MARKS (region_type = "usage_mark"):
   Transcribe the underlying text NORMALLY (do not use ~~strikethrough~~).
   The diagonal marks are editorial provenance markers, not text deletions.
   In editorial_note, note: "Passage marked with Erledigt-Strich (used in
   later publication)" and describe the extent of the mark.

8. MARGINAL NOTES (region_type = "marginal_note"):
   Transcribe fully. In editorial_note, state the marginal position (left,
   right, top, bottom, opposite) and whether the note appears to be a later
   addition (different ink, different pen width, etc.).
   Marginal notes are SEPARATE from the main text — do NOT duplicate any
   content from adjacent main_text regions.

9. PRESERVE line breaks within a region using \\n

10. TABLES/CALCULATIONS: Preserve columnar structure in table_data.cells

11. IDENTIFY languages: "de" (German), "fr" (French), "la" (Latin), "es" (Spanish)

12. AVOID DUPLICATION: A passage must appear in EXACTLY ONE region's transcription.
    If a marginal note annotates a main_text passage, transcribe the note in the
    marginal_note region and the annotated passage in the main_text region, but
    do NOT copy the note text into the main_text or vice versa.

FOR VISUAL REGIONS (sketch):
- Describe WHAT is depicted: landscape profile, animal/plant diagram, coastline, etc.
- Note any labels or text within the sketch
- Describe technique (pen, pencil, pencil wash)

DETECTED REGIONS:
{regions_json}

Respond ONLY with a JSON array matching each region (same order, same indices):
[
    {{
        "region_index": 0,
        "region_type": "entry_heading",
        "is_visual": false,
        "content": "N. 9-11.",
        "table_data": null,
        "languages": ["de"],
        "editorial_note": null,
        "uncertain_readings": [],
        "crossed_out_text": null,
        "position": "top center",
        "marginal_position": null,
        "writing_layer": "primary",
        "is_pasted_slip": false,
        "is_usage_marked": false
    }},
    {{
        "region_index": 1,
        "region_type": "marginal_note",
        "is_visual": false,
        "content": "Chaymas haben keine Worte für Vergangenheit.",
        "table_data": null,
        "languages": ["de"],
        "editorial_note": "Left margin, later addition in narrower pen, possibly different ink",
        "uncertain_readings": [],
        "crossed_out_text": null,
        "position": "left margin",
        "marginal_position": "left",
        "writing_layer": "later_addition",
        "is_pasted_slip": false,
        "is_usage_marked": false
    }},
    {{
        "region_index": 2,
        "region_type": "pasted_slip",
        "is_visual": false,
        "content": "Breite des Ortes nach Beobachtung\\n10° 27' 52\" N.",
        "table_data": null,
        "languages": ["de"],
        "editorial_note": "Small slip pasted over the lower quarter of the page; Humboldt's hand",
        "uncertain_readings": [],
        "crossed_out_text": null,
        "position": "center, pasted",
        "marginal_position": null,
        "writing_layer": "later_addition",
        "is_pasted_slip": true,
        "is_usage_marked": false
    }},
    {{
        "region_index": 3,
        "region_type": "usage_mark",
        "is_visual": false,
        "content": "Die Chaymas zeigen bei dem Tode...",
        "table_data": null,
        "languages": ["de"],
        "editorial_note": "Passage covered by long diagonal Erledigt-Strich from top-left to bottom-right; text remains legible",
        "uncertain_readings": [],
        "crossed_out_text": null,
        "position": "main body",
        "marginal_position": null,
        "writing_layer": "primary",
        "is_pasted_slip": false,
        "is_usage_marked": true
    }}
]

CRITICAL: For Humboldt's difficult handwriting, an uncertain reading marked [?]
is MUCH better than a wrong guess. Accuracy matters more than completeness.
Do NOT duplicate text between regions.
"""


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def transcribe_regions(
    client: genai.Client,
    image_path: str | Path,
    detected_regions: List[Dict[str, Any]],
    model_id: str,
    thinking_level: str = "medium",
) -> List[Region]:
    """
    Transcribe or describe each detected region on a Humboldt journal page.

    Uses a higher thinking level than default because Humboldt's handwriting
    is exceptionally difficult and requires careful analysis.
    """
    if not detected_regions:
        return []

    image_data, mime_type = load_image_as_base64(image_path)
    image_bytes = base64.b64decode(image_data)

    regions_json = json.dumps(detected_regions, ensure_ascii=False, indent=2)
    prompt = TRANSCRIPTION_PROMPT.format(regions_json=regions_json)

    max_attempts = 3
    data: List[Dict] = []

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=[
                    types.Content(
                        parts=[
                            types.Part(text=prompt),
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

            data = parse_json_robust(response.text)
            if not isinstance(data, list):
                data = []
            if data:
                break

        except json.JSONDecodeError as exc:
            logger.error("JSON error in transcription (attempt %d/%d): %s",
                         attempt, max_attempts, exc)
        except Exception as exc:
            logger.error("Transcription error (attempt %d/%d): %s",
                         attempt, max_attempts, exc)

    # Build Region objects
    regions: List[Region] = []
    data_by_index = {item.get("region_index", i): item for i, item in enumerate(data)}

    for det in detected_regions:
        idx = det["region_index"]
        transcribed = data_by_index.get(idx, {})

        region_type = transcribed.get("region_type", det.get("region_type", "main_text"))
        content = transcribed.get("content", det.get("summary", ""))
        is_visual = transcribed.get("is_visual", not det.get("has_text", True))
        table_data = transcribed.get("table_data")
        languages = transcribed.get("languages", [])
        editorial_note = transcribed.get("editorial_note")
        position = transcribed.get("position", det.get("position"))
        uncertain_readings = transcribed.get("uncertain_readings", [])
        crossed_out_text = transcribed.get("crossed_out_text")
        related_to_entry = transcribed.get("related_entry") or det.get("related_entry")
        bbox = det.get("bbox")  # bbox comes from region detection, not transcription
        # New fields
        marginal_position = transcribed.get("marginal_position") or det.get("marginal_position")
        writing_layer = transcribed.get("writing_layer")
        is_pasted_slip = bool(transcribed.get("is_pasted_slip", False))
        is_usage_marked = bool(transcribed.get("is_usage_marked",
                                               region_type == "usage_mark"))

        regions.append(Region(
            region_type=region_type,
            region_index=idx,
            content=content,
            is_visual=is_visual,
            table_data=table_data,
            languages=languages,
            editorial_note=editorial_note,
            position=position,
            uncertain_readings=uncertain_readings,
            crossed_out_text=crossed_out_text,
            related_to_entry=related_to_entry,
            bbox=bbox,
            marginal_position=marginal_position,
            writing_layer=writing_layer,
            is_pasted_slip=is_pasted_slip,
            is_usage_marked=is_usage_marked,
        ))

    regions.sort(key=lambda r: r.region_index)
    return regions

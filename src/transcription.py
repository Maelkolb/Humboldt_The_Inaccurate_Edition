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
transcribing and editing his travel journals

You are given a page image from Humboldt's travel journal.
and a list of detected regions. For EACH region, produce an accurate scholarly transcription.

HUMBOLDT'S HANDWRITING CHARACTERISTICS (AMERICAN JOURNALS):
- German Kurrentschrift, fluid and hasty; word spacing inconsistent
- Common German abbreviations: "d." = der/die/das, "u." = und, "v." = von,
  "Th." = Theil, "Anm." = Anmerkung, "s." = siehe, "vgl." = vergleiche
- French in Latin script (often easier): species names, references, scientific terms
- Latin in Latin script: species binomials (e.g. "Croton cascarilla"), citations
- Spanish: place names, colonial terms (e.g. "Llanos",
  "Cumaná", "Maracaibo", "Misioneros", "Alcalde", "Corregidor")
- Indigenous place/group names may appear in any script
- Degree symbols ° ′ ″ used extensively for geographic coordinates and angles
- Astronomical notation: h (hours), ' (minutes), " (seconds)
- Alchemical symbols: ☉ (Sonne/Sun), ☿ (Quecksilber/Mercury), ♂ (Eisen/Iron)
- Superscript small letters for abbreviations

TRANSCRIPTION RULES – FOLLOW EXACTLY:

1. PRESERVE original spelling:

2. MARK uncertain readings with [?]:
   - A single unclear word: "Salzburg und [?]"
   - Completely illegible: "[?]"
   - Partially illegible: "Mon[?]te"
3. INLINE EDITORIAL MARKUP:
   - STRUCK-THROUGH words/phrases (correction strikethrough): ~~text~~
     Example: "Die Höhe beträgt ~~120~~ 135 Toisen"
   - UNDERLINED words: <u>text</u>
     Example: "besonders <u>wichtig</u> für die Beobachtung"

4. MARGINAL NOTES (region_type = "marginal_note"):
   TWO DISTINCT CASES — treat them very differently:

   A) MARGIN NOTES ON THIS PAGE (marginal_position = "left", "right", "mTop", "mBottom"):
      Text Humboldt physically wrote in the margin of this page. Transcribe fully.
      In editorial_note, state the marginal position and whether it is a later
      addition (different ink, different pen width, etc.)

   B) OPPOSITE-FOLIO BLEEDTHROUGH (marginal_position = "opposite"):
      Text from the OTHER side of the leaf or the facing folio that is faintly
      visible through the paper. It is NOT meant to be read from this side and
      is often mirrored, very faint, or fragmentary. Do NOT transcribe it.
      Set content: "" (empty string).
      In editorial_note, write: "Bleedthrough from opposite folio — not transcribed."

   Marginal notes are SEPARATE from the main text — do NOT duplicate any
   content from adjacent main_text regions.

5. PRESERVE line breaks within a region using \\n

6. TABLES (observation_table, instrument_list):
   ALWAYS provide BOTH fields — even when the table is difficult to read:
   - content: verbatim transcription of every visible character, line breaks as \\n.
     This is shown as fallback if cells cannot be rendered as a table.
   - table_data: {{"cells": [["Col1","Col2",...], ["val","val",...], ...], "caption": "..."}}
     Row 0 must be column headers. If column boundaries are unclear, use one column.
     Example for a typical Humboldt observation table:
     {{"cells": [["Uhr", "Min.", "Sec.", "Grad"], ["6", "42", "15", "78° 20'"], ["6", "44", "03", "78° 21'"]], "caption": "Winkel-Messung"}}
   Never return table_data with an empty cells array. If cells are truly unreadable,
   omit table_data entirely (null) and put everything in content.

7. IDENTIFY languages: "de" (German), "fr" (French), "la" (Latin), "es" (Spanish)

8. AVOID DUPLICATION

9. FOR VISUAL REGIONS (sketch):
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
        "is_pasted_slip": false
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
        "is_pasted_slip": false
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
        "is_pasted_slip": true
    }},
    {{
        "region_index": 3,
        "region_type": "observation_table",
        "is_visual": false,
        "content": "Uhr  Min.  Sec.  Grad\\n6    42    15    78° 20'\\n6    44    03    78° 21'",
        "table_data": {{"cells": [["Uhr", "Min.", "Sec.", "Grad"], ["6", "42", "15", "78° 20'"], ["6", "44", "03", "78° 21'"]], "caption": "Winkel-Messung"}},
        "languages": ["de"],
        "editorial_note": "Angular measurement table, three observations",
        "uncertain_readings": [],
        "crossed_out_text": null,
        "position": "lower center",
        "marginal_position": null,
        "writing_layer": "primary",
        "is_pasted_slip": false
    }},
    {{
        "region_index": 4,
        "region_type": "marginal_note",
        "is_visual": false,
        "content": "",
        "table_data": null,
        "languages": [],
        "editorial_note": "Bleedthrough from opposite folio — not transcribed.",
        "uncertain_readings": [],
        "crossed_out_text": null,
        "position": "right edge",
        "marginal_position": "opposite",
        "writing_layer": null,
        "is_pasted_slip": false
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
        ))

    regions.sort(key=lambda r: r.region_index)
    return regions

"""
Consistency Check (Step 2.5) – Humboldt Journal Edition
========================================================
LLM-powered post-transcription quality gate that runs after all regions have
been transcribed for a page.

Problems detected and fixed:
1. DUPLICATE LINES – text that appears verbatim (or near-verbatim) in two or
   more regions, because the region margins included lines of other regions.
2. MAIN-TEXT CONTAMINATION – main_text regions that incorporate marginal or
   pasted-slip text that should have been kept separate.
3. CONTENT GAPS – a region_detection bbox suggests content that is absent from
   the transcription (empty content when has_text was true).
4. LANGUAGE INCONSISTENCIES – a "de" region that is clearly French/Latin/Spanish
   or vice versa.
5. UNCERTAIN-READING RESOLUTION (NEW) – when the page image is available, the
   model is shown every word marked ``[?]`` and asked to attempt a corrected
   reading directly from the image. Resolved words are replaced inline; the
   ``uncertain_readings`` list is updated accordingly and a note is appended
   to ``editorial_note`` for traceability.

The check uses a single structured LLM call (with the image, when supplied)
that returns a list of issues and corrected region transcriptions. Corrections
are applied in-place.
"""

import base64
import dataclasses
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai import types

from .json_utils import parse_json_robust
from .models import Region

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Uncertain-reading extraction
# ---------------------------------------------------------------------------

# Matches:   word[?]    Mon[?]te    [?]   etc.
# Group 1 = the (possibly empty) word stem immediately preceding "[?]"
_UNCERTAIN_RE = re.compile(r"(\w*)\[\?\]")


def _extract_uncertain_occurrences(regions: List[Region]) -> List[Dict[str, Any]]:
    """
    Walk every region.content and return one entry per ``[?]`` occurrence.

    Each entry carries:
      - region_index
      - stem    : the partial reading immediately before "[?]" (may be "")
      - context : ~60 chars of surrounding text, with the marker inline
      - occurrence_idx : 0-based index of this [?] within the region
    """
    out: List[Dict[str, Any]] = []
    for r in regions:
        if not r.content:
            continue
        for occ_idx, m in enumerate(_UNCERTAIN_RE.finditer(r.content)):
            stem = m.group(1) or ""
            start = max(0, m.start() - 30)
            end = min(len(r.content), m.end() + 30)
            ctx = r.content[start:end].replace("\n", " ")
            out.append({
                "region_index": r.region_index,
                "occurrence_idx": occ_idx,
                "stem": stem,
                "context": ctx,
                "marker": f"{stem}[?]",
            })
    return out


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CONSISTENCY_CHECK_PROMPT_WITH_IMAGE = """\
You are a scholarly editor reviewing the output of an automated transcription
pipeline applied to a page from Alexander von Humboldt's travel journal.

You are given:
  1. The page image (attached).
  2. The full list of regions detected on this page, with the transcription
     produced for each region (JSON below).
  3. A separate list of every uncertain reading "[?]" that the transcription
     model emitted on this page, with surrounding context.

YOUR JOB has two parts.

==============================================================================
PART A — STRUCTURAL CONSISTENCY (existing, do not skip)
==============================================================================

Detect and fix these four structural problems in the transcriptions:

1. DUPLICATE TEXT: The same phrase, sentence, or passage appears in TWO or more
   regions. Usually this happens when a marginal note's text was accidentally
   copied into the adjacent main_text region. Identify the correct owner and
   remove the duplicate from the other region(s).

2. CONTAMINATED MAIN TEXT: A main_text region contains content that clearly
   belongs to a marginal_note or pasted_slip. Move it.

3. EMPTY REGIONS: A region has empty or near-empty (< 5 char) content when it
   should contain text (is_visual=false). Exception: marginal notes on the
   opposite folio (bleedthrough) are intentionally empty.

4. LANGUAGE MISMATCH: A region is labelled ["de"] but the content is clearly
   French / Latin / Spanish / English (or vice versa). Correct the languages
   list.

==============================================================================
PART B — UNCERTAIN-READING RESOLUTION (NEW — this is where the image matters)
==============================================================================

The transcription model marked the following words with "[?]" because it could
not read them confidently. For EACH of these uncertain readings:

  - Look at the actual ink strokes in the image, in the relevant region.
  - If you can read the word with high confidence, REPLACE the "[?]" with
    your corrected reading IN THE REGION CONTENT and remove that entry from
    the region's ``uncertain_readings`` list.
  - If the word is partially legible, supply only what you can read and KEEP
    the "[?]" marker (e.g. "Mon[?]te" → "Monte[?]" stays if only "Mont…te"
    is legible).
  - If you cannot read it any better than the transcription model did, LEAVE
    THE "[?]" UNCHANGED. Do not invent. Honest "[?]" is better than a wrong
    confident reading.

Important rules for Part B:
  - Preserve the rest of the region's content exactly. Do NOT rewrite parts
    that were not marked "[?]".
  - When you resolve a "[?]", append a short note to the region's
    editorial_note, e.g. "Resolved [?] → 'Monte' from image". Do NOT replace
    the existing editorial_note; append.
  - When emitting "uncertain_readings", list only the words still containing
    "[?]" after your fixes. Drop the resolved ones.

UNCERTAIN READINGS ON THIS PAGE:
{uncertain_json}

==============================================================================
TRANSCRIBED REGIONS (JSON):
==============================================================================
{regions_json}

==============================================================================
RESPONSE FORMAT
==============================================================================

Respond ONLY with a JSON object of the form:

{{
  "issues_found": [
    {{
      "issue_type": "duplicate_text|contamination|empty_region|language_mismatch|uncertain_resolved",
      "region_indices": [0, 3],
      "description": "Human-readable description of the problem",
      "severity": "error|warning"
    }}
  ],
  "corrected_regions": [
    {{
      "region_index": 0,
      "content": "corrected content (only include regions that actually changed)",
      "languages": ["de"],
      "uncertain_readings": ["list of words still marked [?] after your fix"],
      "editorial_note": "fragment to APPEND to existing note (not replace)"
    }}
  ]
}}

If no issues are found AND no uncertain readings can be resolved, return:
{{"issues_found": [], "corrected_regions": []}}

RULES (for the whole response):
  - Only include regions in "corrected_regions" if something actually changed.
  - Do NOT change bboxes, region_type, region_index, table_data, position,
    marginal_position, writing_layer, or is_pasted_slip.
  - When removing duplicate text, keep it in the region where it semantically
    belongs (marginal text → marginal_note, prose → main_text).
  - For Part B, use "issue_type": "uncertain_resolved" and severity "warning"
    if you resolved one or more [?] in that region.
  - If in doubt about a duplicate, flag it as a warning but do NOT alter content.
"""


CONSISTENCY_CHECK_PROMPT_TEXT_ONLY = """\
You are a scholarly editor reviewing the output of an automated transcription
pipeline applied to Alexander von Humboldt's travel journal.

The pipeline detected regions on the page and transcribed each one. Your task:
detect and fix four specific quality problems in the transcriptions below.

PROBLEMS TO DETECT AND FIX:

1. DUPLICATE TEXT: The same phrase, sentence, or passage appears in TWO or more
   regions. Usually this happens when a marginal note's text was accidentally
   copied into the adjacent main_text region. Identify the correct owner and
   remove the duplicate from the other region(s).

2. CONTAMINATED MAIN TEXT: A main_text region contains content that clearly
   belongs to a marginal_note or pasted_slip. Flag and move it.

3. EMPTY REGIONS: A region has empty or near-empty (< 5 char) content when it
   should contain text (is_visual=false). Exception: marginal notes on opposite
   folio (bleedthrough) are intentionally empty.

4. LANGUAGE MISMATCH: A region is labelled ["de"] but the content is clearly
   French / Latin / Spanish / English (or vice versa). Correct the languages
   list.

(Without the page image, do NOT attempt to resolve "[?]" uncertain readings;
leave them as-is.)

TRANSCRIBED REGIONS (JSON):
{regions_json}

Respond with a JSON object:
{{
  "issues_found": [
    {{
      "issue_type": "duplicate_text|contamination|empty_region|language_mismatch",
      "region_indices": [0, 3],
      "description": "Human-readable description of the problem",
      "severity": "error|warning"
    }}
  ],
  "corrected_regions": [
    {{
      "region_index": 0,
      "content": "corrected content (only include regions that changed)",
      "languages": ["de"],
      "editorial_note": "fragment to APPEND to existing note"
    }}
  ]
}}

If no issues are found, return:
{{"issues_found": [], "corrected_regions": []}}

RULES:
  - Only include regions in "corrected_regions" if content actually changed.
  - Do NOT change bboxes, region_type, region_index.
  - If in doubt about a duplicate, flag it as a warning but do NOT alter content.
"""


# ---------------------------------------------------------------------------
# Image helper (lazy-import-friendly local copy)
# ---------------------------------------------------------------------------

def _load_image_bytes(image_path: str | Path) -> Tuple[bytes, str]:
    """Return (raw bytes, mime_type) for the page image."""
    # Use the same loader as region_detection / transcription so we stay
    # consistent (JPEG re-encoded, RGB, quality 95).
    from .region_detection import load_image_as_base64
    image_data, mime_type = load_image_as_base64(image_path)
    return base64.b64decode(image_data), mime_type


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def check_and_fix_regions(
    client: genai.Client,
    regions: List[Region],
    model_id: str,
    thinking_level: str = "low",
    image_path: Optional[str | Path] = None,
) -> Tuple[List[Region], List[Dict[str, Any]]]:
    """
    Run the consistency check on a list of transcribed regions.

    When ``image_path`` is provided, the check additionally focuses on every
    word marked ``[?]`` by the transcription model and asks the LLM to attempt
    a corrected reading directly from the image.

    Args:
        client:         Gemini client.
        regions:        List of transcribed Region objects.
        model_id:       Gemini model ID for this stage.
        thinking_level: "none" | "low" | "medium" | "high".
        image_path:     Optional path to the page image. When supplied, the
                        LLM call is multimodal and Part B (uncertain-reading
                        resolution) is enabled.

    Returns:
        ``(corrected_regions, issues)`` — the same regions list with any
        corrections applied, plus a list of issue dicts for logging.
    """
    if not regions:
        return regions, []

    # ----- Snapshot pre-consistency state for EVERY region -----
    # This runs before any prompt/LLM work, so even if the LLM call fails or
    # finds no issues, every region's pre-consistency fields are set to the
    # exact content Gemini produced in Step 2. Downstream code (HTML viewer,
    # JSON inspection) can then compare "what Gemini said" vs. "what survived
    # the QA pass" for every region, regardless of whether it was modified.
    for r in regions:
        # Only overwrite if not already set (defensive: idempotent if the
        # consistency check is somehow run twice on the same regions).
        if r.content_pre_consistency is None:
            r.content_pre_consistency = r.content
        if r.uncertain_readings_pre_consistency is None:
            r.uncertain_readings_pre_consistency = list(r.uncertain_readings or [])

    # ----- Serialize regions for the prompt -----
    serialized = [
        {
            "region_index": r.region_index,
            "region_type": r.region_type,
            "is_visual": r.is_visual,
            "content": r.content,
            "languages": r.languages,
            "uncertain_readings": r.uncertain_readings,
            "position": r.position,
            "marginal_position": r.marginal_position,
            "is_pasted_slip": r.is_pasted_slip,
            "editorial_note": r.editorial_note,
            "bbox": r.bbox,
        }
        for r in regions
    ]
    regions_json = json.dumps(serialized, ensure_ascii=False, indent=2)

    use_image = image_path is not None
    if use_image:
        uncertain = _extract_uncertain_occurrences(regions)
        uncertain_json = json.dumps(uncertain, ensure_ascii=False, indent=2)
        prompt = CONSISTENCY_CHECK_PROMPT_WITH_IMAGE.format(
            regions_json=regions_json,
            uncertain_json=uncertain_json,
        )
        logger.debug(
            "Consistency check (multimodal): %d uncertain readings found.",
            len(uncertain),
        )
    else:
        prompt = CONSISTENCY_CHECK_PROMPT_TEXT_ONLY.format(
            regions_json=regions_json
        )

    # ----- LLM call (with retries) -----
    result: Dict[str, Any] = {}
    for attempt in range(1, 3):
        try:
            if use_image:
                image_bytes, mime_type = _load_image_bytes(image_path)
                contents = [
                    types.Content(parts=[
                        types.Part(text=prompt),
                        types.Part(inline_data=types.Blob(
                            mime_type=mime_type, data=image_bytes
                        )),
                    ])
                ]
            else:
                contents = prompt

            response = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_level=thinking_level
                    ),
                    response_mime_type="application/json",
                ),
            )
            result = parse_json_robust(response.text)
            if isinstance(result, dict):
                break
        except Exception as exc:
            logger.error(
                "Consistency check error (attempt %d/2): %s",
                attempt, exc,
            )

    if not isinstance(result, dict):
        result = {}

    issues: List[Dict] = result.get("issues_found", []) or []
    corrections: List[Dict] = result.get("corrected_regions", []) or []

    # ----- Log issues -----
    if issues:
        for issue in issues:
            severity = issue.get("severity", "error")
            itype    = issue.get("issue_type", "?")
            indices  = issue.get("region_indices", [])
            desc     = issue.get("description", "")
            in_corrections = any(
                c.get("region_index") in indices for c in corrections
            )
            if severity == "warning" or not in_corrections:
                logger.warning(
                    "Consistency [%s] regions %s: %s", itype, indices, desc
                )
            else:
                logger.info(
                    "Consistency fixed [%s] regions %s: %s",
                    itype, indices, desc,
                )

    # ----- Apply corrections in-place -----
    if corrections:
        correction_map: Dict[int, Dict] = {
            c["region_index"]: c for c in corrections if isinstance(c, dict)
        }
        out: List[Region] = []
        for region in regions:
            fix = correction_map.get(region.region_index)
            if not fix:
                out.append(region)
                continue

            new_content = fix.get("content", region.content)
            new_langs   = fix.get("languages", region.languages)

            # uncertain_readings: trust the model's updated list if given,
            # otherwise recompute from the new content
            if "uncertain_readings" in fix:
                new_unc = list(fix.get("uncertain_readings") or [])
            else:
                new_unc = [m.group(0) for m in _UNCERTAIN_RE.finditer(new_content or "")]

            # editorial_note: append rather than replace
            old_note = region.editorial_note or ""
            note_fragment = (fix.get("editorial_note") or "").strip()
            if note_fragment and note_fragment not in old_note:
                new_note = (old_note + " | " + note_fragment).strip(" |")
            else:
                new_note = old_note or None

            out.append(dataclasses.replace(
                region,
                content=new_content,
                languages=new_langs,
                uncertain_readings=new_unc,
                editorial_note=new_note or None,
            ))
            logger.info("  Applied correction to region %d", region.region_index)
        regions = out

    return regions, issues

"""
Consistency Check (Step 2.5) – Humboldt Journal Edition
========================================================
LLM-powered post-transcription quality gate that runs after all regions have
been transcribed for a page but BEFORE NER/Geocoding.

Problems detected and fixed:
1. DUPLICATE LINES – text that appears verbatim (or near-verbatim) in two or
   more regions, because the region margins included lines of other regions. 
2. MAIN-TEXT CONTAMINATION – main_text regions that incorporate marginal or
   pasted-slip text that should have been kept separate.
3. CONTENT GAPS – a region_detection bbox suggests content that is absent from
   the transcription (empty content when has_text was true).
4. LANGUAGE INCONSISTENCIES – a "de" region that is clearly French/Latin/Spanish
   or vice versa.
5. ENTRY-NUMBER INCONSISTENCIES – entry numbers that don't form a plausible
   sequence (e.g. 50, 51, 99 → likely a misread).

The check uses a single structured LLM call that returns a list of issues and
corrected region transcriptions. Corrections are applied in-place.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from .json_utils import parse_json_robust
from .models import Region

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

CONSISTENCY_CHECK_PROMPT = """\
You are a scholarly editor reviewing the output of an automated transcription
pipeline applied to Alexander von Humboldt's American travel journal
(Amerikanische Reisetagebücher, Venezuela 1799–1804).

The pipeline detected regions on the page and transcribed each one. Your task:
detect and fix five specific quality problems in the transcriptions below.

PROBLEMS TO DETECT AND FIX:

1. DUPLICATE TEXT: The same phrase, sentence, or passage appears in TWO or more
   regions. This usually happens when a marginal note's text is accidentally
   copied into the adjacent main_text region. Identify which region is the
   "correct owner" and remove the duplicate from the other region(s).

2. CONTAMINATED MAIN TEXT: A main_text region contains content that clearly
   belongs to a marginal_note or pasted_slip (e.g. an observation that should
   be in the margin appears mid-paragraph in the main text). Flag and move it.

3. EMPTY REGIONS: A region has an empty or near-empty "content" (less than 5
   characters) when it should contain text (is_visual=false). Note: if the
   region is genuinely empty in the image, keep it but note that.

4. LANGUAGE MISMATCH: A region is labelled ["de"] but the content is clearly
   French or Spanish (or vice versa). Correct the languages list.

5. IMPLAUSIBLE ENTRY SEQUENCE: Entry numbers across entry_heading and main_text
   regions (e.g. "50)", "51)", "99)") contain a number that is far out of
   sequence, suggesting a misread digit. Flag it for human review.

TRANSCRIBED REGIONS (JSON):
{regions_json}

Respond with a JSON object:
{{
    "issues_found": [
        {{
            "issue_type": "duplicate_text|contamination|empty_region|language_mismatch|entry_sequence",
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
            "editorial_note": "updated editorial note (append, don't replace)"
        }}
    ]
}}

If no issues are found, return:
{{
    "issues_found": [],
    "corrected_regions": []
}}

RULES:
- Only include regions in "corrected_regions" if their content actually changed.
- When removing duplicate text, keep it in the region where it semantically
  belongs (marginal note → marginal_note region, main prose → main_text).
- Do NOT rewrite or improve the transcription beyond fixing the listed problems.
- Do NOT change bboxes, region_type, or region_index.
- If in doubt about a duplicate, flag it as a warning but do NOT alter content.
"""


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def check_and_fix_regions(
    client: genai.Client,
    regions: List[Region],
    model_id: str,
    thinking_level: str = "low",
) -> tuple[List[Region], List[Dict[str, Any]]]:
    """
    Run consistency check on a list of transcribed Region objects.

    Returns:
        (corrected_regions, issues)  where issues is a list of issue dicts.
    """
    if not regions:
        return regions, []

    # Serialize only the fields needed for the check
    serialized = [
        {
            "region_index": r.region_index,
            "region_type": r.region_type,
            "is_visual": r.is_visual,
            "content": r.content,
            "languages": r.languages,
            "position": r.position,
            "marginal_position": r.marginal_position,
            "is_pasted_slip": r.is_pasted_slip,
            "is_usage_marked": r.is_usage_marked,
            "editorial_note": r.editorial_note,
        }
        for r in regions
    ]

    regions_json = json.dumps(serialized, ensure_ascii=False, indent=2)
    prompt = CONSISTENCY_CHECK_PROMPT.format(regions_json=regions_json)

    result: Dict[str, Any] = {}
    for attempt in range(1, 3):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
                    response_mime_type="application/json",
                ),
            )
            result = parse_json_robust(response.text)
            if isinstance(result, dict):
                break
        except Exception as exc:
            logger.error("Consistency check error (attempt %d/2): %s", attempt, exc)

    if not isinstance(result, dict):
        result = {}

    issues: List[Dict] = result.get("issues_found", [])
    corrections: List[Dict] = result.get("corrected_regions", [])

    if issues:
        for issue in issues:
            # Log all findings at INFO — these are expected, handled findings,
            # not Python errors. Only use WARNING for issues that couldn't be
            # auto-corrected and may need human review.
            severity = issue.get("severity", "error")
            itype    = issue.get("issue_type", "?")
            indices  = issue.get("region_indices", [])
            desc     = issue.get("description", "")
            in_corrections = any(
                c.get("region_index") in indices for c in corrections
            )
            if severity == "warning" or not in_corrections:
                logger.warning("Consistency [%s] regions %s: %s", itype, indices, desc)
            else:
                logger.info("Consistency fixed [%s] regions %s: %s", itype, indices, desc)

    # Apply corrections in-place
    if corrections:
        correction_map: Dict[int, Dict] = {c["region_index"]: c for c in corrections
                                            if isinstance(c, dict)}
        corrected = []
        for region in regions:
            fix = correction_map.get(region.region_index)
            if fix:
                new_content = fix.get("content", region.content)
                new_langs = fix.get("languages", region.languages)
                # Append to editorial note rather than replacing
                old_note = region.editorial_note or ""
                new_note_fragment = fix.get("editorial_note", "")
                if new_note_fragment and new_note_fragment not in old_note:
                    new_note = (old_note + " | " + new_note_fragment).strip(" |")
                else:
                    new_note = old_note or None
                import dataclasses
                region = dataclasses.replace(
                    region,
                    content=new_content,
                    languages=new_langs,
                    editorial_note=new_note or None,
                )
                logger.info("  Applied correction to region %d", region.region_index)
            corrected.append(region)
        regions = corrected

    return regions, issues

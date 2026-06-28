"""Whole-page layout pass over the merged regions — never rewrites wording, only
resolves cross-region issues: duplicate/overlapping lines (kept in the region
they belong to, emptied elsewhere), contamination, and opposite-folio
bleedthrough. May also fix a wrong language tag, set ``writing_layer``, or append
an editorial note. Each region keeps a pre-pass snapshot for auditing. Returns
``(regions, issues)``; the issues populate ``PageResult.consistency_issues``.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from google import genai

from .llm import generate_json
from .models import Region

logger = logging.getLogger(__name__)

ImageInput = Tuple[bytes, str]

_UNCERTAIN_RE = re.compile(r"\w*\[\?\]")
_WRITING_LAYERS = {"primary", "later_addition", "unknown"}


LAYOUT_PROMPT = """\
You are checking the LAYOUT of one page of Alexander von Humboldt's journal. Each
region already has its FINAL text — do NOT change any wording or spelling. Only
fix cross-region layout, by region:
  • DUPLICATE/OVERLAPPING line (same physical line in two regions, usually the
    last line of one reappearing as the first of the next): keep it in the region
    its bbox contains and set the other's "duplicate_of" to the keeper's index.
  • CONTAMINATION (a region's text belongs to a different region): "drop": true.
You may also fix a clearly wrong "languages" tag, set "writing_layer"
("primary"|"later_addition"|"unknown"), or append a short "editorial_note".
NEVER return a "content" field.

REGIONS (index, type, bbox [y_min,x_min,y_max,x_max] 0–1000, marginal_position, content):
{regions_json}

Based on the page above, respond ONLY with JSON:
{{"regions": [
  {{"region_index": 0, "drop": false, "duplicate_of": null,
    "languages": ["de"], "writing_layer": "primary", "editorial_note": null}}
]}}
Return one object per region, in the same order.
"""


def resolve_layout(
    client: genai.Client,
    regions: List[Region],
    model_id: str,
    thinking_level: str = "medium",
    *,
    page_bytes: Optional[ImageInput] = None,
    temperature: Optional[float] = None,
    cached_content: Optional[str] = None,
) -> Tuple[List[Region], List[Dict[str, Any]]]:
    """Resolve cross-region layout without rewriting any region's wording."""
    if not regions:
        return regions, []

    _snapshot_pre_layout(regions)

    prompt = LAYOUT_PROMPT.format(
        regions_json=_json([_serialize_region_layout(r) for r in regions])
    )

    result = generate_json(
        client, model_id, prompt,
        thinking_level=thinking_level,
        temperature=temperature,
        images=[page_bytes] if page_bytes else None,
        cached_content=cached_content,
        default={},
        max_attempts=2,
        stage="layout",
    )
    if not isinstance(result, dict):
        result = {}

    decisions: List[Dict] = result.get("regions") or []
    if not isinstance(decisions, list):
        decisions = []
    # Strip any content the model returned despite instructions — layout never
    # rewrites text; _apply_decisions then preserves each region's own content.
    for d in decisions:
        if isinstance(d, dict):
            d.pop("content", None)

    regions, issues = _apply_decisions(regions, decisions)
    _log_issues(issues)
    return regions, issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot_pre_layout(regions: List[Region]) -> None:
    """Record each region's pre-layout reading before this pass can empty it."""
    for r in regions:
        if r.content_pre_consistency is None:
            r.content_pre_consistency = (
                r.region_reading if r.region_reading is not None else r.content
            )
        if r.uncertain_readings_pre_consistency is None:
            r.uncertain_readings_pre_consistency = list(r.uncertain_readings or [])


def _serialize_region_layout(r: Region) -> Dict[str, Any]:
    return {
        "region_index": r.region_index,
        "region_type": r.region_type,
        "marginal_position": r.marginal_position,
        "bbox": r.bbox,
        "is_visual": r.is_visual,
        "content": r.content,
    }


def _apply_decisions(
    regions: List[Region], decisions: List[Dict]
) -> Tuple[List[Region], List[Dict[str, Any]]]:
    by_index: Dict[int, Dict] = {
        d["region_index"]: d for d in decisions
        if isinstance(d, dict) and "region_index" in d
    }
    out: List[Region] = []
    issues: List[Dict[str, Any]] = []

    for region in regions:
        decision = by_index.get(region.region_index)
        if not decision:
            out.append(region)
            continue

        dropped = bool(decision.get("drop"))
        dup_of = decision.get("duplicate_of")
        is_dup = dup_of is not None and dup_of != region.region_index

        if dropped or is_dup:
            if is_dup:
                note_frag = f"Duplicate of region {dup_of} — removed here."
                issues.append({"issue_type": "duplicate_text",
                               "region_indices": [region.region_index, dup_of],
                               "description": note_frag, "severity": "warning"})
            else:
                note_frag = "Dropped by layout pass (not this region's text)."
                issues.append({"issue_type": "dropped_region",
                               "region_indices": [region.region_index],
                               "description": note_frag, "severity": "warning"})
            new_note = _append_note(region.editorial_note, note_frag)
            extra = (decision.get("editorial_note") or "").strip()
            if extra:
                new_note = _append_note(new_note, extra)
            out.append(dataclasses.replace(
                region, content="", uncertain_readings=[], editorial_note=new_note,
            ))
            logger.info("  Emptied region %d (%s)", region.region_index, note_frag)
            continue

        new_langs = decision.get("languages") or region.languages
        new_layer = region.writing_layer
        layer = (decision.get("writing_layer") or "").strip()
        if layer in _WRITING_LAYERS:
            new_layer = layer
        note_frag = (decision.get("editorial_note") or "").strip()
        new_note = _append_note(region.editorial_note, note_frag) if note_frag else region.editorial_note
        if note_frag:
            issues.append({"issue_type": "editorial_note",
                           "region_indices": [region.region_index],
                           "description": note_frag, "severity": "warning"})

        out.append(dataclasses.replace(
            region, languages=new_langs, editorial_note=new_note, writing_layer=new_layer,
        ))
    return out, issues


def _append_note(existing: Optional[str], fragment: Optional[str]) -> Optional[str]:
    old = existing or ""
    frag = (fragment or "").strip()
    if frag and frag not in old:
        return (old + " | " + frag).strip(" |")
    return old or None


def _log_issues(issues: List[Dict]) -> None:
    for issue in issues:
        logger.info("Layout [%s] regions %s: %s",
                    issue.get("issue_type", "?"),
                    issue.get("region_indices", []),
                    issue.get("description", ""))


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)

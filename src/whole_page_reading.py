"""Read every region of a page from one whole-page call, keyed by region_index —
the two whole-page ensemble candidates: M2 (free, :data:`WHOLE_PAGE_PROMPT`) and
M3 (structured, :data:`STRUCTURED_READING_PROMPT`, one line → one region).

Both use the shared house style as the system instruction
(:data:`src.prompts.HOUSE_STYLE`), so the per-call prompt stays short. The page
image is encoded once by the caller and passed in as ``page_bytes``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from google import genai

from .llm import generate_json
from .prompts import HOUSE_STYLE

logger = logging.getLogger(__name__)

ImageInput = Tuple[bytes, str]


# ---------------------------------------------------------------------------
# Prompts (concise; the data comes last, then the task — Gemini 3.x guidance)
# ---------------------------------------------------------------------------

WHOLE_PAGE_PROMPT = """\
You see the FULL page image and the regions already located on it.

REGIONS (index, type, bbox [y_min,x_min,y_max,x_max] 0–1000, marginal_position):
{regions_json}

Based on the page above, transcribe the text of EACH region in natural reading
order, using the whole page for context (surrounding lines, repeated words, flow).
Assign every line of writing to EXACTLY ONE region — the one whose bounding box
contains it. Do NOT pull a neighbouring region's lines into this one (e.g. a
citation below a marginal note belongs to its own region); respect the boxes.
Skip purely visual regions and opposite-folio bleedthrough (empty content).

Respond ONLY with JSON:
{{"regions": [{{"region_index": 0, "content": "...", "languages": ["de"]}}, ...]}}
"""

STRUCTURED_READING_PROMPT = """\
You see the FULL page image and the exact regions located on it (with bboxes).

REGIONS (index, type, bbox [y_min,x_min,y_max,x_max] 0–1000, marginal_position):
{regions_json}

Based on the page above, transcribe the text as STRUCTURED data: produce each
region separately and assign every line of writing to EXACTLY ONE region (the one
whose bounding box contains it). Do not let a line appear in two regions, and do
not pull a neighbour's lines into this one — respect the boxes precisely. Skip
purely visual regions and opposite-folio bleedthrough (empty content).

Respond ONLY with JSON:
{{"regions": [{{"region_index": 0, "content": "...", "languages": ["de"]}}, ...]}}
"""


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def read_whole_page(
    client: genai.Client,
    detected_regions: List[Dict[str, Any]],
    model_id: str,
    thinking_level: str = "low",
    *,
    page_bytes: ImageInput,
    prompt_template: str = WHOLE_PAGE_PROMPT,
    stage: str = "read_page",
    temperature: Optional[float] = None,
    cached_content: Optional[str] = None,
) -> Dict[int, Dict[str, Any]]:
    """Whole-page transcription of every detected region, keyed by index.

    ``page_bytes`` is the pre-encoded ``(jpeg_bytes, mime)`` for the page (encoded
    once by the caller). Returns ``{region_index: {"content", "languages"}}`` with
    one entry per detected region (visual / opposite-folio / no-text → empty).
    """
    if not detected_regions:
        return {}

    known: Dict[int, Dict[str, Any]] = {d["region_index"]: d for d in detected_regions}
    prompt = prompt_template.format(regions_json=_serialize_regions(detected_regions))

    result = generate_json(
        client, model_id, prompt,
        thinking_level=thinking_level,
        temperature=temperature,
        system_instruction=HOUSE_STYLE,
        images=[page_bytes],
        cached_content=cached_content,
        default={"regions": []},
        stage=stage,
    )
    if not isinstance(result, dict):
        result = {"regions": []}
    items = result.get("regions")
    if not isinstance(items, list):
        items = []

    readings: Dict[int, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("region_index")
        if idx not in known:
            continue
        readings[idx] = {
            "content": item.get("content") or "",
            "languages": item.get("languages") or [],
        }

    out: Dict[int, Dict[str, Any]] = {}
    for idx, det in known.items():
        out[idx] = ({"content": "", "languages": []}
                    if _force_empty(det)
                    else readings.get(idx, {"content": "", "languages": []}))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_regions(detected_regions: List[Dict[str, Any]]) -> str:
    import json
    slim = [
        {
            "region_index": d.get("region_index"),
            "region_type": d.get("region_type"),
            "bbox": d.get("bbox"),
            "marginal_position": d.get("marginal_position"),
        }
        for d in detected_regions
    ]
    return json.dumps(slim, ensure_ascii=False, indent=2)


def _force_empty(det: Dict[str, Any]) -> bool:
    """Regions the whole-page read must never transcribe. Opposite-folio
    bleedthrough is covered by ``has_text is False`` (set during detection)."""
    return (
        det.get("has_text") is False
        or det.get("region_type") == "sketch"
    )

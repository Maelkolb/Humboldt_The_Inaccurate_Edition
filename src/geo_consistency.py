"""
Geolocation Consistency Check (Step 4.5) – Humboldt Journal Edition
===================================================================
A text-based quality gate that runs AFTER NER (Step 3) and Geocoding
(Step 4). Geocoders such as Wikidata/Nominatim happily return a coordinate
for almost any string, so a misread place name or an ambiguous homonym can
resolve to somewhere that makes no sense for Humboldt's journal (e.g. a town
in the United States standing in for a Venezuelan village, or a Wikidata item
that is not a geographic place at all).

This module asks the model to judge — from the page text alone, no image —
whether each resolved :class:`~src.models.GeoLocation` plausibly is the place
Humboldt actually named. Locations judged invalid with sufficient confidence
are dropped; everything else is kept, so only sensible geocoding results reach
the edition. The per-location verdicts are returned for auditing.

The check is conservative and fails open: on any error it keeps every
location untouched.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

from google import genai
from google.genai import types

from .json_utils import parse_json_robust
from .models import Entity, GeoLocation

logger = logging.getLogger(__name__)

# Drop a location only when the model is at least this confident it is wrong.
_DROP_CONFIDENCE = 0.6
# Truncate the page-context snippet sent to the model.
_CONTEXT_CHARS = 2000


_PROMPT = """\
You are validating automated geocoding results for a page of Alexander von
Humboldt's travel journal. Humboldt's American expedition (1799–1804) covered
Venezuela, Cuba, New Granada (Colombia), the Andes (Ecuador, Peru), and New
Spain (Mexico); his journals also reference his earlier European travels and
the scientists, instruments and places of his era. Place names appear in
German, French, Spanish and Latin, often in historical spellings, and the
transcription may contain misreadings (marked with "[?]").

For each resolved location you are given:
  * "name"        – the place name as it appears in the transcription
  * "display_name"– the label of the entity the geocoder resolved it to
  * "lat"/"lon"   – the resolved coordinates
  * "source"      – "wikidata" or "nominatim"
  * "context"     – sentences from the page mentioning the name (may be empty)

Decide whether the resolved entity plausibly IS the place Humboldt referred
to. Mark a result INVALID only when it clearly makes no sense, e.g.:
  * the resolved entity is not a geographic place at all;
  * the coordinates fall in a region inconsistent with the surrounding text
    and Humboldt's itinerary (e.g. a North-American homonym standing in for a
    South-American village discussed in the context);
  * the geocoder latched onto an unrelated famous place that merely shares
    the name / a misread fragment.

Be conservative: historical names, alternative spellings and small/obscure
places are NOT grounds for rejection. When unsure, mark it VALID.

PAGE CONTEXT (transcribed text excerpt):
```
{page_context}
```

RESOLVED LOCATIONS (JSON):
{locations_json}

Respond ONLY with a JSON array, one object per location, SAME ORDER as input:
[
  {{
    "name": "<the input name>",
    "verdict": "valid" | "invalid",
    "confidence": 0.0,
    "reason": "<short justification>"
  }}
]
"""


def _serialise(
    locations: List[GeoLocation],
    entities: List[Entity],
) -> List[Dict[str, Any]]:
    """Build the per-location payload, attaching any NER context for the name."""
    ctx_by_name: Dict[str, str] = {}
    for e in entities:
        if e.entity_type == "Location" and e.context and e.text not in ctx_by_name:
            ctx_by_name[e.text] = e.context
    return [
        {
            "name": loc.name,
            "display_name": loc.display_name,
            "lat": round(loc.lat, 4),
            "lon": round(loc.lon, 4),
            "source": loc.source,
            "context": ctx_by_name.get(loc.name, ""),
        }
        for loc in locations
    ]


def validate_locations(
    client: genai.Client,
    locations: List[GeoLocation],
    entities: List[Entity],
    page_context: str,
    model_id: str,
    thinking_level: str = "low",
) -> Tuple[List[GeoLocation], List[Dict[str, Any]]]:
    """Validate geocoded locations against the page text.

    Returns ``(kept_locations, reports)`` where *reports* holds one verdict
    dict per input location. Locations judged invalid with confidence
    >= ``_DROP_CONFIDENCE`` are removed from *kept_locations*.
    """
    if not locations:
        return locations, []

    locations_json = json.dumps(
        _serialise(locations, entities), ensure_ascii=False, indent=2
    )
    prompt = _PROMPT.format(
        page_context=(page_context or "")[:_CONTEXT_CHARS],
        locations_json=locations_json,
    )

    data: Any = []
    for attempt in range(1, 3):
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_level=thinking_level
                    ),
                    response_mime_type="application/json",
                ),
            )
            data = parse_json_robust(response.text)
            if isinstance(data, list):
                break
        except Exception as exc:
            logger.error(
                "Geo-validation error (attempt %d/2): %s", attempt, exc
            )

    if not isinstance(data, list):
        logger.warning("Geo-validation returned no usable verdict; keeping all.")
        return locations, []

    # Index verdicts by location name (the prompt preserves order, but match
    # on name defensively so a reordered/short response can't drop the wrong one).
    verdicts: Dict[str, Dict[str, Any]] = {}
    reports: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        verdict = str(item.get("verdict", "valid")).strip().lower()
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        report = {
            "name": name,
            "verdict": verdict,
            "confidence": conf,
            "reason": item.get("reason", ""),
        }
        reports.append(report)
        if name:
            verdicts[name] = report

    kept: List[GeoLocation] = []
    dropped = 0
    for loc in locations:
        v = verdicts.get(loc.name)
        if (v and v["verdict"] == "invalid"
                and v["confidence"] >= _DROP_CONFIDENCE):
            dropped += 1
            logger.info(
                "  Dropped implausible location %r (%s) — %s",
                loc.name, loc.display_name, v.get("reason", ""),
            )
            continue
        kept.append(loc)

    logger.info(
        "  Geo-validation: kept %d / %d locations (%d dropped).",
        len(kept), len(locations), dropped,
    )
    return kept, reports

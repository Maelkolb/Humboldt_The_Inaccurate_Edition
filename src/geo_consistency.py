"""Text-based plausibility gate for geocoding results.

Geocoders return a coordinate for almost any string, so a misread or ambiguous
place name can resolve somewhere implausible for Humboldt's journal. The model
judges, from the page text alone, whether each resolved
:class:`~src.models.GeoLocation` is the place actually named. Any location
judged invalid is re-geocoded with the model's own proposed correction before
being dropped — dropped only if that retry also fails. Per-location verdicts
are returned for auditing. Fails open (keeps everything) on any error.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from google import genai

from .geocoding import geocode_query
from .llm import generate_json
from .models import Entity, GeoLocation

logger = logging.getLogger(__name__)

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

When you mark a result INVALID, also consider whether the page context makes
a better search query inferable — e.g. adding a disambiguating region/country
("Rochester, Kent, England" instead of "Rochester"), or fixing an obvious
misreading. If so, include it as "suggested_query" so the geocoder can be
re-run with it. Leave "suggested_query" as an empty string when VALID, or
when no better query is apparent.

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
    "reason": "<short justification>",
    "suggested_query": "<corrected/disambiguated search string to re-geocode, or "">"
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
    geo_cache: Optional[Dict[str, Optional[Dict]]] = None,
) -> Tuple[List[GeoLocation], List[Dict[str, Any]]]:
    """Validate geocoded locations against the page text.

    Returns ``(kept_locations, reports)`` where *reports* holds one verdict
    dict per input location. Any location judged invalid is, when the model
    proposed a "suggested_query", re-geocoded with that corrected query (see
    :func:`src.geocoding.geocode_query`) instead of being dropped outright —
    regardless of the model's confidence, since a free retry against a
    query the model itself proposed carries no real downside. Only when the
    retry also fails to resolve (or no query was proposed) is the location
    removed from *kept_locations*. ``geo_cache`` is the same cross-page cache
    used by :func:`src.geocoding.geocode_entities`, so retries are cached too.
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

    data = generate_json(
        client, model_id, prompt,
        thinking_level=thinking_level,
        default=[],
        max_attempts=2,
        stage="geo_validation",
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
            "suggested_query": str(item.get("suggested_query", "")).strip(),
        }
        reports.append(report)
        if name:
            verdicts[name] = report

    kept: List[GeoLocation] = []
    dropped = 0
    corrected = 0
    retry_session: Optional[requests.Session] = None
    for loc in locations:
        v = verdicts.get(loc.name)
        if v and v["verdict"] == "invalid":
            query = v.get("suggested_query") or ""
            retry_result = None
            if query and query != loc.name:
                if retry_session is None:
                    retry_session = requests.Session()
                retry_result = geocode_query(query, cache=geo_cache, session=retry_session)
            if retry_result:
                kept.append(GeoLocation(
                    name=loc.name,
                    lat=retry_result["lat"],
                    lon=retry_result["lon"],
                    display_name=retry_result["display_name"],
                    wikidata_id=retry_result.get("wikidata_id"),
                    geonames_id=retry_result.get("geonames_id"),
                    source=retry_result.get("source", "nominatim"),
                    resolved_query=query,
                ))
                v["retry_result"] = "corrected"
                corrected += 1
                logger.info(
                    "  Re-geocoded %r via corrected query %r -> %.4f, %.4f",
                    loc.name, query, retry_result["lat"], retry_result["lon"],
                )
                continue
            v["retry_result"] = "still_invalid" if query else "no_query_suggested"
            dropped += 1
            logger.info(
                "  Dropped implausible location %r (%s) — %s",
                loc.name, loc.display_name, v.get("reason", ""),
            )
            continue
        kept.append(loc)

    logger.info(
        "  Geo-validation: kept %d / %d locations (%d dropped, %d corrected via retry).",
        len(kept), len(locations), dropped, corrected,
    )
    return kept, reports

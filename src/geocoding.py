"""
Geocoding (Step 4) – Humboldt Journal Edition
==============================================
Resolves Location entities to geographic coordinates and GeoNames IDs.

Resolution order:
1. Wikidata (primary) – returns coordinates + GeoNames ID + Wikidata QID
2. Nominatim / OpenStreetMap (fallback) – coordinates only

"""

import logging
import time
from typing import Dict, List, Optional

import requests

from .models import Entity, GeoLocation

logger = logging.getLogger(__name__)

WIKIDATA_SEARCH_URL = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL_URL  = "https://query.wikidata.org/sparql"
NOMINATIM_URL        = "https://nominatim.openstreetmap.org/search"
USER_AGENT           = "HumboldtDigitalEdition/1.0 (academic research)"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_wikidata_point(point_str: str) -> Optional[tuple]:
    """Parse a WKT Point string like 'Point(16.3700 48.2000)' → (lon, lat)."""
    try:
        inner = point_str.strip()
        if inner.upper().startswith("POINT("):
            inner = inner[6:].rstrip(")")
        lon_s, lat_s = inner.split()
        return float(lon_s), float(lat_s)
    except (ValueError, AttributeError):
        return None


def _wikidata_search(name: str, session: requests.Session) -> Optional[str]:
    """Search Wikidata for *name* and return the best-matching QID, or None."""
    headers = {"User-Agent": USER_AGENT}
    for lang in ("de", "en"):
        params = {
            "action": "wbsearchentities",
            "search": name,
            "language": lang,
            "type": "item",
            "limit": 5,
            "format": "json",
        }
        try:
            resp = session.get(WIKIDATA_SEARCH_URL, params=params,
                               headers=headers, timeout=10)
            resp.raise_for_status()
            hits = resp.json().get("search", [])
            if hits:
                return hits[0]["id"]          # e.g. "Q54810"
        except (requests.RequestException, ValueError, KeyError) as exc:
            logger.debug("Wikidata search failed for %r (lang=%s): %s", name, lang, exc)
    return None


def _wikidata_resolve(qid: str, session: requests.Session) -> Optional[Dict]:
    """
    For a known QID fetch:
      - P625  coordinate location  → lat/lon
      - P1566 GeoNames ID          → geonames_id (optional)

    Returns a partial result dict or None when no coordinates exist.
    """
    sparql = (
        f"SELECT ?coord ?geonamesId ?label WHERE {{"
        f"  wd:{qid} wdt:P625 ?coord ."
        f"  OPTIONAL {{ wd:{qid} wdt:P1566 ?geonamesId . }}"
        f"  OPTIONAL {{"
        f"    wd:{qid} rdfs:label ?label ."
        f"    FILTER(LANG(?label) IN ('de','en'))"
        f"  }}"
        f"}}"
        f" LIMIT 1"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/sparql-results+json",
    }
    try:
        resp = session.get(WIKIDATA_SPARQL_URL,
                           params={"query": sparql, "format": "json"},
                           headers=headers, timeout=15)
        resp.raise_for_status()
        bindings = resp.json()["results"]["bindings"]
        if not bindings:
            return None
        row = bindings[0]

        parsed = _parse_wikidata_point(row["coord"]["value"])
        if parsed is None:
            return None
        lon, lat = parsed

        geonames_id: Optional[int] = None
        if "geonamesId" in row:
            try:
                geonames_id = int(row["geonamesId"]["value"])
            except ValueError:
                pass

        display_name = row["label"]["value"] if "label" in row else qid

        return {
            "lat": lat,
            "lon": lon,
            "display_name": display_name,
            "wikidata_id": qid,
            "geonames_id": geonames_id,
            "source": "wikidata",
        }
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.debug("Wikidata SPARQL failed for %r: %s", qid, exc)
        return None


# ---------------------------------------------------------------------------
# Public geocoding functions
# ---------------------------------------------------------------------------

def resolve_via_wikidata(
    name: str,
    session: Optional[requests.Session] = None,
) -> Optional[Dict]:
    """
    Primary resolver: look up *name* in Wikidata and return coordinates,
    GeoNames ID, and Wikidata QID.

    Returns a dict with keys: lat, lon, display_name, wikidata_id,
    geonames_id, source – or None when nothing was found.
    """
    sess = session or requests.Session()

    qid = _wikidata_search(name, sess)
    if qid is None:
        return None

    return _wikidata_resolve(qid, sess)


def resolve_via_nominatim(
    name: str,
    session: Optional[requests.Session] = None,
) -> Optional[Dict]:
    """
    Fallback resolver: query Nominatim / OSM for coordinates only.

    Returns a dict with keys: lat, lon, display_name, source – or None.
    """
    sess = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}

    params = {
        "q": name,
        "format": "json",
        "limit": 1,
        "accept-language": "de,en",
    }
    try:
        resp = sess.get(NOMINATIM_URL, params=params,
                        headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        if results:
            hit = results[0]
            return {
                "lat": float(hit["lat"]),
                "lon": float(hit["lon"]),
                "display_name": hit.get("display_name", name),
                "wikidata_id": None,
                "geonames_id": None,
                "source": "nominatim",
            }
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.debug("Nominatim failed for %r: %s", name, exc)

    return None


def geocode_location(
    name: str,
    session: Optional[requests.Session] = None,
) -> Optional[Dict]:
    """
    Resolve *name* to geographic data.

    Tries Wikidata first; falls back to Nominatim when Wikidata cannot
    provide coordinates.
    """
    sess = session or requests.Session()

    result = resolve_via_wikidata(name, session=sess)
    if result:
        logger.debug(
            "  [wikidata] %s -> %.4f, %.4f  (QID=%s, GeoNames=%s)",
            name, result["lat"], result["lon"],
            result.get("wikidata_id"), result.get("geonames_id"),
        )
        return result

    logger.debug("  Wikidata miss for %r, trying Nominatim…", name)
    result = resolve_via_nominatim(name, session=sess)
    if result:
        logger.debug(
            "  [nominatim] %s -> %.4f, %.4f",
            name, result["lat"], result["lon"],
        )
    return result


def geocode_entities(
    entities: List[Entity],
    cache: Optional[Dict[str, Optional[Dict]]] = None,
    delay: float = 1.0,
) -> List[GeoLocation]:
    """Geocode all Location entities and return a deduplicated GeoLocation list."""
    if cache is None:
        cache = {}

    session = requests.Session()
    location_names = list(dict.fromkeys(
        e.text for e in entities if e.entity_type == "Location"
    ))

    new_queries = [n for n in location_names if n not in cache]
    if new_queries:
        logger.info("Geocoding %d new location names…", len(new_queries))

    for name in new_queries:
        result = geocode_location(name, session=session)
        cache[name] = result
        if not result:
            logger.debug("  %s -> not found", name)
        time.sleep(delay)

    locations: List[GeoLocation] = []
    seen: set = set()
    for name in location_names:
        if name in seen:
            continue
        seen.add(name)
        geo = cache.get(name)
        if geo:
            locations.append(GeoLocation(
                name=name,
                lat=geo["lat"],
                lon=geo["lon"],
                display_name=geo["display_name"],
                wikidata_id=geo.get("wikidata_id"),
                geonames_id=geo.get("geonames_id"),
                source=geo.get("source", "nominatim"),
            ))

    return locations

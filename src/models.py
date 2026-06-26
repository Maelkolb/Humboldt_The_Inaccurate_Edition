"""
Data structures for the Humboldt Journal Digital Edition pipeline.

Extended with editorial apparatus fields for scholarly edition:
- language tracking per region
- editorial notes for crossed-out text, uncertain readings
- positional info (margin location)
"""

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Any, Optional
from enum import Enum


class RegionType(str, Enum):
    ENTRY_HEADING = "entry_heading"
    MAIN_TEXT = "main_text"
    MARGINAL_NOTE = "marginal_note"
    CALCULATION = "calculation"
    OBSERVATION_TABLE = "observation_table"
    SKETCH = "sketch"
    CROSSED_OUT = "crossed_out"
    BIBLIOGRAPHIC_REF = "bibliographic_ref"
    COORDINATES = "coordinates"
    INSTRUMENT_LIST = "instrument_list"
    PAGE_NUMBER = "page_number"
    CATCH_PHRASE = "catch_phrase"


@dataclass
class Region:
    """A detected region on a Humboldt journal page."""
    region_type: str
    region_index: int
    content: str
    is_visual: bool = False
    table_data: Optional[Dict[str, Any]] = None
    # Editorial apparatus
    languages: List[str] = field(default_factory=list)  # ["de", "fr", "la", "es"]
    editorial_note: Optional[str] = None  # e.g. "partially illegible", "ink blot"
    position: Optional[str] = None  # e.g. "left margin", "top margin", "above line 3"
    uncertain_readings: List[str] = field(default_factory=list)  # words marked [?]
    crossed_out_text: Optional[str] = None  # reconstructed deleted text
    related_to_entry: Optional[str] = None  # which numbered entry this belongs to
    bbox: Optional[List[float]] = None  # [y_min, x_min, y_max, x_max] as 0–1000 coords
    # Marginal layout
    marginal_position: Optional[str] = None  # "left", "right", "mTop", "mBottom", "opposite", "inline"
    # Temporal stratigraphy
    writing_layer: Optional[str] = None  # "primary", "later_addition", "unknown"
    # Special region flags
    is_pasted_slip: bool = False   # physical paper slip pasted onto the page
    # TEI source tracking
    tei_id: Optional[str] = None  # xml:id of the source TEI element
    # Ground-truth comparison (populated only when --ground-truth-tei is used)
    ground_truth_content: Optional[str] = None       # GT text matched to this region
    ground_truth_confidence: Optional[float] = None  # 0..1 confidence of the match
    # eHD gold-standard entity annotations (persName/placeName/orgName from the
    # GT TEI, already carrying register/authority refs) whose surface form
    # appears in this region's ground_truth_content. Populated by ground_truth.py.
    # Quoted annotation: Entity is defined below Region in this module.
    ground_truth_entities: "List[Entity]" = field(default_factory=list)
    # Pre-consistency-check snapshots (populated only when the consistency
    # check runs). These let us inspect/compare what Gemini transcribed
    # BEFORE the consistency QA pass touched it.
    content_pre_consistency: Optional[str] = None
    uncertain_readings_pre_consistency: Optional[List[str]] = None

    def to_dict(self) -> Dict:
        d = {
            "region_type": self.region_type,
            "region_index": self.region_index,
            "content": self.content,
            "is_visual": self.is_visual,
            "languages": self.languages,
        }
        if self.bbox:
            d["bbox"] = self.bbox
        if self.table_data is not None:
            d["table_data"] = self.table_data
        if self.editorial_note:
            d["editorial_note"] = self.editorial_note
        if self.position:
            d["position"] = self.position
        if self.uncertain_readings:
            d["uncertain_readings"] = self.uncertain_readings
        if self.crossed_out_text:
            d["crossed_out_text"] = self.crossed_out_text
        if self.related_to_entry:
            d["related_to_entry"] = self.related_to_entry
        if self.marginal_position:
            d["marginal_position"] = self.marginal_position
        if self.writing_layer:
            d["writing_layer"] = self.writing_layer
        if self.is_pasted_slip:
            d["is_pasted_slip"] = self.is_pasted_slip
        if self.tei_id:
            d["tei_id"] = self.tei_id
        if self.ground_truth_content is not None:
            d["ground_truth_content"] = self.ground_truth_content
        if self.ground_truth_confidence is not None:
            d["ground_truth_confidence"] = self.ground_truth_confidence
        if self.ground_truth_entities:
            d["ground_truth_entities"] = [
                entity_to_dict(e) for e in self.ground_truth_entities
            ]
        if self.content_pre_consistency is not None:
            d["content_pre_consistency"] = self.content_pre_consistency
        if self.uncertain_readings_pre_consistency is not None:
            d["uncertain_readings_pre_consistency"] = self.uncertain_readings_pre_consistency
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "Region":
        return cls(
            region_type=d["region_type"],
            region_index=d["region_index"],
            content=d.get("content", ""),
            is_visual=d.get("is_visual", False),
            table_data=d.get("table_data"),
            languages=d.get("languages", []),
            editorial_note=d.get("editorial_note"),
            position=d.get("position"),
            uncertain_readings=d.get("uncertain_readings", []),
            crossed_out_text=d.get("crossed_out_text"),
            related_to_entry=d.get("related_to_entry"),
            bbox=d.get("bbox"),
            marginal_position=d.get("marginal_position"),
            writing_layer=d.get("writing_layer"),
            is_pasted_slip=d.get("is_pasted_slip", False),
            tei_id=d.get("tei_id"),
            ground_truth_content=d.get("ground_truth_content"),
            ground_truth_confidence=d.get("ground_truth_confidence"),
            ground_truth_entities=[
                entity_from_dict(e) for e in d.get("ground_truth_entities", [])
            ],
            content_pre_consistency=d.get("content_pre_consistency"),
            uncertain_readings_pre_consistency=d.get("uncertain_readings_pre_consistency"),
        )


@dataclass
class Entity:
    """A named entity found in Humboldt's text."""
    text: str
    entity_type: str
    start_char: int
    end_char: int
    context: Optional[str] = None
    normalized_form: Optional[str] = None  # modern/standardized form
    language: Optional[str] = None  # language of the entity mention
    # ----- Authority linking (edition humboldt digital register) -----
    # Populated by the optional, standalone src.entity_linking post-processor
    # (run after the pipeline via scripts/link_entities.py). Absent/None when
    # no register match was found (e.g. minerals among Species entities, which
    # are not in the plant register). ``authority_uri`` chains out to VIAF/GND
    # (persons), GeoNames (places) or GBIF (plants).
    ehd_id: Optional[str] = None            # eHD register id, e.g. "H0006403"
    ehd_url: Optional[str] = None           # https://edition-humboldt.de/v11/<id>
    authority_uri: Optional[str] = None     # VIAF/GND/GeoNames/GBIF URI
    authority_label: Optional[str] = None   # canonical register/authority label
    link_method: Optional[str] = None       # normalized_form|exact|alt|surname|fuzzy
    link_score: Optional[float] = None      # 0..1 match confidence
    link_ambiguous: bool = False            # >1 register candidate at top rank
    link_candidates: List[str] = field(default_factory=list)  # other eHD ids


def entity_to_dict(e: "Entity") -> Dict:
    """Serialise an Entity, omitting authority-link fields that are unset.

    Keeps the JSON for unlinked entities byte-for-byte compatible with the
    pre-linking pipeline output; linked entities gain the eHD/authority keys.
    """
    d = {
        "text": e.text,
        "entity_type": e.entity_type,
        "start_char": e.start_char,
        "end_char": e.end_char,
        "context": e.context,
        "normalized_form": e.normalized_form,
        "language": e.language,
    }
    if e.ehd_id:
        d["ehd_id"] = e.ehd_id
        if e.ehd_url:
            d["ehd_url"] = e.ehd_url
        d["authority_uri"] = e.authority_uri
        d["authority_label"] = e.authority_label
        d["link_method"] = e.link_method
        d["link_score"] = e.link_score
        if e.link_ambiguous:
            d["link_ambiguous"] = True
        if e.link_candidates:
            d["link_candidates"] = e.link_candidates
    return d


def entity_from_dict(e: Dict) -> "Entity":
    """Reconstruct an Entity from its serialised dict (inverse of entity_to_dict)."""
    return Entity(
        text=e["text"],
        entity_type=e["entity_type"],
        start_char=e.get("start_char", -1),
        end_char=e.get("end_char", -1),
        context=e.get("context"),
        normalized_form=e.get("normalized_form"),
        language=e.get("language"),
        ehd_id=e.get("ehd_id"),
        ehd_url=e.get("ehd_url"),
        authority_uri=e.get("authority_uri"),
        authority_label=e.get("authority_label"),
        link_method=e.get("link_method"),
        link_score=e.get("link_score"),
        link_ambiguous=e.get("link_ambiguous", False),
        link_candidates=e.get("link_candidates", []),
    )


@dataclass
class GeoLocation:
    """Geographic coordinates and authority identifiers for a location entity."""
    name: str
    lat: float
    lon: float
    display_name: str
    # Authority identifiers – populated when resolved via Wikidata
    wikidata_id: Optional[str] = None    # e.g. "Q54810"
    geonames_id: Optional[int] = None   # numeric GeoNames feature ID (P1566)
    # Provenance of the resolved data
    source: str = "nominatim"           # "wikidata" | "nominatim"


@dataclass
class PageResult:
    """Complete result for a single journal page."""
    page_number: int
    image_filename: str
    folio_label: str  # e.g. "67r", "56v" from Humboldt's foliation
    regions: List[Region]
    full_text: str
    entities: List[Entity]
    locations: List[GeoLocation]
    processing_timestamp: str
    model_used: str
    entry_numbers: List[str] = field(default_factory=list)  # e.g. ["50", "51", "52"]
    page_languages: List[str] = field(default_factory=list)  # languages on this page
    # Consistency check report (Step 2.5). Populated whenever the consistency
    # check ran on this page. Each entry has the LLM's original shape:
    #   {"issue_type": "...", "region_indices": [...],
    #    "description": "...", "severity": "error"|"warning"}
    # Together with each Region's ``content_pre_consistency`` snapshot, this
    # makes the QA pass fully auditable from the JSON output.
    consistency_issues: List[Dict[str, Any]] = field(default_factory=list)
    # Geolocation validation report (Step 4.5). One verdict dict per resolved
    # location: {"name", "verdict": "valid"|"invalid", "confidence", "reason"}.
    geo_validation: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_ground_truth(self) -> bool:
        """True if at least one region has ground_truth_content populated."""
        return any(
            r.ground_truth_content is not None and r.ground_truth_content != ""
            for r in self.regions
        )

    def to_dict(self) -> Dict:
        d = {
            "page_number": self.page_number,
            "image_filename": self.image_filename,
            "folio_label": self.folio_label,
            "regions": [r.to_dict() for r in self.regions],
            "full_text": self.full_text,
            "entities": [entity_to_dict(e) for e in self.entities],
            "locations": [asdict(loc) for loc in self.locations],
            "processing_timestamp": self.processing_timestamp,
            "model_used": self.model_used,
            "entry_numbers": self.entry_numbers,
            "page_languages": self.page_languages,
        }
        if self.consistency_issues:
            d["consistency_issues"] = self.consistency_issues
        if self.geo_validation:
            d["geo_validation"] = self.geo_validation
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "PageResult":
        regions = [Region.from_dict(r) for r in d.get("regions", [])]
        entities = [
            Entity(
                text=e["text"],
                entity_type=e["entity_type"],
                start_char=e["start_char"],
                end_char=e["end_char"],
                context=e.get("context"),
                normalized_form=e.get("normalized_form"),
                language=e.get("language"),
                ehd_id=e.get("ehd_id"),
                ehd_url=e.get("ehd_url"),
                authority_uri=e.get("authority_uri"),
                authority_label=e.get("authority_label"),
                link_method=e.get("link_method"),
                link_score=e.get("link_score"),
                link_ambiguous=e.get("link_ambiguous", False),
                link_candidates=e.get("link_candidates", []),
            )
            for e in d.get("entities", [])
        ]
        locations = [
            GeoLocation(
                name=loc["name"],
                lat=loc["lat"],
                lon=loc["lon"],
                display_name=loc["display_name"],
                wikidata_id=loc.get("wikidata_id"),
                geonames_id=loc.get("geonames_id"),
                source=loc.get("source", "nominatim"),
            )
            for loc in d.get("locations", [])
        ]
        return cls(
            page_number=d["page_number"],
            image_filename=d["image_filename"],
            folio_label=d.get("folio_label", ""),
            regions=regions,
            full_text=d.get("full_text", ""),
            entities=entities,
            locations=locations,
            processing_timestamp=d.get("processing_timestamp", ""),
            model_used=d.get("model_used", ""),
            entry_numbers=d.get("entry_numbers", []),
            page_languages=d.get("page_languages", []),
            consistency_issues=d.get("consistency_issues", []),
            geo_validation=d.get("geo_validation", []),
        )

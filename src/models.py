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

    def to_dict(self) -> Dict:
        return {
            "page_number": self.page_number,
            "image_filename": self.image_filename,
            "folio_label": self.folio_label,
            "regions": [r.to_dict() for r in self.regions],
            "full_text": self.full_text,
            "entities": [asdict(e) for e in self.entities],
            "locations": [asdict(loc) for loc in self.locations],
            "processing_timestamp": self.processing_timestamp,
            "model_used": self.model_used,
            "entry_numbers": self.entry_numbers,
            "page_languages": self.page_languages,
        }

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
        )

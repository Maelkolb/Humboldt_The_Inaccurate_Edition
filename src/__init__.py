"""humboldt-edition – LLM pipeline for Humboldt journal digitization."""
from .models import Entity, GeoLocation, Region, RegionType, PageResult
from .pipeline import process_book, process_page, load_results_from_json
from .html_generator import (
    generate_html_edition,
    build_edition_bundle,
    zip_bundle,
)
from .geocoding import geocode_entities
from .region_detection import detect_regions
from .whole_page_reading import read_whole_page
from .transcription import transcribe_regions_ensemble
from .layout import resolve_layout
from .ner import perform_ner
from .entity_register import EntityRegister, RegisterEntry, LinkMatch
from .entity_linking import (
    link_entity,
    link_entities,
    link_results,
    check_entity_consistency,
    link_and_check,
    link_and_check_json,
    DEFAULT_TYPE_TO_KIND,
)
from .geo_consistency import validate_locations
from .tei_parser import parse_tei_file, parse_tei_string
from .tei_writer import (
    results_to_tei_document,
    page_result_to_tei_document,
    write_tei_file,
)
from .ground_truth import (
    match_ground_truth_to_page,
    annotate_results_with_ground_truth,
    fill_missing_body_ground_truth,
)

__all__ = [
    "Entity",
    "GeoLocation",
    "Region",
    "RegionType",
    "PageResult",
    "process_book",
    "process_page",
    "load_results_from_json",
    "generate_html_edition",
    "build_edition_bundle",
    "zip_bundle",
    "geocode_entities",
    "detect_regions",
    "read_whole_page",
    "transcribe_regions_ensemble",
    "resolve_layout",
    "perform_ner",
    "EntityRegister",
    "RegisterEntry",
    "LinkMatch",
    "link_entity",
    "link_entities",
    "link_results",
    "check_entity_consistency",
    "link_and_check",
    "link_and_check_json",
    "DEFAULT_TYPE_TO_KIND",
    "validate_locations",
    "parse_tei_file",
    "parse_tei_string",
    "results_to_tei_document",
    "page_result_to_tei_document",
    "write_tei_file",
    "match_ground_truth_to_page",
    "annotate_results_with_ground_truth",
    "fill_missing_body_ground_truth",
]

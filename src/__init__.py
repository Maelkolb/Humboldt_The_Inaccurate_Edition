"""humboldt-edition – LLM pipeline for Humboldt journal digitization."""
from .models import Entity, GeoLocation, Region, RegionType, PageResult
from .pipeline import process_book, process_page, load_results_from_json
from .html_generator import generate_html_edition
from .geocoding import geocode_entities
from .region_detection import detect_regions
from .transcription import transcribe_regions
from .consistency_check import check_and_fix_regions
from .ner import perform_ner
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
    "geocode_entities",
    "detect_regions",
    "transcribe_regions",
    "check_and_fix_regions",
    "perform_ner",
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

"""
Processing Pipeline – Humboldt Journal Edition
===============================================
Orchestrates all steps for Humboldt's journal pages.

Special handling:
- Folio label extraction from filenames (e.g. H0019734__67r.jpg → "67r")
- Entry number detection from region content
- Higher thinking levels for difficult handwriting
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from tqdm import tqdm
from google import genai

from .models import Entity, GeoLocation, Region, PageResult
from .region_detection import detect_regions
from .transcription import transcribe_regions
from .consistency_check import check_and_fix_regions
from .ner import perform_ner
from .geocoding import geocode_entities

logger = logging.getLogger(__name__)

VALID_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_image_files(folder: str | Path) -> List[Path]:
    """Return all image files from folder, sorted by name."""
    folder = Path(folder)
    files = [
        folder / f
        for f in os.listdir(folder)
        if Path(f).suffix.lower() in VALID_IMAGE_EXTENSIONS
    ]
    return sorted(files)


def extract_folio_label(filename: str) -> str:
    """
    Extract folio label from Humboldt image filenames.
    Patterns:
      H0019734__67r.jpg  → "67r"
      H0019734__56v.jpg  → "56v"
      H0019734__63v.jpg  → "63v"
      page_001.jpg       → "1"
    """
    # Humboldt SBB pattern: H0019734__NNx.jpg
    m = re.search(r'__(\d+[rv]?)\.\w+$', filename, re.IGNORECASE)
    if m:
        return m.group(1)

    # Generic page number
    m = re.search(r'(\d+)\.\w+$', filename)
    if m:
        return m.group(1)

    return filename


def extract_page_number(filename: str) -> int:
    """Extract a numeric sort key from filename."""
    folio = extract_folio_label(filename)
    # Get just the number part for sorting
    m = re.match(r'(\d+)', folio)
    if m:
        num = int(m.group(1))
        # recto before verso
        if 'v' in folio.lower():
            return num * 2
        else:
            return num * 2 - 1
    return 0


def extract_entry_numbers(regions: List[Region]) -> List[str]:
    """
    Extract numbered entry identifiers from entry_heading regions.
    E.g. "N. 50-52." → ["50", "51", "52"]
         "9)" → ["9"]
    """
    entries = []
    for r in regions:
        if r.region_type == "entry_heading":
            # Match patterns like "N. 50-52", "50)", "N. 9-11"
            numbers = re.findall(r'(\d+)', r.content)
            if len(numbers) == 2:
                try:
                    start, end = int(numbers[0]), int(numbers[1])
                    entries.extend(str(n) for n in range(start, end + 1))
                except ValueError:
                    entries.extend(numbers)
            else:
                entries.extend(numbers)

        # Also check main_text for entry starts like "50) am 31 Oct..."
        elif r.region_type == "main_text" and r.content:
            m = re.match(r'^(\d{1,3})\)', r.content.strip())
            if m:
                num = m.group(1)
                if num not in entries:
                    entries.append(num)

    return entries


def extract_page_languages(regions: List[Region]) -> List[str]:
    """Collect all languages used across regions on a page."""
    langs = set()
    for r in regions:
        for lang in (r.languages or []):
            if lang is not None:
                langs.add(lang)
    return sorted(langs)


def build_full_text(regions: List[Region]) -> str:
    """Combine text from all non-visual regions for NER."""
    parts: List[str] = []
    for region in regions:
        if region.is_visual:
            continue
        if region.region_type == "observation_table" and region.table_data:
            for row in region.table_data.get("cells", []):
                parts.extend(cell for cell in row if cell)
        elif region.content:
            parts.append(region.content)
    return "\n\n".join(parts)


def load_results_from_json(source: str | Path) -> List[PageResult]:
    """Load previously processed PageResult objects from JSON."""
    source = Path(source)
    if source.is_file():
        with open(source, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        results = [PageResult.from_dict(d) for d in data]
    elif source.is_dir():
        json_files = sorted(source.glob("page_*.json"))
        results = []
        for jf in json_files:
            with open(jf, "r", encoding="utf-8") as fh:
                results.append(PageResult.from_dict(json.load(fh)))
    else:
        raise FileNotFoundError(f"No file or directory at {source}")

    results.sort(key=lambda r: r.page_number)
    logger.info("Loaded %d page results.", len(results))
    return results


# ---------------------------------------------------------------------------
# Single-page processor
# ---------------------------------------------------------------------------

def process_page(
    client: genai.Client,
    image_path: str | Path,
    page_number: int,
    entity_types: dict,
    model_id: str,
    thinking_level: str = "medium",
    thinking_level_layout: str | None = None,
    thinking_level_transcription: str | None = None,
    run_consistency_check: bool = True,
    geo_cache: Optional[Dict] = None,
) -> PageResult:
    """Run the full pipeline for one Humboldt journal page."""
    image_path = Path(image_path)
    folio_label = extract_folio_label(image_path.name)
    logger.info("Processing folio %s (page %d): %s", folio_label, page_number, image_path.name)

    # Use separate thinking levels if provided, otherwise fall back to default
    layout_thinking = thinking_level_layout or thinking_level
    transcription_thinking = thinking_level_transcription or thinking_level

    # Step 1 – Region Detection (high thinking for complex layouts)
    logger.info("  Step 1: Region detection (thinking: %s)...", layout_thinking)
    detected = detect_regions(client, image_path, model_id, layout_thinking)
    logger.info("  Detected %d regions", len(detected))

    # Step 2 – Transcription (low thinking for speed)
    logger.info("  Step 2: Transcription (thinking: %s)...", transcription_thinking)
    regions = transcribe_regions(client, image_path, detected, model_id, transcription_thinking)
    logger.info("  Transcribed %d regions", len(regions))

    # Step 2.5 – Consistency / Deduplication check
    if run_consistency_check:
        logger.info("  Step 2.5: Consistency check...")
        regions, issues = check_and_fix_regions(
            client, regions, model_id, thinking_level="low"
        )
        errors   = [i for i in issues if i.get("severity") != "warning"]
        warnings = [i for i in issues if i.get("severity") == "warning"]
        if issues:
            logger.info(
                "  Consistency: %d issue(s) found — %d corrected, %d flagged for review.",
                len(issues), len(errors), len(warnings),
            )
        else:
            logger.info("  Consistency: clean.")

    # Extract metadata
    entry_numbers = extract_entry_numbers(regions)
    page_languages = extract_page_languages(regions)

    # Step 3 – NER
    full_text = build_full_text(regions)
    logger.info("  Step 3: NER on %d chars...", len(full_text))
    entities = perform_ner(client, full_text, entity_types, model_id, thinking_level)
    logger.info("  Found %d entities", len(entities))

    # Step 4 – Geocoding
    logger.info("  Step 4: Geocoding...")
    locations = geocode_entities(entities, cache=geo_cache)
    logger.info("  Geocoded %d locations", len(locations))

    return PageResult(
        page_number=page_number,
        image_filename=image_path.name,
        folio_label=folio_label,
        regions=regions,
        full_text=full_text,
        entities=entities,
        locations=locations,
        processing_timestamp=datetime.now().isoformat(),
        model_used=model_id,
        entry_numbers=entry_numbers,
        page_languages=page_languages,
    )


# ---------------------------------------------------------------------------
# Book-level processor
# ---------------------------------------------------------------------------

def process_book(
    client: genai.Client,
    image_folder: str | Path,
    output_folder: str | Path,
    entity_types: dict,
    model_id: str,
    thinking_level: str = "medium",
    thinking_level_layout: str | None = None,
    thinking_level_transcription: str | None = None,
    run_consistency_check: bool = True,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
) -> List[PageResult]:
    """Process all pages in image_folder through the full pipeline."""
    image_folder = Path(image_folder)
    output_folder = Path(output_folder)
    json_folder = output_folder / "json"
    json_folder.mkdir(parents=True, exist_ok=True)

    image_files = get_image_files(image_folder)
    if not image_files:
        logger.error("No images found in %s", image_folder)
        return []

    logger.info("Found %d images in %s", len(image_files), image_folder)

    subset = image_files[start_page:end_page]
    logger.info("Processing %d pages...", len(subset))

    geo_cache: Dict = {}
    results: List[PageResult] = []

    for idx, image_path in enumerate(tqdm(subset, desc="Folios", unit="fol")):
        page_num = extract_page_number(image_path.name) or (idx + 1)
        try:
            result = process_page(
                client, image_path, page_num, entity_types,
                model_id, thinking_level,
                thinking_level_layout=thinking_level_layout,
                thinking_level_transcription=thinking_level_transcription,
                run_consistency_check=run_consistency_check,
                geo_cache=geo_cache,
            )
            results.append(result)

            page_json = json_folder / f"page_{page_num:04d}.json"
            with open(page_json, "w", encoding="utf-8") as fh:
                json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)

        except Exception as exc:
            logger.error("Error processing %s: %s", image_path.name, exc, exc_info=True)

    results.sort(key=lambda r: r.page_number)

    combined_json = output_folder / "digital_edition_complete.json"
    with open(combined_json, "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in results], fh, ensure_ascii=False, indent=2)
    logger.info("Combined JSON saved: %s", combined_json)

    if geo_cache:
        cache_path = output_folder / "geocode_cache.json"
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(geo_cache, fh, ensure_ascii=False, indent=2)

    logger.info(
        "Done. Folios: %d | Entities: %d | Locations: %d",
        len(results),
        sum(len(r.entities) for r in results),
        sum(len(r.locations) for r in results),
    )
    return results

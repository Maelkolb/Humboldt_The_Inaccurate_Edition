"""Pipeline orchestration: per-page processing (detection → ensemble reading →
layout → NER → geocoding → optional ground-truth) and the book-level driver."""

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from tqdm import tqdm
from google import genai

from . import config
from .models import Entity, GeoLocation, Region, PageResult
from .imaging import load_image_rgb, encode_jpeg, JPEG_MIME, fit_longest_side
from .region_detection import detect_regions
from .whole_page_reading import read_whole_page, STRUCTURED_READING_PROMPT
from .transcription import transcribe_regions_ensemble, DEFAULT_MAX_WORKERS
from .layout import resolve_layout
from .ner import perform_ner
from .geocoding import geocode_entities
from .geo_consistency import validate_locations
from .ground_truth import (
    _build_gt_index,
    _norm_folio,
    gt_lookup,
    match_ground_truth_to_page,
)
from .tei_writer import write_tei_file

logger = logging.getLogger(__name__)

VALID_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_image_files(folder: str | Path) -> List[Path]:
    """Return all image files from folder, sorted in manuscript FOLIATION order.

    Sorting by raw filename is wrong: "H0017682__20r.jpg" sorts before
    "…__2r.jpg" (because '0' < 'r'), so ``--start/--end`` would slice a scattered,
    non-leading set of folios. We sort by the numeric folio key instead (recto
    before verso), so ``--end 5`` really means the first five folios.
    """
    folder = Path(folder)
    files = [
        folder / f
        for f in os.listdir(folder)
        if Path(f).suffix.lower() in VALID_IMAGE_EXTENSIONS
    ]
    return sorted(files, key=lambda p: (extract_page_number(p.name), p.name))


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
    ensemble_k: int | None = None,
    geo_cache: Optional[Dict] = None,
    run_geo_validation: bool = True,
    transcription_workers: int = DEFAULT_MAX_WORKERS,
    *,
    model_id_layout: str | None = None,
    model_id_transcription: str | None = None,
    model_id_merge: str | None = None,
    model_id_ner: str | None = None,
    model_id_ground_truth: str | None = None,
    model_id_geo_validation: str | None = None,
    thinking_level_merge: str | None = None,
    thinking_level_ner: str | None = None,
    thinking_level_ground_truth: str | None = None,
    thinking_level_geo_validation: str | None = None,
    gt_page: Optional[PageResult] = None,
) -> PageResult:
    """Run the full pipeline for one Humboldt journal page.

    Reading is the heterogeneous ensemble: detection → 3 candidate readings per
    region (M1 region crop + M2 whole-page + M3 structured whole-page) → an
    alignment-locked merge → a whole-page layout pass. ``model_id`` is the default
    for every stage; the ``model_id_*`` / ``thinking_level_*`` args override a
    stage. When ``gt_page`` is given, ground-truth matching runs and populates
    each region's ``ground_truth_content``.
    """
    image_path = Path(image_path)
    folio_label = extract_folio_label(image_path.name)
    logger.info("Processing folio %s (page %d): %s", folio_label, page_number, image_path.name)

    # Per-stage models (detection + reads fall back to model_id; merge/layout to best).
    detect_model = model_id_layout        or model_id
    read_model   = model_id_transcription or model_id
    merge_model  = model_id_merge         or config.MODEL_ID_MERGE_DEFAULT
    ner_model    = model_id_ner           or model_id
    # GT matching/alignment uses the best model (smarter span assignment).
    gt_model     = model_id_ground_truth  or config.MODEL_ID_MERGE_DEFAULT
    geo_val_model = model_id_geo_validation or model_id

    # Per-stage thinking levels.
    detect_thinking = thinking_level_layout        or thinking_level
    read_thinking   = thinking_level_transcription or thinking_level
    merge_thinking  = thinking_level_merge         or config.THINKING_LEVEL_MERGE
    ner_thinking    = thinking_level_ner           or thinking_level
    gt_thinking     = thinking_level_ground_truth  or "medium"
    geo_val_thinking = thinking_level_geo_validation or "low"
    k_crop = ensemble_k if ensemble_k is not None else config.ENSEMBLE_K_CROP

    # Load + encode the page ONCE per page (reused across every stage).
    page = load_image_rgb(image_path)
    page_bytes_full = (encode_jpeg(page), JPEG_MIME)                       # detection (native res)
    page_read = fit_longest_side(page, config.PAGE_READ_MAX_PX)
    page_bytes_read = (encode_jpeg(page_read), JPEG_MIME)                  # reads + merge + layout

    # Step 1 – Region detection.
    logger.info("  Step 1: Region detection (model: %s, thinking: %s)...", detect_model, detect_thinking)
    detected = detect_regions(client, image_path, detect_model, detect_thinking,
                              page_bytes=page_bytes_full)
    logger.info("  Detected %d regions", len(detected))

    # Step 2 – Whole-page candidate readings (M2 free, M3 structured/diverse),
    # run concurrently (independent calls; global LLM semaphore caps total load).
    # M3 uses a DIFFERENT model from M1/M2 so the ensemble has two model families.
    logger.info("  Step 2: Whole-page reads (M2 %s, M3 %s)...",
                read_model, config.MODEL_ID_STRUCTURED)
    with ThreadPoolExecutor(max_workers=2) as _read_pool:
        f_m2 = _read_pool.submit(
            read_whole_page, client, detected, read_model, read_thinking,
            page_bytes=page_bytes_read, stage="read_page",
            temperature=config.TEMPERATURE_READ)
        f_m3 = _read_pool.submit(
            read_whole_page, client, detected, config.MODEL_ID_STRUCTURED,
            config.THINKING_LEVEL_STRUCTURED,
            page_bytes=page_bytes_read, prompt_template=STRUCTURED_READING_PROMPT,
            stage="read_structured", temperature=config.TEMPERATURE_READ_STRUCTURED)
        m2 = f_m2.result()
        m3 = f_m3.result()

    # Step 3 – Per-region crop reads (M1) + alignment-locked merge.
    logger.info("  Step 3: Ensemble (crop x%d on %s + merge on %s, %d workers)...",
                k_crop, read_model, merge_model, transcription_workers)
    regions = transcribe_regions_ensemble(
        client, detected, read_model, read_thinking,
        page=page,
        k_crop=k_crop,
        page_candidates=[m2, m3],
        merge_model=merge_model,
        merge_thinking=merge_thinking,
        merge_crop_max_px=config.MERGE_CROP_MAX_PX,
        merge_page_bytes=(page_bytes_read if config.MERGE_WITH_PAGE else None),
        read_temperature=config.TEMPERATURE_READ,
        merge_temperature=config.TEMPERATURE_MERGE,
        max_workers=transcription_workers,
    )
    logger.info("  Read %d regions (ensemble)", len(regions))

    # Step 4 – Whole-page layout pass (dedup / contamination / bleed; no rewriting).
    consistency_issues: List[Dict] = []
    if run_consistency_check:
        logger.info("  Step 4: Layout pass (model: %s, thinking: %s)...", merge_model, merge_thinking)
        regions, consistency_issues = resolve_layout(
            client, regions, merge_model,
            thinking_level=merge_thinking,
            page_bytes=page_bytes_read,
            temperature=config.TEMPERATURE_MERGE,
        )
        logger.info("  Layout: %d decision(s) recorded.", len(consistency_issues))

    # Extract metadata
    entry_numbers = extract_entry_numbers(regions)
    page_languages = extract_page_languages(regions)

    # Steps 5–8 split into two independent branches that run concurrently: the
    # NER → geocoding → geo-validation chain and ground-truth matching both depend
    # only on the finalized regions, not on each other. Neither mutates `regions`
    # (GT-matching returns new region copies), so reading them in parallel is safe.
    def _ner_branch():
        full_text = build_full_text(regions)
        logger.info("  Step 5: NER on %d chars (model: %s, thinking: %s)...",
                    len(full_text), ner_model, ner_thinking)
        entities = perform_ner(client, full_text, entity_types, ner_model, ner_thinking)
        logger.info("  Found %d entities", len(entities))
        logger.info("  Step 6: Geocoding...")
        locations = geocode_entities(entities, cache=geo_cache)
        logger.info("  Geocoded %d locations", len(locations))
        geo_validation: List[Dict] = []
        if run_geo_validation and locations:
            logger.info("  Step 7: Geo-validation (model: %s, thinking: %s)...",
                        geo_val_model, geo_val_thinking)
            locations, geo_validation = validate_locations(
                client, locations, entities, full_text,
                model_id=geo_val_model, thinking_level=geo_val_thinking,
                geo_cache=geo_cache,
            )
        return full_text, entities, locations, geo_validation

    def _gt_branch():
        if gt_page is None:
            return regions
        logger.info("  Step 8: Ground-truth matching (model: %s, thinking: %s)...",
                    gt_model, gt_thinking)
        return match_ground_truth_to_page(
            client, image_path, regions, gt_page,
            model_id=gt_model, thinking_level=gt_thinking,
        )

    with ThreadPoolExecutor(max_workers=2) as _branch_pool:
        f_ner = _branch_pool.submit(_ner_branch)
        f_gt = _branch_pool.submit(_gt_branch)
        full_text, entities, locations, geo_validation = f_ner.result()
        regions = f_gt.result()

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
        consistency_issues=consistency_issues,
        geo_validation=geo_validation,
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
    ensemble_k: int | None = None,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    run_geo_validation: bool = True,
    transcription_workers: int = DEFAULT_MAX_WORKERS,
    folio_workers: int = 1,
    resume: bool = True,
    *,
    model_id_layout: str | None = None,
    model_id_transcription: str | None = None,
    model_id_merge: str | None = None,
    model_id_ner: str | None = None,
    model_id_ground_truth: str | None = None,
    model_id_geo_validation: str | None = None,
    thinking_level_merge: str | None = None,
    thinking_level_ner: str | None = None,
    thinking_level_ground_truth: str | None = None,
    thinking_level_geo_validation: str | None = None,
    ground_truth_tei: str | Path | None = None,
    book_title: str = "Humboldt – Travel Journal (automated transcription)",
) -> List[PageResult]:
    """Process all pages in image_folder through the full pipeline.

    `model_id` is the default applied to every LLM stage. Pass any of
    `model_id_layout`, `model_id_transcription`, `model_id_consistency`,
    `model_id_ner`, `model_id_ground_truth` to override the model for that
    stage only.

    When ``ground_truth_tei`` is provided, the optional Step 5
    (ground-truth matching) is enabled: for each page whose folio label
    appears in the GT TEI, every region gets ``ground_truth_content``
    populated. The HTML viewer then exposes a Gemini / Ground Truth / Diff
    toggle on those pages.
    """
    image_folder = Path(image_folder)
    output_folder = Path(output_folder)
    json_folder = output_folder / "json"
    json_folder.mkdir(parents=True, exist_ok=True)

    image_files = get_image_files(image_folder)
    if not image_files:
        logger.error("No images found in %s", image_folder)
        return []

    logger.info("Found %d images in %s", len(image_files), image_folder)
    logger.info(
        "Models — default: %s | detect: %s | reads M1/M2: %s | read M3: %s | "
        "merge/layout: %s | ner: %s | ground_truth: %s",
        model_id,
        model_id_layout        or model_id,
        model_id_transcription or model_id,
        config.MODEL_ID_STRUCTURED,
        model_id_merge         or config.MODEL_ID_MERGE_DEFAULT,
        model_id_ner           or model_id,
        model_id_ground_truth  or config.MODEL_ID_MERGE_DEFAULT,
    )

    # Build GT index once if ground-truth-matching is enabled
    gt_index: Dict[str, PageResult] = {}
    if ground_truth_tei:
        logger.info("Loading ground-truth TEI: %s", ground_truth_tei)
        try:
            gt_index = _build_gt_index(ground_truth_tei)
        except Exception as exc:
            logger.error(
                "Failed to load ground-truth TEI (%s) — proceeding without GT.",
                exc,
            )
            gt_index = {}

    subset = image_files[start_page:end_page]
    logger.info("Processing %d pages (folio_workers=%d)...", len(subset), folio_workers)

    geo_cache: Dict = {}              # shared; geocoding serialises its own access
    results: List[PageResult] = []
    gt_folios_unmatched: List[str] = []   # folios with no GT page in the TEI
    _collect_lock = threading.Lock()

    def _process_one(idx: int, image_path: Path) -> None:
        page_num = extract_page_number(image_path.name) or (idx + 1)
        page_json = json_folder / f"page_{page_num:04d}.json"

        # Resume: reuse an already-completed page (non-empty) from a prior run
        # instead of reprocessing it. A 0-region page (a prior failure) is redone.
        if resume and page_json.exists():
            try:
                with open(page_json, "r", encoding="utf-8") as fh:
                    existing = PageResult.from_dict(json.load(fh))
                if existing.regions:
                    with _collect_lock:
                        results.append(existing)
                    logger.info("  Resume: %s already done (%s) — skipping.",
                                image_path.name, page_json.name)
                    return
            except Exception as exc:
                logger.warning("  Resume: %s unreadable (%s) — reprocessing.",
                               page_json.name, exc)

        # Look up matching GT page (if any) by normalised folio label.
        gt_page = None
        if gt_index:
            gt_page = gt_lookup(gt_index, extract_folio_label(image_path.name))
            if gt_page is None:
                key = _norm_folio(extract_folio_label(image_path.name))
                with _collect_lock:
                    gt_folios_unmatched.append(key)
                logger.info(
                    "No GT folio in TEI for %s (key=%r) — page will have no "
                    "Ground-Truth/Diff tabs.", image_path.name, key,
                )
        try:
            result = process_page(
                client, image_path, page_num, entity_types,
                model_id, thinking_level,
                thinking_level_layout=thinking_level_layout,
                thinking_level_transcription=thinking_level_transcription,
                run_consistency_check=run_consistency_check,
                ensemble_k=ensemble_k,
                geo_cache=geo_cache,
                run_geo_validation=run_geo_validation,
                transcription_workers=transcription_workers,
                model_id_layout=model_id_layout,
                model_id_transcription=model_id_transcription,
                model_id_merge=model_id_merge,
                model_id_ner=model_id_ner,
                model_id_ground_truth=model_id_ground_truth,
                model_id_geo_validation=model_id_geo_validation,
                thinking_level_merge=thinking_level_merge,
                thinking_level_ner=thinking_level_ner,
                thinking_level_ground_truth=thinking_level_ground_truth,
                thinking_level_geo_validation=thinking_level_geo_validation,
                gt_page=gt_page,
            )
            page_json = json_folder / f"page_{page_num:04d}.json"
            with open(page_json, "w", encoding="utf-8") as fh:
                json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)
            with _collect_lock:
                results.append(result)
        except Exception as exc:
            logger.error("Error processing %s: %s", image_path.name, exc, exc_info=True)

    # Folios run concurrently (folio_workers); the global LLM semaphore in
    # src/llm.py caps total simultaneous API calls regardless of this fan-out.
    workers = max(1, min(folio_workers, len(subset)))
    if workers == 1:
        for idx, image_path in enumerate(tqdm(subset, desc="Folios", unit="fol")):
            _process_one(idx, image_path)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_process_one, idx, ip)
                       for idx, ip in enumerate(subset)]
            for _ in tqdm(as_completed(futures), total=len(futures),
                          desc="Folios", unit="fol"):
                pass

    results.sort(key=lambda r: r.page_number)

    # ----- Ground-truth coverage summary (diagnostic) -----
    if gt_index:
        with_gt = sum(1 for r in results if r.has_ground_truth)
        logger.info(
            "Ground-truth coverage: %d / %d pages show GT/Diff tabs "
            "(%d folios had no GT page in the TEI%s).",
            with_gt, len(results), len(gt_folios_unmatched),
            (": " + ", ".join(gt_folios_unmatched[:12])
             + ("…" if len(gt_folios_unmatched) > 12 else ""))
            if gt_folios_unmatched else "",
        )
        in_tei_no_gt = [
            r.folio_label for r in results
            if not r.has_ground_truth
            and gt_lookup(gt_index, r.folio_label) is not None
        ]
        if in_tei_no_gt:
            logger.warning(
                "  %d page(s) HAD a matching TEI folio but produced no GT "
                "matches (see the per-page 'GT matched' / 'no usable matches' "
                "logs above): %s",
                len(in_tei_no_gt),
                ", ".join(in_tei_no_gt[:12])
                + ("…" if len(in_tei_no_gt) > 12 else ""),
            )

    # ----- Output: combined JSON -----
    combined_json = output_folder / "digital_edition_complete.json"
    with open(combined_json, "w", encoding="utf-8") as fh:
        json.dump([r.to_dict() for r in results], fh, ensure_ascii=False, indent=2)
    logger.info("Combined JSON saved: %s", combined_json)

    # ----- Output: full-book TEI (standard output file) -----
    try:
        tei_path = output_folder / "digital_edition.tei.xml"
        write_tei_file(results, tei_path, title=book_title)
    except Exception as exc:
        logger.error("Failed to write TEI document: %s", exc, exc_info=True)

    if geo_cache:
        cache_path = output_folder / "geocode_cache.json"
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(geo_cache, fh, ensure_ascii=False, indent=2)

    logger.info(
        "Done. Folios: %d | Entities: %d | Locations: %d | GT-matched pages: %d",
        len(results),
        sum(len(r.entities) for r in results),
        sum(len(r.locations) for r in results),
        sum(1 for r in results if r.has_ground_truth),
    )
    return results

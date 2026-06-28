#!/usr/bin/env python3
"""CLI: run the Humboldt journal digitization pipeline.

Per page: region detection → ensemble reading (region crop + two whole-page reads,
alignment-locked merge) → layout pass → NER → geocoding → geo-validation →
optional ground-truth matching. Then writes the HTML edition bundle and TEI XML.

Usage:
    python scripts/process_journal.py --images images/ --out output/
    python scripts/process_journal.py --images images/ --out output/ --end 5

Requires GEMINI_API_KEY in the environment or a .env file.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google import genai
from src import config, process_book, generate_html_edition
from src.logging_setup import configure_logging


def main() -> None:
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Humboldt Journal Digital Edition – run the full pipeline."
    )
    parser.add_argument(
        "--images", default=str(config.IMAGE_FOLDER),
        help="Folder containing journal page images",
    )
    parser.add_argument(
        "--out", default=str(config.OUTPUT_FOLDER),
        help="Output folder",
    )
    parser.add_argument(
        "--start", type=int, default=None,
        help="0-based start index (inclusive)",
    )
    parser.add_argument(
        "--end", type=int, default=None,
        help="0-based end index (exclusive)",
    )
    parser.add_argument(
        "--model", default=config.MODEL_ID,
        help=f"Default Gemini model ID for all stages (default: {config.MODEL_ID})",
    )
    parser.add_argument(
        "--model-layout", default=config.MODEL_ID_LAYOUT,
        help="Override model for region detection (default: same as --model)",
    )
    parser.add_argument(
        "--model-transcription", default=config.MODEL_ID_TRANSCRIPTION,
        help="Override model for the region/whole-page reads (default: same as --model)",
    )
    parser.add_argument(
        "--model-merge", default=config.MODEL_ID_MERGE,
        help=f"Override model for the merge resolver + layout pass "
             f"(default: {config.MODEL_ID_MERGE_DEFAULT})",
    )
    parser.add_argument(
        "--model-ner", default=config.MODEL_ID_NER,
        help="Override model for NER (default: same as --model)",
    )
    parser.add_argument(
        "--model-geo-validation", default=config.MODEL_ID_GEO_VALIDATION,
        help="Override model for geolocation validation (default: same as --model)",
    )
    parser.add_argument(
        "--model-ground-truth", default=None,
        help="Override model for ground-truth matching (default: same as --model)",
    )
    parser.add_argument(
        "--thinking", default=config.THINKING_LEVEL,
        choices=["none", "low", "medium", "high"],
        help="Default thinking level (fallback if per-stage not set)",
    )
    parser.add_argument(
        "--thinking-layout", default=config.THINKING_LEVEL_LAYOUT,
        choices=["none", "low", "medium", "high"],
        help="Thinking level for region detection (default: high)",
    )
    parser.add_argument(
        "--thinking-transcription", default=config.THINKING_LEVEL_TRANSCRIPTION,
        choices=["none", "low", "medium", "high"],
        help="Thinking level for the reads (default: low)",
    )
    parser.add_argument(
        "--thinking-merge", default=None,
        choices=["none", "low", "medium", "high"],
        help="Thinking level for the merge resolver + layout pass (default: medium)",
    )
    parser.add_argument(
        "--thinking-ner", default=None,
        choices=["none", "low", "medium", "high"],
        help="Thinking level for NER (default: same as --thinking)",
    )
    parser.add_argument(
        "--thinking-geo-validation", default=None,
        choices=["none", "low", "medium", "high"],
        help="Thinking level for geolocation validation (default: low)",
    )
    parser.add_argument(
        "--thinking-ground-truth", default="medium",
        choices=["none", "low", "medium", "high"],
        help="Thinking level for ground-truth matching (default: medium)",
    )
    parser.add_argument(
        "--transcription-workers", type=int, default=6,
        help="Concurrent workers for the per-region reading (default: 6)",
    )
    parser.add_argument(
        "--ensemble-k", type=int, default=config.ENSEMBLE_K_CROP,
        help=f"Number of per-region CROP reads (M1) per region "
             f"(default: {config.ENSEMBLE_K_CROP}); the whole-page (M2) and "
             f"whole-page-structured (M3) candidates are always added.",
    )
    parser.add_argument(
        "--no-consistency", action="store_true",
        help="Skip Step 4 (the whole-page layout pass: dedup / contamination / bleed).",
    )
    parser.add_argument(
        "--no-geo-validation", action="store_true",
        help="Skip the text-based geolocation validation.",
    )
    parser.add_argument(
        "--ground-truth-tei", default=None, metavar="PATH",
        help=(
            "Optional: path to a ground-truth TEI XML file (e.g. from "
            "edition-humboldt.de). When given, runs an extra step per page "
            "to match the GT text to the detected regions; the HTML viewer "
            "then exposes a Gemini / Ground Truth / Diff toggle."
        ),
    )
    parser.add_argument(
        "--embed-images", action="store_true",
        help="Embed facsimiles as base64 in index.html instead of copying "
             "them into the bundle's facsimiles/ folder",
    )
    parser.add_argument(
        "--image-ref-prefix", default=None,
        help="Reference facsimiles via this URL/path prefix instead of "
             "bundling them (for externally hosted images)",
    )
    parser.add_argument(
        "--title", default="Alexander von Humboldt — Journals",
        help="Title for the HTML edition",
    )
    parser.add_argument(
        "--subtitle", default="Digital Scholarly Edition",
        help="Subtitle for the HTML edition",
    )
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY") or config.GEMINI_API_KEY
    if not api_key:
        print("Error: GEMINI_API_KEY not set. Add it to your environment or .env file.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    read_model = args.model_transcription or args.model
    merge_model = args.model_merge or config.MODEL_ID_MERGE_DEFAULT
    print(f"Gemini client ready – default model: {args.model}")
    print(f"  Ensemble reads: crop x{args.ensemble_k} (M1) + whole-page (M2) + "
          f"structured (M3) on {read_model}; alignment-locked merge + layout on {merge_model}")
    print(f"  detect={args.model_layout or args.model}, ner={args.model_ner or args.model}")
    print(f"  Layout pass: {'OFF' if args.no_consistency else 'ON'} | "
          f"region workers: {args.transcription_workers}")

    results = process_book(
        client=client,
        image_folder=args.images,
        output_folder=args.out,
        entity_types=config.ENTITY_TYPES,
        model_id=args.model,
        thinking_level=args.thinking,
        thinking_level_layout=args.thinking_layout,
        thinking_level_transcription=args.thinking_transcription,
        run_consistency_check=not args.no_consistency,
        ensemble_k=args.ensemble_k,
        start_page=args.start,
        end_page=args.end,
        run_geo_validation=not args.no_geo_validation,
        transcription_workers=args.transcription_workers,
        model_id_layout=args.model_layout,
        model_id_transcription=args.model_transcription,
        model_id_merge=args.model_merge,
        model_id_ner=args.model_ner,
        model_id_ground_truth=args.model_ground_truth,
        model_id_geo_validation=args.model_geo_validation,
        thinking_level_merge=args.thinking_merge,
        thinking_level_ner=args.thinking_ner,
        thinking_level_ground_truth=args.thinking_ground_truth,
        thinking_level_geo_validation=args.thinking_geo_validation,
        ground_truth_tei=args.ground_truth_tei,
        book_title=args.title,
    )

    if results:
        edition_zip = generate_html_edition(
            results=results,
            output_path=Path(args.out) / "humboldt_edition.html",
            title=args.title,
            subtitle=args.subtitle,
            entity_colors=config.ENTITY_COLORS,
            entity_labels=config.ENTITY_LABELS,
            region_colors=config.REGION_COLORS,
            region_labels=config.REGION_LABELS,
            image_folder=args.images,
            image_ref_prefix=args.image_ref_prefix,
            embed_images=args.embed_images,
        )
        print(f"\nDigital edition bundle: {edition_zip.with_suffix('')}/")
        print(f"Digital edition archive: {edition_zip}")

    total_ents = sum(len(r.entities) for r in results)
    total_locs = sum(len(r.locations) for r in results)
    print(f"Folios processed: {len(results)}")
    print(f"Entities annotated: {total_ents}")
    print(f"Locations geocoded: {total_locs}")


if __name__ == "__main__":
    main()

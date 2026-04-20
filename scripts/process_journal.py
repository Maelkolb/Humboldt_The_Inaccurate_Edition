#!/usr/bin/env python3
"""
CLI: Run the Humboldt Journal digitization pipeline.

Pipeline steps:
  1. Region Detection (Gemini) – Humboldt-specific region types
  2. Transcription – Kurrentschrift + multilingual scholarly transcription
  3. Entity Annotation (NER) – scientific/geographic entities
  4. Georeferencing (Nominatim) – with historical name mapping
  5. HTML Digital Edition – scholarly side-by-side facsimile + transcription

Usage:
    python scripts/process_journal.py --images images/ --out output/
    python scripts/process_journal.py --images images/ --out output/ --embed-images --end 5

Requires GEMINI_API_KEY in environment or .env file.
"""

import argparse
import logging
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

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
        help=f"Gemini model ID (default: {config.MODEL_ID})",
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
        help="Thinking level for transcription (default: low)",
    )
    parser.add_argument(
        "--embed-images", action="store_true",
        help="Embed facsimile images as base64 in the HTML",
    )
    parser.add_argument(
        "--image-ref-prefix", default=None,
        help="Reference images via this path prefix instead of embedding",
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
    print(f"Gemini client ready – model: {args.model}")
    print(f"  Thinking: layout={args.thinking_layout}, transcription={args.thinking_transcription}")

    results = process_book(
        client=client,
        image_folder=args.images,
        output_folder=args.out,
        entity_types=config.ENTITY_TYPES,
        model_id=args.model,
        thinking_level=args.thinking,
        thinking_level_layout=args.thinking_layout,
        thinking_level_transcription=args.thinking_transcription,
        start_page=args.start,
        end_page=args.end,
    )

    if results:
        html_path = generate_html_edition(
            results=results,
            output_path=Path(args.out) / "humboldt_edition.html",
            title=args.title,
            subtitle=args.subtitle,
            entity_colors=config.ENTITY_COLORS,
            entity_labels=config.ENTITY_LABELS,
            region_colors=config.REGION_COLORS,
            region_labels=config.REGION_LABELS,
            image_folder=args.images if args.embed_images else None,
            image_ref_prefix=args.image_ref_prefix,
        )
        print(f"\nDigital edition: {html_path}")

    total_ents = sum(len(r.entities) for r in results)
    total_locs = sum(len(r.locations) for r in results)
    print(f"Folios processed: {len(results)}")
    print(f"Entities annotated: {total_ents}")
    print(f"Locations geocoded: {total_locs}")


if __name__ == "__main__":
    main()

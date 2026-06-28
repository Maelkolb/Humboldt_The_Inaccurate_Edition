"""Process Humboldt journal: Reise. 1790. England (H0017682).

The English travel journal (the one the pipeline was first optimized on).
Run from the project root with the venv:
    python run_england.py
"""

import os
import sys
import logging
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

# Paths for THIS journal (verified)
JOURNAL_DIR = Path(r"C:/Users/totom/Projects/Humboldt/H0017682_humboldt")
IMAGE_FOLDER = JOURNAL_DIR / "images"            # 21 images live in images/
GT_TEI = JOURNAL_DIR / "tei" / "H0017682.xml"
OUTPUT_FOLDER = PROJECT_DIR / "output_england_full"
OUTPUT_FOLDER.mkdir(exist_ok=True)

# Settings
MAX_PAGES = None          # None = all 21 folios; set e.g. 3 for a quick test
RUN_CONSISTENCY_CHECK = True
TRANSCRIPTION_WORKERS = 10    # crop-read/merge workers per folio
FOLIO_WORKERS = 3            # folios processed concurrently
LLM_MAX_CONCURRENCY = 8      # global cap on simultaneous Gemini calls (the real throttle)

# API key (.env fallback for local runs)
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
except Exception:
    pass
assert os.environ.get("GEMINI_API_KEY"), "Set GEMINI_API_KEY (env var or .env)"

from src import config, process_book, generate_html_edition
from src.logging_setup import configure_logging
from src import llm
from google import genai
from google.genai import types

configure_logging("INFO")   # clean logs (no HTTP/AFC spam)
llm.set_max_concurrency(LLM_MAX_CONCURRENCY)

# 180s per-request timeout: a dropped/half-open connection errors out and is
# retried with backoff instead of hanging the run forever.
client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(timeout=180_000),
)
print("Gemini client ready\n")

results = process_book(
    client=client,
    image_folder=str(IMAGE_FOLDER),
    output_folder=str(OUTPUT_FOLDER),
    entity_types=config.ENTITY_TYPES,
    model_id=config.MODEL_ID,
    # Per-stage thinking from config: detection "high", per-region reads "low".
    thinking_level=config.THINKING_LEVEL,                              # fallback (NER, etc.)
    thinking_level_layout=config.THINKING_LEVEL_LAYOUT,               # detection: high
    thinking_level_transcription=config.THINKING_LEVEL_TRANSCRIPTION,  # M1/M2 reads: low
    transcription_workers=TRANSCRIPTION_WORKERS,
    folio_workers=FOLIO_WORKERS,
    run_consistency_check=RUN_CONSISTENCY_CHECK,
    start_page=None,
    end_page=MAX_PAGES,
    ground_truth_tei=str(GT_TEI),                # enables GT / Diff tabs + eval
    book_title="Alexander von Humboldt - Reise. 1790. England (Tagebuch der England-Reise)",
)

print("\n" + "=" * 60)
print(f"Pipeline complete!  Folios: {len(results)}  "
      f"Entities: {sum(len(r.entities) for r in results)}  "
      f"Locations: {sum(len(r.locations) for r in results)}")

# HTML edition (new name + metadata for this journal)
html_path = generate_html_edition(
    results=results,
    output_path=OUTPUT_FOLDER / "humboldt_england_1790.html",
    title="Alexander v. Humboldt - The Inaccurate Edition",
    subtitle="Reise. 1790. England - Tagebuch der England-Reise",
    entity_colors=config.ENTITY_COLORS,
    entity_labels=config.ENTITY_LABELS,
    region_colors=config.REGION_COLORS,
    region_labels=config.REGION_LABELS,
    image_folder=str(IMAGE_FOLDER),   # embeds facsimiles as base64
)
print(f"\nEdition ready: {html_path}  ({html_path.stat().st_size / 1e6:.1f} MB)")

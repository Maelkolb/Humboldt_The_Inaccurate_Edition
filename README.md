# Humboldt Journal – Digital Edition Pipeline

A specialized LLM-powered workflow for creating a scholarly digital edition of **Alexander von Humboldt's handwritten scientific field journals**. Uses **Google Gemini 3** with prompts deeply tailored to Humboldt's difficult handwriting, complex page layouts, and multilingual content.

## Why a specialized pipeline?

Humboldt's journal pages present extraordinary challenges for automated transcription:

- **Kurrentschrift** with extremely fluid, hasty strokes and personal abbreviations
- **Complex layouts** — numbered entries, marginal notes, astronomical calculation tables, crossed-out passages, interlinear additions, pen sketches of landscapes
- **Multilingual** — German, French, and Latin mixed freely, often within a single sentence
- **Non-linear reading order** — marginalia relate to specific entries, insertions marked with reference signs
- **Scientific notation** — degree/minute/second symbols, coordinate tables, instrument specifications with prices

This pipeline addresses all of these with custom region types, editorial conventions, and deeply specialized prompts.

## Pipeline

| Step | Module | Description |
|------|--------|-------------|
| 1. Region Detection | `region_detection.py` | Identifies Humboldt-specific regions: entry headings, main text, marginal notes, calculation blocks, observation tables, sketches, crossed-out passages, interlinear additions, bibliographic references, coordinates, instrument lists |
| 2. Transcription | `transcription.py` | Each detected region is cropped from the page (with generous margins) and transcribed on its **own** focused request, in parallel. Short per-region-type prompts replace the former single whole-page call with the full rulebook, keeping the model's attention on one region at a time. Tracks languages and table structure; defers editorial/structural judgement to Step 2.5. |
| 2.5. Consistency Check | `consistency_check.py` | **Multimodal whole-page QA pass** — now the home of the editorial rulebook. Looking at the page image alongside every region, it (a) reconciles overlapping/duplicate transcript lines that the padded per-region crops can introduce, deciding line ownership from the bounding boxes, and fixes contaminated main-text and language mismatches; (b) supplies editorial interpretation (marginal role, writing layer); and (c) attempts to resolve every word marked `[?]` directly from the ink, dropping the marker only on a confident reading. Each region keeps a snapshot (`content_pre_consistency`, `uncertain_readings_pre_consistency`) of the Step-2 output so the QA pass can be audited per region. |
| 3. Entity Annotation | `ner.py` | NER for persons, locations, institutions, instruments, publications, celestial objects, measurements, natural objects |
| 4. Georeferencing | `geocoding.py` | Location resolution with a historical place-name mapping (Oedenburg→Sopron, Preßburg→Bratislava, etc.) |
| 4.5. Geolocation Validation | `geo_consistency.py` | **Text-based QA pass.** Runs after NER + geocoding and judges, from the page text alone, whether each resolved coordinate plausibly is the place Humboldt named. Implausible hits (non-places, wrong-continent homonyms, misread fragments that latched onto an unrelated famous place) are dropped; per-location verdicts are stored in `geo_validation` for auditing. Conservative and fails open. |
| 5. Ground-Truth Matching | `ground_truth.py` | **Optional**, enabled by `--ground-truth-tei PATH`. For each page whose folio appears in an externally-provided ground-truth TEI (e.g. from [edition-humboldt.de](https://edition-humboldt.de/)), every region's matching GT text is attached to the region. The HTML viewer then exposes a **Gemini / Ground Truth / Diff** toggle for that page. |
| 6. HTML Edition | `html_generator.py` | Self-contained digital edition with side-by-side facsimile + transcription, editorial apparatus, entity highlighting, map view, per-page TEI download |
| 7. TEI Export | `tei_writer.py` | Always emits `digital_edition.tei.xml` in the output folder, following the same structural conventions as edition-humboldt digital (`<pb>`, `<head>`, `<note place="…">`, `<del rendition="#s">`, `<hi rendition="#u">`, `<unclear>`, `<gap/>`, `<supplied>`, `<fw type="folNum"/"catch">`, …). |

## Humboldt-Specific Region Types

| Region Type | Description | Visual Style |
|---|---|---|
| `entry_heading` | Numbered entry header (N. 50-52) | Blue heading with divider |
| `main_text` | Primary journal prose | Standard text |
| `marginal_note` | Notes in margins | Purple background, italic |
| `calculation` | Mathematical/astronomical calculations | Monospace, warm background |
| `observation_table` | Structured observational data | Tabular, monospace numbers |
| `sketch` | Pen drawings, landscape profiles | Dashed border, description |
| `crossed_out` | Struck-through passages | Strikethrough, red border |
| `interlinear` | Text added between lines | Yellow background |
| `bibliographic_ref` | Citations of publications | Brown left border |
| `coordinates` | Geographic coordinate notations | Blue background, monospace |
| `instrument_list` | Scientific instruments with prices | Orange background |

## Quick Start

### 1. Install

```bash
git clone <repo-url>
cd humboldt-edition
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set your API key

```bash
export GEMINI_API_KEY="your-key-from-aistudio.google.com"
```

### 3. Add journal images

Place your Humboldt journal page images in the `images/` folder. The pipeline handles filenames like:
- `H0019734__67r.jpg` (SBB Berlin format → folio "67r")
- `page_001.jpg` (generic format)

### 4. Run the pipeline

```bash
# Process all pages with embedded facsimile images
python scripts/process_journal.py --images images/ --out output/ --embed-images

# Process a subset for testing (first 3 pages)
python scripts/process_journal.py --images images/ --out output/ --embed-images --end 3

# Use higher thinking for difficult pages
python scripts/process_journal.py --images images/ --out output/ --thinking high --embed-images

# Compare against an existing scholarly transcription
# (e.g. the corresponding TEI from edition-humboldt.de) — the HTML viewer
# then gets a per-page Gemini / Ground Truth / Diff toggle.
python scripts/process_journal.py \
    --images images/ --out output/ --embed-images \
    --ground-truth-tei reference/H0017682.xml
```


## Output Files

```
output/
├── json/
│   ├── page_0001.json                # Per-page results (regions, entities, metadata)
│   └── ...
├── digital_edition_complete.json     # All results
├── digital_edition.tei.xml           # Full-book TEI XML (always written)
├── humboldt_edition/                 # Self-contained edition bundle
│   ├── index.html                    #   • side-by-side facsimile + transcription
│   ├── assets/edition.css            #   • stylesheet (external)
│   ├── assets/edition.js             #   • behaviour: nav, zoom, search, toggles
│   ├── tei/folio_<label>.tei.xml     #   • one real TEI file per folio (download)
│   ├── tei/digital_edition.tei.xml   #   • full-book TEI (copied in)
│   └── facsimiles/<image>            #   • page images (unless --embed-images
│                                     #     or --image-ref-prefix is used)
├── humboldt_edition.zip              # The bundle above, zipped for distribution
└── geocode_cache.json
```

The HTML edition offers a Gemini / Ground Truth / Diff toggle (only on pages
where `--ground-truth-tei` produced a match), a Document/Reading view switch,
per-page TEI download, entity highlighting, and a Leaflet map of geolocated
places.


## Editorial Conventions

The transcription follows diplomatic conventions:
- Original spelling preserved 
- Uncertain readings marked `[?]`
- Crossed-out text rendered with strikethrough
- Interlinear additions clearly marked
- Languages tracked per region (DE/FR/LA/ESP badges)
- Editorial notes for ink changes, illegible passages, later additions

## Ground-Truth Comparison (optional)

When the pipeline is run with `--ground-truth-tei PATH`, every detected
region on every page is matched against the corresponding text in the
provided ground-truth TEI (typically the scholarly transcription published
on [edition-humboldt.de](https://edition-humboldt.de/)).

For each page where the GT folio is found:

1. A multimodal Gemini call receives the image, the detected bounding
   boxes + Gemini's own (often noisy) transcription, **and** the full GT
   text for that folio.
2. The model returns one matched GT segment per detected region.
3. Those matches are stored alongside the Gemini transcription on each
   region — both end up in the JSON, neither overwrites the other.



## License

MIT

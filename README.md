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
| 2. Transcription | `transcription.py` | Scholarly diplomatic transcription, tracking languages, noting editorial observations |
| 3. Entity Annotation | `ner.py` | NER for persons, locations, institutions, instruments, publications, celestial objects, measurements, natural objects |
| 4. Georeferencing | `geocoding.py` | Location resolution with a historical place-name mapping (Oedenburg→Sopron, Preßburg→Bratislava, etc.) |
| 5. HTML Edition | `html_generator.py` |digital edition with side-by-side facsimile + transcription, editorial apparatus, entity highlighting, map view |

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
```


## Output Files

```
output/
├── json/
│   ├── page_0001.json          # Per-page results (regions, entities, metadata)
│   └── ...
├── digital_edition_complete.json # All results
├── humboldt_edition.html       # Interactive scholarly HTML edition
└── geocode_cache.json
```

## Editorial Conventions

The transcription follows diplomatic conventions:
- Original spelling preserved 
- Uncertain readings marked `[?]`
- Crossed-out text rendered with strikethrough
- Interlinear additions clearly marked
- Languages tracked per region (DE/FR/LA/ESP badges)
- Editorial notes for ink changes, illegible passages, later additions

## License

MIT

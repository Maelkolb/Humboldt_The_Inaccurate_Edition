# Humboldt – The Inaccurate Edition

An LLM-powered pipeline for producing scholarly digital editions of **Alexander von Humboldt's handwritten scientific field journals**. Runs on Google Gemini and outputs a self-contained HTML viewer alongside TEI-P5 XML, with optional comparison against the existing scholarly transcription from [edition-humboldt.de](https://edition-humboldt.de/).

Currently supports three corpora:

| Journal | File | Runner |
|---------|------|--------|
| *Reise. 1790. England* (H0017682) | `run_england.py` | 21 folios, GT TEI available |
| *Amerikanische Reisetagebücher* (H0019734) | `run_america.py` | equinoctial journals |
| *Österreich-Reise* | `run_austr.py` | European trip 1797/98 |

---

## Why a specialised pipeline?

Humboldt's journals resist generic OCR and out-of-the-box LLM transcription:

- **Kurrentschrift** — fluid, hasty strokes with personal abbreviations and ligatures
- **Complex layouts** — numbered entries, marginal annotations, astronomical calculation tables, crossed-out passages, interlinear insertions, pen sketches
- **Multilingual** — German, French and Latin mixed freely, often mid-sentence
- **Non-linear reading order** — marginalia keyed to specific passages by reference signs
- **Scientific notation** — coordinate tables, instrument lists with prices, barometric readings

The pipeline addresses each of these with a custom region taxonomy, editorial conventions, and deeply tailored prompts.

---

## Pipeline architecture

Each folio goes through the following stages in `src/pipeline.py`:

```
Image
  │
  ▼
[1] Region detection          region_detection.py     thinking: high
  │   Humboldt-specific layout regions (entry headings, marginalia,
  │   calculation blocks, sketches, …)
  │
  ├──▶ [2a] M2 whole-page free read     ─┐
  │         whole_page_reading.py        │  concurrent
  └──▶ [2b] M3 whole-page structured ───┘  (different model → diversity)
  │         whole_page_reading.py
  │
  ▼
[3] Per-region crop reads (M1 × k) + alignment-locked merge
      transcription.py  ←→  consensus.py
      Each region is cropped (neighbours masked) and read k times.
      M1 crops + M2/M3 whole-page candidates are merged per region:
        • tokens ALL candidates agree on → locked automatically
        • disagreements → resolved by the best model from crop + page
  │
  ▼
[4] Layout pass                layout.py               thinking: medium
      Whole-page deduplication / contamination / bleed-over fix.
      No rewriting — only line ownership decisions across regions.
  │
  ├──────────────────────────┐
  ▼                          ▼
[5] NER              [8] Ground-truth matching   (concurrent)
[6] Geocoding              ground_truth.py
[7] Geo-validation
      ner.py / geocoding.py / geo_consistency.py
  │
  ▼
[9]  HTML edition bundle     src/html_edition/
[10] TEI-P5 XML              tei_writer.py
```

### The ensemble reading strategy

The core innovation is a **heterogeneous ensemble** with three reading modes per folio:

| Mode | What it does | Model | Thinking | Temperature |
|------|-------------|-------|----------|-------------|
| M1 (crops) | Reads each region from its own masked crop, k times | `gemini-3-flash-preview` | low | 0.2 |
| M2 (whole-page free) | Reads all regions from the full page simultaneously | `gemini-3-flash-preview` | low | 0.2 |
| M3 (whole-page structured) | Same but with a diversity-oriented prompt at high temperature | `gemini-3.5-flash` | medium | 1.0 |

All three candidate sets feed into `consensus.py`, which locks tokens that every candidate agrees on and sends only the contested spans to `gemini-3.5-flash` (+ the crop and page image) to adjudicate.

The layout pass then runs a single whole-page call to fix line-boundary bleed-over and cross-region duplicate lines that the padded crops can introduce — without touching any transcribed content it agrees with.

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/Maelkolb/Humboldt_The_Inaccurate_Edition.git
cd Humboldt_The_Inaccurate_Edition
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set your API key

```bash
# env var
export GEMINI_API_KEY="your-key-from-aistudio.google.com"

# or put it in a .env file in the project root
echo GEMINI_API_KEY=your-key > .env
```

### 3. Add journal images

Put your page images in an `images/` folder (or point `--images` at any folder).
Recognised filename patterns:

- `H0017682__67r.jpg` — SBB Berlin format; folio label extracted automatically
- `page_001.jpg` — generic sequential format

### 4. Run

**Using a runner script** (pre-configured for one journal):

```bash
python run_england.py     # England 1790
python run_america.py     # American journals
python run_austr.py       # Austria 1797/98
```

Edit the `JOURNAL_DIR`, `IMAGE_FOLDER`, `OUTPUT_FOLDER` paths at the top of the script before running.

**Using the generic CLI:**

```bash
# All pages, embed facsimiles in the HTML
python scripts/process_journal.py --images images/ --out output/ --embed-images

# First 5 pages only (testing)
python scripts/process_journal.py --images images/ --out output/ --end 5

# With a ground-truth TEI for comparison tabs
python scripts/process_journal.py \
    --images images/ --out output/ --embed-images \
    --ground-truth-tei path/to/H0017682.xml
```

---

## CLI reference

```
--images PATH              Folder of journal page images
--out PATH                 Output folder
--start N / --end N        0-based slice of the image list
--model ID                 Default Gemini model for all stages
--model-layout ID          Override model for region detection
--model-transcription ID   Override model for M1/M2/M3 reads
--model-merge ID           Override model for the merge resolver + layout pass
--model-ner ID             Override model for NER
--model-geo-validation ID  Override model for geo-validation
--model-ground-truth ID    Override model for GT matching
--thinking LEVEL           Default thinking level (none/low/medium/high)
--thinking-layout          Thinking for detection (default: high)
--thinking-transcription   Thinking for reads (default: low)
--thinking-merge           Thinking for merge + layout (default: medium)
--ensemble-k N             Number of M1 crop reads per region (default: 1)
--transcription-workers N  Concurrent per-region workers (default: 6)
--no-consistency           Skip Step 4 (layout pass)
--no-geo-validation        Skip geo-validation
--ground-truth-tei PATH    GT TEI — enables Gemini/GT/Diff toggle in the viewer
--embed-images             Base64-embed facsimiles in index.html
--image-ref-prefix URL     Reference facsimiles via an external URL prefix
--title TEXT               HTML edition title
```

All model IDs and thinking levels can also be set as environment variables (`MODEL_ID`, `MODEL_ID_MERGE`, `THINKING_LEVEL_MERGE`, `LLM_MAX_CONCURRENCY`, etc.). See `src/config.py` for the full list.

---

## Output structure

```
output/
├── json/
│   ├── page_0001.json            per-page result (regions, entities, locations, GT)
│   └── ...
├── digital_edition_complete.json all pages combined
├── digital_edition.tei.xml       full-book TEI-P5 XML
├── geocode_cache.json            location resolution cache (reused on resume)
└── humboldt_england_1790.html    self-contained HTML edition (or as named by runner)
```

The HTML edition includes:
- Side-by-side facsimile + transcription with region highlighting
- Per-region language badges (DE / FR / LA / ESP)
- Entity highlighting for persons, locations, instruments, species, …
- Leaflet map of all geolocated places
- Per-page TEI-P5 download
- **Gemini / Ground Truth / Diff** toggle on pages where GT matching ran

The pipeline resumes automatically: pages with an existing non-empty JSON in `json/` are skipped.

---

## Region types

| Type | Description |
|------|-------------|
| `entry_heading` | Numbered entry header (e.g. "N. 50-52", "9)") |
| `main_text` | Primary journal prose |
| `marginal_note` | Annotations in the margins |
| `pasted_slip` | Separate slip of paper physically pasted to the page |
| `calculation` | Astronomical / mathematical computation blocks |
| `observation_table` | Structured observational data (angles, times, measurements) |
| `sketch` | Pen drawings, landscape profiles, diagrams |
| `crossed_out` | Struck-through passages |
| `instrument_list` | Scientific instruments, often with prices |
| `page_number` | Folio number (usually top corner) |

---

## Entity types

NER runs on the assembled full text of each page and produces eight entity classes:

| Class | What is extracted |
|-------|------------------|
| `Person` | Named persons: scientists, instrument makers, local informants, etc. |
| `Location` | Named places: cities, rivers, mountains, missions, forts |
| `Indigenous_Group` | Peoples, ethnic groups and language groups (mainly American journals) |
| `Instrument` | Scientific instruments and measuring devices |
| `Species` | Plants, animals, minerals by name (Latin or vernacular) |
| `Publication` | Books, maps, journals and atlases cited by Humboldt |
| `Celestial_Object` | Stars, planets, constellations in an astronomical context |
| `Measurement` | Coordinates, temperatures, pressures and other quantified readings |

---

## Optional post-processing: entity linking

After the main pipeline finishes, you can link extracted persons, places and species to the **edition-humboldt-digital** authority registers:

```bash
# Build the index from a local clone of telota/edition-humboldt-digital
python scripts/link_entities.py \
    --input output/digital_edition_complete.json \
    --register path/to/edition-humboldt-digital \
    --out output/digital_edition_linked.json
```

Matching uses fuzzy string search (configurable via `--fuzzy-cutoff`, default 0.9). Results are stored as `ehd_uri` on each matched entity mention and written to a separate output JSON so the original is never overwritten.

---

## Ground-truth comparison

Pass `--ground-truth-tei PATH` to enable per-page comparison against an existing scholarly transcription (TEI-P5 from edition-humboldt.de or any compatible file).

For each page whose folio label appears in the TEI:
1. A multimodal call receives the page image, the detected regions and Humboldt's own transcription text.
2. The model assigns a GT text segment to each detected region.
3. Both the pipeline transcription and the matched GT segment are stored on the region object — neither overwrites the other.
4. The HTML viewer exposes a **Gemini / Ground Truth / Diff** three-way toggle for those pages.

---

## Configuration

Key environment variables (can also go in `.env`):

| Variable | Default | Effect |
|----------|---------|--------|
| `GEMINI_API_KEY` | — | Required |
| `MODEL_ID` | `gemini-3-flash-preview` | Default model for all stages |
| `MODEL_ID_BEST` | `gemini-3.5-flash` | Used for merge resolver + layout pass |
| `MODEL_ID_MERGE` | (MODEL_ID_BEST) | Override merge + layout model |
| `LLM_MAX_CONCURRENCY` | `8` | Global cap on simultaneous Gemini calls |
| `ENSEMBLE_K_CROP` | `1` | M1 crop reads per region |
| `TEMPERATURE_READ` | `0.2` | M1/M2 read temperature |
| `TEMPERATURE_READ_STRUCTURED` | `1.0` | M3 temperature (kept high for diversity) |
| `TEMPERATURE_MERGE` | `0.0` | Merge + layout temperature |
| `PAGE_READ_MAX_PX` | `2000` | Longest-side resolution for whole-page reads |
| `EHD_REGISTER_DIR` | — | Path to local edition-humboldt-digital clone (entity linker) |

---

## Editorial conventions

- Original orthography and punctuation preserved throughout
- Uncertain readings marked `[?]`; the merge resolver attempts to resolve them from the ink before locking them in
- Crossed-out passages retained with strikethrough
- Interlinear additions and marginal notes each get their own region with explicit type
- Languages tracked per region (DE / FR / LA / ESP)

---

## License

MIT

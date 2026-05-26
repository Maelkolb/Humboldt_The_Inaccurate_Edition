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
| 2.5. Consistency Check | `consistency_check.py` | **Multimodal QA pass.** Looks at the page image alongside every region and (a) fixes structural problems like duplicate lines, contaminated main-text, language mismatches, and (b) attempts to resolve every word marked `[?]` directly from the ink, dropping the marker only when a confident reading can be supplied. |
| 3. Entity Annotation | `ner.py` | NER for persons, locations, institutions, instruments, publications, celestial objects, measurements, natural objects |
| 4. Georeferencing | `geocoding.py` | Location resolution with a historical place-name mapping (Oedenburg→Sopron, Preßburg→Bratislava, etc.) |
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
├── humboldt_edition.html             # Interactive scholarly HTML edition
│                                     # • side-by-side facsimile + transcription
│                                     # • per-page "TEI" download button
│                                     # • Gemini / Ground Truth / Diff toggle
│                                     #   (only on pages where --ground-truth-tei
│                                     #    produced a match)
└── geocode_cache.json
```

## TEI XML output

The pipeline always writes a TEI document at `output/digital_edition.tei.xml`
modelled on edition-humboldt digital's encoding conventions:

| Element | Carries |
|---|---|
| `<pb n="2r" facs="…"/>` | Page break per folio |
| `<fw type="folNum">…</fw>` | Page numbers Humboldt wrote himself |
| `<fw type="catch">…</fw>` | Catch-phrases |
| `<div type="diaryEntry"><head>…</head><p>…</p></div>` | Diary entries |
| `<note hand="#author" place="left|right|mTop|mBottom|opposite">` | Marginal notes |
| `<note rend="sticked">` | Pasted slips |
| `<hi rendition="#u">…</hi>` | Underlined text |
| `<del rendition="#s">…</del>` | Crossed-out text |
| `<unclear>…</unclear>` | Words the pipeline marked `[?]` |
| `<supplied>…</supplied>` | Editorial supplies (square brackets in the transcription) |
| `<gap unit="…" quantity="…" reason="illegible"/>` / `<gap/>` | Illegible passages |
| `<lb/>` | Line breaks |
| `<persName ref="…"/>`, `<placeName ref="…"/>`, `<orgName ref="…"/>` | Entities |
| `<figure><figDesc>…</figDesc></figure>` | Sketches |

The HTML edition's **TEI** button next to the page toolbar downloads the
same TEI as a single-page self-contained file (`folio_2r.tei.xml`, …).

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

In the HTML viewer this adds a **source toggle** to the page toolbar:

- **Gemini** — what the pipeline produced (default; identical to the
  no-GT view).
- **Ground Truth** — the scholarly transcription from the GT TEI.
- **Diff** — word-level diff of Gemini vs. ground-truth, with
  `git diff`-style highlighting: red strikethrough for tokens only in
  Gemini (misreads, hallucinations), green underline for tokens only in
  the ground-truth (missed words).

Press **S** to cycle through the three modes from the keyboard.

Pages whose folio label has no match in the GT TEI fall back silently to
Gemini-only display.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| ← / → | Previous / next page (hold Shift to jump to first / last) |
| T | Open/close table of contents |
| R | Toggle Document ↔ Reading transcription mode |
| F | Toggle layout (full-width facsimile etc.) |
| B | Toggle region overlay on the facsimile |
| S | Cycle source mode: Gemini → Ground Truth → Diff (when GT was matched) |

## License

MIT
